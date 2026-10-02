# ====================================================================================================
# plugins.py (v52.2) - mods foundation. Load order: after playback.py, before main_window.py.
# Mods are .py scripts in the `mods` folder next to the program. NOTHING is executed unless the mod is ticked in
# Preferences > Plug-ins (pref_mods_on); metadata is read with `ast` only. Three kinds:
#   MOD_TYPE = "tool" -> an icon in the tool row (several tools share ONE stacked button; click it again to list them)
#   MOD_TYPE = "mode" -> a tab in the title bar next to Video / GIF that swaps the whole window for build_mode(api)'s widget
#   MOD_TYPE = "tab"  -> a window (dock panel) like Project / Preview / Timeline, next to Preview; listed in the eye menu
# Script contract (module-level constants):
#   MOD_NAME, MOD_TYPE, MOD_ICON (a glyph, or a png/svg file name inside mods/; tool only), MOD_DESC, MOD_AUTHOR
#   tool: on_tool_selected(api) / on_tool_deselected(api)     mode: build_mode(api) -> QWidget, on_mode_shown(api)
#   tab:  build_tab(api) -> QWidget                            any: on_unload(api)
#   [52.24] A mode may return the string "editor" from build_mode: it then SHARES the main editor page (no own page) and gets
#   on_mode_shown(api) when its tab is clicked and on_mode_hidden(api) when the user goes back to Video / GIF or another mode.
# `api` = ModAPI (win, engine, seq, tl, stage, status(msg), add_dock/remove_dock [52.25], mods_dir). Ticking a mod loads it live; unticking removes its UI and calls
# on_unload, but Python cannot un-import its code -> Preferences warns that a restart is needed to fully unload it.
# ====================================================================================================

import os
import json
import sys
import re
import math
import time
import queue
import ctypes
import glob
import bisect
import shutil
import hashlib
import tempfile
import threading
import subprocess

# [STALE][KNOWN ISSUE F14] Unused imports (safe to delete): QRect, QRegion, QCursor (QtCore/QtGui) and
# QVideoWidget (QtMultimediaWidgets - replaced by VideoView/QGraphicsVideoItem). Left as-is on purpose.
from PySide6.QtCore import (Qt, QUrl, QTimer, QObject, Signal, QRectF, QPointF, QLineF,
                            QSize, QMimeData, QThread, QPoint, QEvent, QRect, QSizeF, QSettings,
                            QVariantAnimation, QEasingCurve)
from PySide6.QtGui import (QAction, QColor, QPainter, QPen, QPixmap, QIcon, QPalette, QFont,
                           QPolygonF, QKeySequence, QShortcut, QPainterPath, QRegion, QIntValidator,
                           QCursor, QDesktopServices, QTransform, QDrag)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QSplitter, QLabel, QToolButton, QPushButton, QListWidget,
                               QListWidgetItem, QFileDialog, QMessageBox, QScrollBar, QSlider,
                               QMenu, QCheckBox, QFrame, QProgressDialog, QButtonGroup,
                               QAbstractItemView, QInputDialog, QLineEdit, QDialog, QDoubleSpinBox,
                               QGraphicsView, QGraphicsScene, QComboBox, QSpinBox, QGridLayout, QSizePolicy,
                               QColorDialog)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget, QGraphicsVideoItem

from utils import *
from media_model import *
from probing import *
from export_worker import *
from dialogs import *
from widgets import *
from preview_stack import *
from timeline import *
from playback import *
import ast
import importlib.util


class ModInfo:
    def __init__(self, path):
        self.path, self.file = path, os.path.basename(path)
        self.name, self.type, self.icon, self.desc, self.author = os.path.splitext(self.file)[0], "tool", "\u2726", "", ""
        self.module, self.error = None, ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    k = node.targets[0].id
                    if k in ("MOD_NAME", "MOD_TYPE", "MOD_ICON", "MOD_DESC", "MOD_AUTHOR"):
                        v = ast.literal_eval(node.value)
                        if isinstance(v, str):
                            setattr(self, k[4:].lower(), v)
        except Exception as e:
            self.error = f"unreadable: {e}"
        self.type = self.type.lower() if self.type.lower() in ("tool", "mode", "tab") else "tool"

    @property
    def enabled(self):
        return self.file in pref_json("pref_mods_on", [])       # opt-in: new scripts are OFF until ticked


def scan_mods():
    try:
        os.makedirs(MODS_DIR, exist_ok=True)
        mods = [ModInfo(p) for p in sorted(glob.glob(os.path.join(MODS_DIR, "*.py"))) if not os.path.basename(p).startswith("_")]
        pos = {f: n for n, f in enumerate(pref_json("pref_mods_order", []))}      # [52.23] user order (Preferences > Plug-ins)
        mods.sort(key=lambda m: pos.get(m.file, len(pos)))                          # stable: unlisted mods stay alphabetical, last
        return mods
    except Exception:
        return []


def mod_icon(info, size=20):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    f = os.path.join(MODS_DIR, info.icon)
    if info.icon.lower().endswith((".png", ".svg", ".jpg")) and os.path.isfile(f):
        src = QPixmap(f)
        if not src.isNull():
            pm = src.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            return QIcon(pm)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor("#d4d4d4"))
    ft = QFont()
    ft.setPixelSize(int(size * 1.5))
    p.setFont(ft)
    p.drawText(QRectF(0, 0, size * 2, size * 2), Qt.AlignmentFlag.AlignCenter, (info.icon or "\u2726")[:2])
    p.end()
    return QIcon(pm)


class ModAPI:
    def __init__(self, win):
        self.win, self.mods_dir = win, MODS_DIR

    engine = property(lambda s: s.win.engine)
    seq = property(lambda s: s.win.seq)
    tl = property(lambda s: s.win.tl)            # [52.9] Timeline widget (has .lanes)
    stage = property(lambda s: s.win.stage)      # [52.9] VideoStage (preview)

    def status(self, msg, ms=5000):
        self.win.statusBar().showMessage(str(msg), ms)

    def add_dock(self, key, title, widget):
        """[52.25] Dock panel owned by a mod of ANY type (e.g. a mode's side panels). Use a unique key like "mod:<file>:<name>".
        The mod must remove_dock() it again when it no longer wants it (on_mode_hidden / on_unload). No-op if the key exists."""
        if key not in self.win.mod_docks:
            self.win.add_mod_dock(key, title, widget)

    def remove_dock(self, key):
        self.win.remove_mod_dock(key)


class _StackPopup(QFrame):
    """Scrollable icon list of every plugin tool (opened by clicking the already-active plugin button)."""
    def __init__(self, infos, cur, pick, parent):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("pluginPopup")
        self.setStyleSheet("QFrame#pluginPopup{background:#2a2a2a;border:1px solid #555;}")
        from PySide6.QtWidgets import QScrollArea
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        sa = QScrollArea()
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidgetResizable(True)
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(2, 2, 2, 2)
        col.setSpacing(2)
        for i in infos:
            b = QToolButton()
            b.setIcon(mod_icon(i))
            b.setIconSize(QSize(20, 20))
            b.setToolTip(i.name)
            b.setCheckable(True)
            b.setChecked(i is cur)
            b.setFixedSize(34, 30)
            b.clicked.connect(lambda _=False, i=i: (pick(i), self.close()))
            col.addWidget(b)
        col.addStretch(1)
        sa.setWidget(inner)
        lay.addWidget(sa)
        self.setFixedSize(46, min(len(infos), 6) * 32 + 10)


class PluginToolStack(QToolButton):
    """ONE tool-row button standing for all plugin tools. Click = activate the top one; click again while active
    (and >1 exist) = popup list to pick another (which then becomes the top)."""
    picked = Signal(object)

    def __init__(self):
        super().__init__()
        self.infos, self.cur, self._was_on = [], None, False
        self.setCheckable(True)
        self.setIconSize(QSize(20, 20))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clicked.connect(self._clicked)

    def paintEvent(self, e):
        """[52.3] With more than one plugin tool: draw a box around the button and a mini arrow (bottom-right) = "stacked"."""
        super().paintEvent(e)
        if len(self.infos) > 1:
            q = QPainter(self)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            c = QColor(theme_map()["accent"])
            q.setPen(QPen(c, 1.2))
            q.setBrush(Qt.BrushStyle.NoBrush)
            q.drawRoundedRect(QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6), 3, 3)
            r = self.rect()
            x, y = r.right() - 3.0, r.bottom() - 3.0
            q.setPen(Qt.PenStyle.NoPen)
            q.setBrush(c.lighter(140))
            q.drawPolygon(QPolygonF([QPointF(x - 6, y - 3.5), QPointF(x, y - 3.5), QPointF(x - 3, y)]))
            q.end()

    def set_mods(self, infos):
        self.infos = list(infos)
        if self.cur not in self.infos:
            self.cur = self.infos[0] if self.infos else None
        self.setVisible(bool(self.infos))
        self._refresh()

    def _refresh(self):
        if not self.cur:
            return
        n = len(self.infos)
        self.setIcon(mod_icon(self.cur))
        self.setToolTip(f"Plugin tool: {self.cur.name} ({keybind_map()['tool_plugin']} cycles)"
                        + (f"\n{n} plugin tools - click again to list them" if n > 1 else ""))

    def mousePressEvent(self, e):
        self._was_on = self.isChecked()
        super().mousePressEvent(e)

    def _clicked(self, _=False):
        if self._was_on and len(self.infos) > 1:
            pop = _StackPopup(self.infos, self.cur, self._pick, self)
            pop.move(self.mapToGlobal(QPoint(0, -pop.height())))
            pop.show()
        else:
            self.picked.emit(self.cur)

    def cycle(self):
        """[52.4] Keybind (default Z): first press activates the current plugin tool, each further press while it is
        active switches to the next plugin tool (which becomes the top of the stack)."""
        if not self.infos:
            return
        if not self.isChecked():
            self.setChecked(True)
            self.picked.emit(self.cur)
            return
        if len(self.infos) > 1:
            self._pick(self.infos[(self.infos.index(self.cur) + 1) % len(self.infos)])

    def _pick(self, info):
        self.cur = info
        self._refresh()
        self.picked.emit(info)


class PluginHost:
    def __init__(self, win):
        self.win, self.api = win, ModAPI(win)
        self.loaded, self.active = {}, None          # file -> ModInfo (with .page/.btn for modes, .dock_key for tabs)
        self.shown = None                            # [52.24] the mode mod whose tab is currently selected
        win.plugin_stack.picked.connect(self._tool_picked)

    def _call(self, info, fn, *a):
        f = getattr(info.module, fn, None) if info.module else None
        if callable(f):
            try:
                return f(self.api, *a)
            except Exception as e:
                self.api.status(f"Mod '{info.name}' error in {fn}: {e}", 8000)

    def broadcast(self, fn):
        """[52.11] Call fn(api) on every loaded mod that defines it (e.g. on_clear)."""
        for info in list(self.loaded.values()):
            self._call(info, fn)

    def _tool_picked(self, info):
        self.win.set_tool("select")                  # plugin tools sit on top of the selection behaviour
        self.active = info
        self._call(info, "on_tool_selected")

    def deactivate(self):
        if self.active:
            info, self.active = self.active, None
            self._call(info, "on_tool_deselected")

    def sync(self):
        """Make the loaded set equal the ticked set: load newly ticked mods (code runs only now), unload unticked ones.
        Returns the names of mods that were unloaded (their code stays imported -> restart needed to fully drop it)."""
        infos = {i.file: i for i in scan_mods()}
        want = {f for f, i in infos.items() if i.enabled and not i.error}
        gone = [self._unload(f) for f in list(self.loaded) if f not in want]
        for f in [f for f in infos if f in want and f not in self.loaded]:          # [52.23] load in the user's order
            self._load(infos[f])
        rank = {f: n for n, f in enumerate(infos)}
        self.win.plugin_stack.set_mods(sorted((i for i in self.loaded.values() if i.type == "tool"),
                                              key=lambda i: rank.get(i.file, 1 << 20)))   # tool stack / Z cycling follow it live
        return gone

    def _load(self, info):
        w = self.win
        try:
            spec = importlib.util.spec_from_file_location("sqvce_mod_" + os.path.splitext(info.file)[0], info.path)
            info.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(info.module)
            self._call(info, "on_load")                   # [52.9] any type: optional on_load(api)
            if info.type == "mode":
                page = self._call(info, "build_mode")
                info.shared = page == "editor"            # [52.24] mode that reuses the editor page
                if info.shared:
                    page = w.page_stack.widget(0)
                else:
                    if not isinstance(page, QWidget):
                        page = QLabel(f"{info.name}\n\n(blank mode)")
                        page.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    w.page_stack.addWidget(page)
                info.page = page
                info.btn = w.titlebar.add_tab(info.name, lambda info=info: self._show_mode(info))
            elif info.type == "tab":
                page = self._call(info, "build_tab")
                if not isinstance(page, QWidget):
                    page = QLabel(f"{info.name}\n\n(blank tab)")
                    page.setAlignment(Qt.AlignmentFlag.AlignCenter)
                info.dock_key = "mod:" + info.file
                w.add_mod_dock(info.dock_key, info.name, page)
            self.loaded[info.file] = info
        except Exception as e:
            info.error = str(e)
            w.statusBar().showMessage(f"Mod '{info.file}' failed to load: {e}", 8000)

    def _unload(self, file):
        info, w = self.loaded.pop(file), self.win
        if self.active is info:
            self.deactivate()
        if self.shown is info:
            self.mode_left()
        self._call(info, "on_unload")
        if info.type == "mode":
            w.titlebar.remove_tab(info.btn)
            if not getattr(info, "shared", False):
                w.page_stack.removeWidget(info.page)
                info.page.deleteLater()
        elif info.type == "tab":
            w.remove_mod_dock(info.dock_key)
        info.module = None
        return info.name

    def _show_mode(self, info):
        self.win.engine.pause()
        if self.shown is not None and self.shown is not info:
            self.mode_left()
        self.shown = info
        self.win.page_stack.setCurrentWidget(info.page)
        self._call(info, "on_mode_shown")

    def mode_left(self):
        """[52.24] Called when the user leaves a mod mode (MainWindow.request_mode, another mode tab, unticking the mod)."""
        if self.shown is not None:
            info, self.shown = self.shown, None
            self._call(info, "on_mode_hidden")
