# ====================================================================================================
# Auto-split from SQVCEversion47.py on 2026-09-27 - see ARCHITECTURE.md for the module map.
# This file's code is verbatim from the original monolith (only imports were added/reorganized).
# Each module imports * from every module before it in the load order below, so any name defined
# anywhere earlier in the original file is guaranteed available here too (matches the flat
# namespace the original single file had). No circular imports: order is fixed and linear.
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

# [52.34] PyInstaller only bundles modules the program imports statically; mods are loaded from disk at runtime, so a stdlib
# module that ONLY a mod imports is missing in the .exe ("Mod 'x.py' failed to load"). Listed here so frozen builds include
# them: uuid / urllib / ssl / http (gif_tab, text_tool, fx_tab, bookmarks) plus common extras for future mods. A mod that needs
# anything else (stdlib or pip) must have it added here or in the .spec hiddenimports.
import uuid, atexit, base64, csv, zipfile, secrets, hmac, html, datetime, itertools, collections
import ssl, http.client
import urllib.request, urllib.parse, urllib.error
import concurrent.futures

# [STALE][KNOWN ISSUE F14] Unused imports (safe to delete): QRect, QRegion, QCursor (QtCore/QtGui) and
# QVideoWidget (QtMultimediaWidgets - replaced by VideoView/QGraphicsVideoItem). Left as-is on purpose.
from PySide6.QtCore import (Qt, QUrl, QTimer, QObject, Signal, QRectF, QPointF, QLineF,
                            QSize, QMimeData, QThread, QPoint, QEvent, QRect, QSizeF, QSettings,
                            QVariantAnimation, QEasingCurve, QEventLoop)
from PySide6.QtGui import (QAction, QColor, QPainter, QPen, QPixmap, QIcon, QPalette, QFont,
                           QPolygonF, QKeySequence, QShortcut, QPainterPath, QRegion, QIntValidator,
                           QCursor, QDesktopServices, QTransform, QDrag)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QSplitter, QLabel, QToolButton, QPushButton, QListWidget,
                               QListWidgetItem, QFileDialog, QMessageBox, QScrollBar, QSlider,
                               QMenu, QCheckBox, QFrame, QProgressDialog, QButtonGroup,
                               QAbstractItemView, QInputDialog, QLineEdit, QDialog, QDoubleSpinBox,
                               QGraphicsView, QGraphicsScene, QComboBox, QSpinBox, QGridLayout, QSizePolicy,
                               QColorDialog, QDockWidget)
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
from plugins import *
from audio_track import *
from recovery import *

# ----------------------------------------------------------------------------- styling
# [MAP] Application-wide Qt style sheet (dark theme) applied in main() on top of the Fusion style + dark_palette().
# Widgets are targeted by objectName (#panel, #titlebar, #primary, #saveover, #warnPill, #projectRow ...). Renaming
# an objectName in Python without updating this sheet silently un-styles the widget. The stylesheet colours the
# program only; the video surface is black regardless.
QSS = """
* { font-family: "Segoe UI", Arial, sans-serif; font-size: 9pt; color: #c8c8c8; }
QMainWindow, QWidget#root { background: #1a1a1a; }
QFrame#panel { background: #232323; border: 1px solid #101010; }
QFrame#panelHead { background: #2a2a2a; border: none; border-bottom: 1px solid #101010;
                   min-height: 28px; max-height: 28px; }
QLabel#panelTitle { color: #eaeaea; font-weight: 600; }
QLabel#tc { color: #2d8ceb; font-family: Consolas, monospace; font-size: 13pt; font-weight: bold; }
QLabel#tcDim { color: #8f8f8f; font-family: Consolas, monospace; font-size: 13pt; }
QWidget#windowFrame { background: #050505; border: 1px solid #050505; }
QFrame#titlebar { background: #141414; border-bottom: 1px solid #101010; }
QToolButton#winBtn { border-radius: 0; padding: 0; }
QToolButton#winBtn:hover { background: #333333; }
QToolButton#closeBtn:hover { background: #e0413a; }
QFrame#topbar { background: #1a1a1a; border-bottom: 1px solid #101010; }
QFrame#controlbar { background: #262626; border-top: 1px solid #101010; }
QFrame#nav { background: #232323; border-top: 1px solid #101010; }
QFrame#optSection { background: #262626; border: 1px solid #3a3a3a; border-radius: 4px; }
QMainWindow::separator { background: #101010; width: 3px; height: 3px; }
QDockWidget { color: #eaeaea; }
QDockWidget::title { background: #2a2a2a; padding: 5px 8px; border-bottom: 1px solid #101010; }
QSplitter::handle { background: #101010; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical { height: 3px; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 3px; }
QToolButton:hover { background: #3a3a3a; }
QToolButton:pressed { background: #2d8ceb; }
QToolButton:checked { background: #3a3a3a; border-color: #2d8ceb; }
QPushButton { background: #383838; border: 1px solid #4a4a4a; border-radius: 3px; padding: 4px 14px; }
QPushButton:hover { background: #454545; }
QPushButton:disabled { color: #666; }
QPushButton#primary { background: #2d8ceb; border-color: #2d8ceb; color: #ffffff; font-weight: 600;
                      padding: 5px 18px; }
QPushButton#primary:hover { background: #4aa0f5; }
QPushButton#saveover { background: #3a3a3a; border-color: #5c5c5c; font-weight: 600; padding: 5px 16px; }
QPushButton#saveover:hover { background: #4a4a4a; }
QPushButton#helpbtn { padding: 5px 14px; }
QListWidget#projectList { background: #1e1e1e; border: none; outline: none; }
QListWidget#projectList::item { border-bottom: 1px solid #292929; }
QListWidget#projectList::item:selected { background: #2d5a8a; }
QFrame#projectRow { background: transparent; }
QScrollBar:horizontal { background: #1e1e1e; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #4a4a4a; border-radius: 5px; min-width: 30px; margin: 1px; }
QScrollBar::handle:horizontal:hover { background: #5c5c5c; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSlider::groove:horizontal { height: 4px; background: #454545; border-radius: 2px; }
QSlider::handle:horizontal { background: #c0c0c0; width: 10px; margin: -4px 0; border-radius: 5px; }
QCheckBox:disabled { color: #666; }
QMenu { background: #2a2a2a; border: 1px solid #444; padding: 4px 0; }
QMenu::item { padding: 5px 28px 5px 20px; }
QMenu::item:selected { background: #2d8ceb; color: #ffffff; }
QMenu::item:disabled { color: #666; }
QMenu::separator { height: 1px; background: #444; margin: 4px 8px; }
QStatusBar { background: #1a1a1a; color: #9a9a9a; }
QToolTip { background: #2a2a2a; color: #dddddd; border: 1px solid #555; }
QProgressDialog, QMessageBox { background: #2b2b2b; }
QProgressBar { background: #1e1e1e; border: 1px solid #444; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #2d8ceb; }
QLabel#warnPill { color: #f4c430; background: #3a3016; border: 1px solid #6b5a1e; border-radius: 3px;
                  padding: 2px 8px; font-size: 8pt; font-weight: 600; }
"""


def dark_palette():
    t = theme_map()
    pal = QPalette()
    R = QPalette.ColorRole
    for role, col in ((R.Window, t["surface"]), (R.WindowText, t["text"]), (R.Base, t["base"]),
                      (R.AlternateBase, t["panel"]), (R.Text, t["text"]), (R.Button, t["button"]),
                      (R.ButtonText, t["text"]), (R.Highlight, t["accent"]), (R.HighlightedText, "#ffffff"),
                      (R.ToolTipBase, t["header"]), (R.ToolTipText, t["text"])):
        pal.setColor(role, QColor(col))
    return pal


# [FEATURE 52.0] QSS with the user's theme colours substituted in ONE regex pass (so swapped colours can't chain).
def build_qss():
    t = theme_map()
    m = {THEME_DEFAULTS[k]: t[k] for k in THEME_DEFAULTS if t[k] != THEME_DEFAULTS[k]}
    if t["accent"] != THEME_DEFAULTS["accent"]:
        a = QColor(t["accent"])
        m["#4aa0f5"], m["#2d5a8a"] = a.lighter(120).name(), a.darker(150).name()
    return re.sub(r"#[0-9a-fA-F]{6}", lambda mo: m.get(mo.group(0).lower(), mo.group(0)), QSS)


# [FEATURE 51.12] Keeps the Timeline dock's height when the WINDOW is resized (extra/lost height goes to the top row, i.e. the
# Preview/Project docks). A dock Resize that arrives together with a new window size is a window resize -> restore the last
# height the layout had at a constant window size (= what the user set by dragging the separator). Enabled shortly after start-up
# so the initial layout passes are not recorded.
class TimelineHeightKeeper(QObject):
    def __init__(self, win, dock):
        super().__init__(win)
        self.win, self.dock = win, dock
        self.h, self.sz, self.pending, self.on = None, win.size(), False, False
        dock.installEventFilter(self)
        QTimer.singleShot(1500, self.enable)

    def enable(self):
        self.on, self.sz, self.h = True, self.win.size(), self.dock.height()

    def eventFilter(self, o, e):
        if self.on and o is self.dock and e.type() == QEvent.Type.Resize:
            sz = self.win.size()
            if sz != self.sz:                       # the window changed size: keep the timeline's height
                self.sz = sz
                if not self.pending and abs(self.dock.height() - self.h) > 1:
                    self.pending = True
                    QTimer.singleShot(0, self.restore)
            elif not self.pending and not self.dock.isHidden():
                self.h = self.dock.height()         # separator drag / layout change at constant window size
        return False

    def restore(self):
        self.pending = False
        d, w = self.dock, self.win
        if self.h is None or d.isHidden() or d.isFloating():
            return
        ref = next((x for x in (w.docks["preview"], w.docks["project"]) if not x.isHidden() and not x.isFloating()), None)
        delta = d.height() - self.h
        if ref is None or abs(delta) <= 1:
            return
        w.dock_host.resizeDocks([ref, d], [max(1, ref.height() + delta), self.h], Qt.Orientation.Vertical)


def eye_icon(color="#d0d0d0"):
    pm = QPixmap(40, 40)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.moveTo(3, 20)
    path.quadTo(20, 3, 37, 20)
    path.quadTo(20, 37, 3, 20)
    p.setPen(QPen(QColor(color), 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(20, 20), 6, 6)
    p.end()
    return QIcon(pm)


# ----------------------------------------------------------------------------- main window
# [MAP] The application shell: builds every panel, owns all state (medias, seq, engine, proxy, worker) and is the
# ONLY place where the parts are wired together (Timeline / Engine / Sequence never import each other's logic).
# LAYOUT  TitleBar | top bar (logo, warnings, Help, GIF preset, Transcode, Save-Over, est. label, Export)
#         Project panel | video column (VideoStage + control bar)   /   Timeline panel
# STATE   medias (ordered list) + by_path / items / rows (dicts keyed by path or Media), primary (Save-Over target,
#         the star), clipboard_seg (9-tuple), seq, worker (MediaWorker), engine, proxy (ReverseProxy),
#         app_mode ("Video" | "GIF"; class attribute default), grid_view (class attribute default).
# DATA FLOW (typical edit): Timeline mouse/menu -> Sequence.edit(fn) -> Sequence.edited ->
#         on_seq_edited: ensure_proxies() -> refresh_stage() -> engine.seek(playhead) -> update_labels() -> tl.update()
# SIGNAL WIRING lives in __init__, build_video_column, build_timeline_panel and build_topbar - read those first
# when a signal "goes nowhere".
class MainWindow(QMainWindow):
    RESIZE_MARGIN = 6
    SPACE_HOLD_MS = 280          # ms of Space held down (while playing) before it's treated as a hold, not a tap

    # [INVARIANT] Construction order matters:
    #   1 seq, worker (MediaWorker starts its thread immediately)        2 UI builders (create self.video, self.tl ...)
    #   3 Engine(seq, self.video) - needs the VideoView from step 2       4 ReverseProxy, and `engine.proxies =
    #   proxy.files` (SHARED dict)                                        5 signal connections that need the engine
    #   6 build_actions / build_shortcuts / update_labels                 7 event filter for frameless-window resizing
    # Lambdas in the builders reference self.engine lazily (they run later), which is why builders can be called
    # before the Engine exists - but any builder code that runs IMMEDIATELY must not touch self.engine.
    # Other start-up facts: window is frameless; Snap/Precise defaults (Precise ON when ffprobe exists); a 3 s timer runs
    # check_system (first run after 400 ms); quiet_ffmpeg_log runs now and at +2 s; command-line video paths are
    # imported on the next event-loop turn; a missing ffmpeg shows a warning box but the app continues.
    def __init__(self, files=None):
        super().__init__()
        self.APP_TITLE = APP_NAME
        self.setWindowTitle(self.APP_TITLE)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self._is_maximized = False
        self._restore_geom = None
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.medias, self.by_path, self.items, self.rows = [], {}, {}, {}
        self.primary = None
        self.clipboard_seg = None
        # --- Space-hold state (see keyPressEvent/keyReleaseEvent/_space_hold_check): a quick tap still
        # toggles play/pause; holding Space down while already playing instead doubles playback speed
        # for as long as it's held, reverting to normal speed (still playing) on release.
        self._space_is_down = False
        self._space_was_playing = False
        self._space_boosted = False
        self.seq = Sequence()
        self.seq.snap = bool(FFPROBE)
        self.worker = MediaWorker()
        self.worker.ready.connect(self.on_media_ready)
        self.worker.progress.connect(self.on_media_progress)

        outer = QWidget()
        outer.setObjectName("windowFrame")
        self.setCentralWidget(outer)
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(1, 1, 1, 1)
        outer_lay.setSpacing(0)
        self.titlebar = TitleBar(self)
        outer_lay.addWidget(self.titlebar)

        root = QWidget()
        root.setObjectName("root")
        from PySide6.QtWidgets import QStackedWidget
        self.page_stack = QStackedWidget()          # [52.0] page 0 = the editor; mod tabs are added after it
        self.page_stack.addWidget(root)
        outer_lay.addWidget(self.page_stack, 1)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.build_topbar())

        v.addWidget(self.build_docks(), 1)

        self.engine = Engine(self.seq, self.video)
        self.atrack = AudioTrack(self)                  # [52.19] audio strip above the video row (needs tl + engine)
        self.proxy = ReverseProxy()
        self.engine.proxies = self.proxy.files
        # [PERF] Adaptive-resolution scrubbing (see ScrubProxy). Forwarded cancel_media/shutdown via companions.
        self.scrub_proxy = ScrubProxy()
        self.scrub_proxy.in_use = lambda: self.engine.path
        self.proxy.companions.append(self.scrub_proxy)
        self.engine.scrub_files = self.scrub_proxy.files
        self.engine.scrub_proxy = self.scrub_proxy
        self.proxy.ready.connect(self.on_proxy_ready)
        self.engine.playheadChanged.connect(self.on_playhead)
        self.engine.playStateChanged.connect(
            lambda p: self.play_btn.setIcon(icon("pause" if p else "play")))
        self.engine.error.connect(lambda m: self.statusBar().showMessage("Playback: " + m, 8000))
        self.seq.edited.connect(self.on_seq_edited)
        self.tl.thumbReady.connect(self.tl._on_thumb_ready)
        self.engine.perfChanged.connect(self.on_perf_changed)
        self.engine.audioLevel.connect(self.vu.set_level)
        self.engine.playStateChanged.connect(self.vu.set_playing)
        self._sys_timer = QTimer(self)
        self._sys_timer.setInterval(3000)
        self._sys_timer.timeout.connect(self.check_system)
        self._sys_timer.start()
        QTimer.singleShot(400, self.check_system)
        quiet_ffmpeg_log()
        QTimer.singleShot(2000, quiet_ffmpeg_log)      # again once Qt's backend has surely loaded

        self.build_actions()
        self.plugins = PluginHost(self)
        self.plugins.sync()
        self.update_labels()
        QApplication.instance().installEventFilter(self)
        self.statusBar().showMessage(
            "Drag videos into the Project panel or straight onto the timeline  |  drag a clip's edge to trim")

        if not FFMPEG:
            QMessageBox.warning(self, "ffmpeg not found",
                                "Could not find ffmpeg.exe.\n\nPlace ffmpeg.exe and ffprobe.exe next to this "
                                "program (or add them to PATH), then restart.")
        # [52.7] Crash recovery: on the first event-loop turn (window already shown) it offers the previous session
        # if the last run ended abnormally, and only THEN imports the command-line files.
        self.recovery = Recovery(self)
        QTimer.singleShot(0, lambda: self.recovery.startup(files))

    # ------------------------------------------------------------------ [FEATURE v50.2] modular dock layout
    # Project / Preview / Timeline are QDockWidgets inside an inner QMainWindow (self.dock_host, hidden central
    # widget). LOCKED (default): no title bars, panels can't move (separators still resize). UNLOCKED (right-click a
    # panel header / dock title / separator -> "Unlock layout"): dock titles appear, drag them to re-dock or tab; the
    # layout re-locks after one move (or via "Lock layout"). Layout = QSettings "dock_layout" (saved on lock + close).
    def build_docks(self):
        h = self.dock_host = QMainWindow()
        h.setWindowFlags(Qt.WindowType.Widget)
        h.setDockNestingEnabled(True)
        h.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)  # no AnimatedDocks: animation re-lays-out Timeline/Video every frame (lag)
        ph = QWidget()
        h.setCentralWidget(ph)
        ph.hide()
        self._layout_unlocked = False
        self._layout_busy = True
        self.docks = {}
        self.mod_docks = {}                          # [52.2] "tab" mods: key "mod:file.py" -> QDockWidget (not in the saved layout)
        _hd = QSettings("QuickCut", "QuickCut").value("hidden_docks", [])
        try:
            self._dock_sizes = {k: tuple(v) for k, v in json.loads(QSettings("QuickCut", "QuickCut").value("dock_sizes", "{}", str)).items()}
        except Exception:
            self._dock_sizes = {}                    # key -> (width, height) each panel had when it was last hidden
        self._hidden_docks = set(_hd if isinstance(_hd, (list, tuple)) else ([_hd] if _hd else []))     # panels hidden via the eye button
        for key, title, w in (("project", "Project", self.build_project()),
                              ("preview", "Preview", self.build_video_column()),
                              ("timeline", "Timeline", self.build_timeline_panel())):
            d = QDockWidget(title)
            d.setObjectName("dock_" + key)
            d.setWidget(w)
            d.dockLocationChanged.connect(self._on_dock_moved)
            d.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            d.customContextMenuRequested.connect(lambda p, d=d: self.layout_menu(d, p))
            d._empty_title = QWidget()      # reused (never re-created) so no widget is destroyed mid-drag
            d._empty_title.setFixedHeight(0)
            self.docks[key] = d
        for w in [h] + [f for pn in (self.proj_panel, self.tl_panel) for f in pn.findChildren(QFrame, "panelHead")] \
                + self.video.window().findChildren(QFrame, "controlbar"):
            w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            w.customContextMenuRequested.connect(lambda p, w=w: self.layout_menu(w, p))
        self._default_dock_layout()
        st = QSettings("QuickCut", "QuickCut").value("dock_layout")
        if st:
            try:
                if not h.restoreState(st):
                    self._default_dock_layout()
            except Exception:
                self._default_dock_layout()
        self._apply_dock_lock()
        for k in self._hidden_docks:
            if k in self.docks:
                self.docks[k].setVisible(False)
        self._layout_busy = False
        QTimer.singleShot(300, self._validate_layout)
        self._tl_keeper = TimelineHeightKeeper(self, self.docks["timeline"])
        return h

    # Safety net: a dock that is hidden / floating / squashed to nothing (bad drop, bad saved state) -> default layout.
    def _validate_layout(self):
        bad = any(d.isHidden() or d.isFloating() or d.width() < 60 or d.height() < 40
                  for k, d in self.docks.items() if k not in self._hidden_docks)   # user-hidden panels are fine
        if bad:
            self.reset_layout(silent=True)
        return not bad

    def _default_dock_layout(self):
        h, d = self.dock_host, self.docks
        for x in d.values():
            x.setFloating(False)
            h.removeDockWidget(x)
        h.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, d["project"])
        h.splitDockWidget(d["project"], d["preview"], Qt.Orientation.Horizontal)
        h.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, d["timeline"])
        for x in d.values():
            x.setVisible(True)
        def sizes():
            h.resizeDocks([d["project"], d["preview"]], [300, 1020], Qt.Orientation.Horizontal)
            h.resizeDocks([d["project"], d["timeline"]], [560, 220], Qt.Orientation.Vertical)
        sizes()
        QTimer.singleShot(0, sizes)
        for k, x in self.mod_docks.items():
            self._place_mod_dock(x)
            x.setVisible(k not in self._hidden_docks)

    # [FEATURE 51.12] Eye button menu: checkmark = panel shown; clicking toggles it. Persisted in QSettings "hidden_docks".
    def show_panels_menu(self, btn):
        menu = QMenu(self)
        for key, title in (("project", "Project"), ("preview", "Preview"), ("timeline", "Timeline")):
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(key not in self._hidden_docks and not self.docks[key].isHidden())
            act.triggered.connect(lambda on, k=key: self.set_panel_visible(k, on))
        if self.mod_docks:
            menu.addSeparator()
        for key, d in self.mod_docks.items():
            act = menu.addAction(d.windowTitle())
            act.setCheckable(True)
            act.setChecked(key not in self._hidden_docks and not d.isHidden())
            act.triggered.connect(lambda on, k=key: self.set_panel_visible(k, on))
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))

    def set_panel_visible(self, key, on):
        d = self.docks.get(key) or self.mod_docks[key]
        st = QSettings("QuickCut", "QuickCut")
        if on:
            self._hidden_docks.discard(key)
            d.setVisible(True)
            QTimer.singleShot(0, lambda: self._restore_panel_size(key))
            QTimer.singleShot(150, lambda: self._restore_panel_size(key))     # again after Qt's own re-layout settles
        else:
            if not d.isHidden():
                self._dock_sizes[key] = (d.width(), d.height())        # [51.13] remember its size for when it comes back
                st.setValue("dock_sizes", json.dumps(self._dock_sizes))
            self._hidden_docks.add(key)
            d.setVisible(False)
        st.setValue("hidden_docks", sorted(self._hidden_docks))

    # [51.13] Put a re-shown panel back to the size it had: Timeline by height (vertical split with the top row),
    # Project/Preview by width (horizontal split with each other).
    def _restore_panel_size(self, key):
        if key not in self.docks:
            return
        sz, d, h = self._dock_sizes.get(key), self.docks[key], self.dock_host
        if not sz or d.isHidden() or d.isFloating():
            return
        w, ht = sz
        try:
            if key == "timeline":
                ref = next((x for x in (self.docks["preview"], self.docks["project"]) if not x.isHidden()), None)
                if ref is not None:
                    tot = ref.height() + d.height()
                    h.resizeDocks([ref, d], [max(1, tot - ht), ht], Qt.Orientation.Vertical)
                    self._tl_keeper.h = ht
            else:
                other = self.docks["preview" if key == "project" else "project"]
                if not other.isHidden() and not other.isFloating():
                    tot = other.width() + d.width()
                    h.resizeDocks([d, other], [w, max(1, tot - w)], Qt.Orientation.Horizontal)
        except Exception:
            pass

    # [52.2] "tab" mods: a window like Project/Preview/Timeline. Sits right of Preview in the top row, has a Panel header,
    # follows the lock/unlock + eye-menu rules. Its position is NOT persisted (re-placed at every start / layout reset).
    # [52.33] Left (Project) and right (Preview + first mod window) are occupied -> every further mod window is TABBED onto
    # the first one instead of splitting the row thinner. Still unlockable/movable: drag a tab or its title bar anywhere.
    def _place_mod_dock(self, d):
        h, pv = self.dock_host, self.docks["preview"]
        others = [x for x in self.mod_docks.values() if x is not d and not x.isHidden() and not x.isFloating()]
        h.removeDockWidget(d)
        if others:
            h.tabifyDockWidget(others[0], d)
            d.show()
            d.raise_()
            return
        h.splitDockWidget(pv, d, Qt.Orientation.Horizontal)
        w = max(240, d._panel.sizeHint().width())
        QTimer.singleShot(0, lambda: h.resizeDocks([pv, d], [max(200, pv.width() - w), w], Qt.Orientation.Horizontal)
                          if not d.isHidden() and not pv.isHidden() else None)

    def add_mod_dock(self, key, title, widget):
        pn = Panel(title)
        pn.body_lay.addWidget(widget)
        d = QDockWidget(title)
        d.setObjectName("dock_" + key.replace(":", "_").replace(".", "_"))
        d.setWidget(pn)
        d._panel = pn
        d.dockLocationChanged.connect(self._on_dock_moved)
        d.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        d.customContextMenuRequested.connect(lambda p, d=d: self.layout_menu(d, p))
        pn.head_lay.parent().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        pn.head_lay.parent().customContextMenuRequested.connect(lambda p, w=pn.head_lay.parent(): self.layout_menu(w, p))
        d._empty_title = QWidget()
        d._empty_title.setFixedHeight(0)
        self.mod_docks[key] = d
        self._layout_busy = True
        self._place_mod_dock(d)
        self._apply_dock_lock()
        self._layout_busy = False
        d.setVisible(key not in self._hidden_docks)

    def remove_mod_dock(self, key):
        d = self.mod_docks.pop(key, None)
        if d is not None:
            self.dock_host.removeDockWidget(d)
            d.setParent(None)
            d.deleteLater()

    def _apply_dock_lock(self):
        on = self._layout_unlocked
        F = QDockWidget.DockWidgetFeature
        for x in list(self.docks.values()) + list(self.mod_docks.values()):
            x.setFeatures(F.DockWidgetMovable if on else F.NoDockWidgetFeatures)
            x.setTitleBarWidget(None if on else x._empty_title)
        # the Panel headers already show the panel name; hide it while the dock title bar is visible (no duplicate)
        for pn in (self.proj_panel, self.tl_panel) + tuple(x._panel for x in self.mod_docks.values()):
            pn.title.setVisible(not on)

    def set_layout_unlocked(self, on):
        self._layout_unlocked = on
        self._apply_dock_lock()
        if not on:
            self.save_layout()
        self.statusBar().showMessage("Layout unlocked - drag a panel's title bar to move it (locks after one move)"
                                     if on else "Layout locked", 5000)

    def _on_dock_moved(self, *_):
        if self._layout_unlocked and not self._layout_busy:
            QTimer.singleShot(400, self._relock_after_drop)

    def _relock_after_drop(self):
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:     # drag still in progress -> wait
            QTimer.singleShot(200, self._relock_after_drop)
            return
        if self._layout_unlocked:
            self.set_layout_unlocked(False)

    def reset_layout(self, silent=False):
        self._layout_busy = True
        self._layout_unlocked = False
        self._hidden_docks.clear()
        self._dock_sizes = {}
        QSettings("QuickCut", "QuickCut").remove("hidden_docks")
        QSettings("QuickCut", "QuickCut").remove("dock_sizes")
        self._default_dock_layout()
        self._apply_dock_lock()
        self._layout_busy = False
        QSettings("QuickCut", "QuickCut").remove("dock_layout")
        if not silent:
            self.statusBar().showMessage("Layout reset", 4000)

    def save_layout(self):
        if self._validate_layout():          # never persist a broken layout
            QSettings("QuickCut", "QuickCut").setValue("dock_layout", self.dock_host.saveState())

    def layout_menu(self, w, pos):
        m = QMenu(self)
        m.addAction("Lock layout" if self._layout_unlocked else "Unlock layout").triggered.connect(
            lambda: self.set_layout_unlocked(not self._layout_unlocked))
        m.addAction("Reset layout").triggered.connect(self.reset_layout)
        m.exec(w.mapToGlobal(pos))

    # ------------------------------------------------------------------ UI builders
    def tool_btn(self, name, tip, slot=None, checkable=False):
        b = QToolButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(20, 20))
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if slot:
            b.clicked.connect(lambda _=False: slot())
        return b

    # [MAP] Top bar and its persisted settings. All settings live in ONE QSettings("QuickCut","QuickCut"):
    #   gif_sel (str)  last GIF preset TITLE      gif_custom (json list)  user GIF presets
    #   hb_sel  (str)  last Transcode preset TITLE   hb_custom (json list) user Transcode presets
    #   hb_on   (bool) Transcode checkbox state
    # Presets are selected by their TEXT title (findText). Combos store the whole preset dict as item data.
    # [KNOWN ISSUE F9] hb_on is restored at launch, so a user who left Transcode ticked gets re-encoded exports next
    # time, contradicting the "off by default, lossless" expectation from the notes (it is only off on first run).
    # GIF combo/cog are hidden until GIF mode; the Transcode controls are hidden in GIF mode (request_mode).
    def build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(40)
        l = QHBoxLayout(bar)
        l.setContentsMargins(14, 0, 12, 0)
        prefs = QPushButton("Preferences")      # sits directly below the Video / GIF tabs (old "QuickCut" logo spot)
        prefs.setToolTip("Open Preferences")
        prefs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        prefs.clicked.connect(lambda: PreferencesDialog(self).exec())
        l.addWidget(prefs)
        eye = QPushButton()                       # [51.12] show/hide panels
        eye.setIcon(eye_icon())
        eye.setIconSize(QSize(20, 20))
        eye.setToolTip("Show / hide panels")
        eye.setFixedWidth(34)
        eye.setFixedHeight(prefs.sizeHint().height())       # [51.13] same height as the neighbouring buttons
        eye.setStyleSheet("padding: 0px;")
        eye.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        eye.clicked.connect(lambda: self.show_panels_menu(eye))
        l.addWidget(eye)
        help_btn = QPushButton("?")             # [51.8] moved next to Preferences, icon-style
        help_btn.setObjectName("helpbtn")
        help_btn.setToolTip("Help")
        help_btn.setFixedWidth(34)
        help_btn.setFixedHeight(prefs.sizeHint().height())
        help_btn.setStyleSheet("padding: 0px; font-weight: 700;")
        help_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        help_btn.clicked.connect(self.show_help)
        l.addWidget(help_btn)
        self.autosave_lbl = QLabel("")            # [52.7] "Auto-Saving N%" - shown by Recovery only while a write runs
        self.autosave_lbl.setStyleSheet("color:#8f8f8f;font-size:8pt;padding-left:8px;")
        self.autosave_lbl.hide()
        l.addWidget(self.autosave_lbl)
        l.addStretch(1)
        self.warnings = WarningBar()
        l.addWidget(self.warnings)
        l.addSpacing(8)
        est_idx = l.count()                       # [52.4] the size/time estimate is inserted here (left of the presets)
        self.gif_combo = QComboBox()
        self.gif_combo.setMinimumWidth(190)
        self.gif_combo.setToolTip("GIF export preset: width - fps - lossy level - colors")
        self.gif_cog = QToolButton()
        self.gif_cog.setText("\u2699")
        self.gif_cog.setToolTip("Add / edit your own GIF presets")
        self.gif_cog.clicked.connect(self.edit_gif_presets)
        l.addWidget(self.gif_combo)
        l.addWidget(self.gif_cog)
        self.gif_combo.hide()
        self.gif_cog.hide()
        self._gif_settings = QSettings("QuickCut", "QuickCut")
        self.reload_gif_combo(self._gif_settings.value("gif_sel", "", str))
        self.gif_combo.currentIndexChanged.connect(
            lambda _=0: (self._gif_settings.setValue("gif_sel", self.gif_combo.currentText()), self.update_est()))

        self.hb_check = QCheckBox("Transcode")
        self.hb_check.setToolTip("Re-encode the export with a HandBrake-style preset instead of "
                                 "SQVCE n4.0's default lossless export")
        self.hb_check.toggled.connect(self._on_hb_toggled)
        self.hb_combo = QComboBox()
        self.hb_combo.setMinimumWidth(190)
        self.hb_combo.setToolTip("Transcode preset: resolution, fps, encoder and quality/bitrate")
        self.hb_cog = QToolButton()
        self.hb_cog.setText("\u2699")
        self.hb_cog.setToolTip("Add / edit your own Transcode presets")
        self.hb_cog.clicked.connect(self.edit_hb_presets)
        l.addWidget(self.hb_combo)                # [52.4] presets appear LEFT of the checkbox so it never moves
        l.addWidget(self.hb_cog)
        l.addWidget(self.hb_check)
        self._hb_settings = self._gif_settings
        self.reload_hb_combo(self._hb_settings.value("hb_sel", "", str))
        self.hb_combo.currentIndexChanged.connect(
            lambda _=0: (self._hb_settings.setValue("hb_sel", self.hb_combo.currentText()), self.update_est()))
        self.hb_check.setChecked(self._hb_settings.value("hb_on", False, bool))
        self._on_hb_toggled(self.hb_check.isChecked())

        self.saveover_btn = QPushButton("Save-Over")
        self.saveover_btn.setObjectName("saveover")
        self.saveover_btn.setToolTip("Overwrite the primary file (starred in Project) with the current edit")
        self.saveover_btn.clicked.connect(self.save_over)
        l.addWidget(self.saveover_btn)
        self.est_label = QLabel("")
        self.est_label.setStyleSheet("color:#8f8f8f;")
        self.est_label.setToolTip("Rough estimate of the exported file size")
        l.insertWidget(est_idx, self.est_label)
        # [52.33] Advanced mode only: ONE button whose TEXT is the export kind ("Video" / "GIF"), like Precise/Keyframes.
        # Video = lossless or the Transcode checkbox + preset + cog; GIF = GIF preset + cog. Hidden outside Advanced.
        self.kind_btn = QPushButton(self.adv_kind_pref())
        self.kind_btn.setObjectName("kindbtn")
        self.kind_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.kind_btn.setToolTip("Advanced mode: choose what Export makes.\n"
                                 "Video: lossless copy, or re-encode with the Transcode checkbox and its presets.\n"
                                 "GIF: shows the GIF presets; Export writes a .gif.")
        self.kind_btn.clicked.connect(lambda: self.set_export_kind("Video" if self.kind_btn.text() == "GIF" else "GIF"))
        self.kind_btn.hide()
        l.addWidget(self.kind_btn)
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("primary")
        self.export_btn.setToolTip("Export sequence as a new file (Ctrl+M)")
        self.export_btn.clicked.connect(self.export)
        l.addWidget(self.export_btn)
        self._sync_export_bar()                   # [52.33] initial visibility of GIF / Transcode / Save-Over controls
        return bar

    def build_project(self):
        p = self.proj_panel = Panel("Project")
        imp = QPushButton("Import")
        imp.setToolTip("Import media (Ctrl+I)")
        imp.clicked.connect(self.import_dialog)
        p.head_lay.addWidget(imp)
        clr = QPushButton("Clear Project")
        clr.setToolTip("Remove all clips and reset the project")
        clr.clicked.connect(self.clear_project)
        p.head_lay.addWidget(clr)
        self.grid_btn = QToolButton()
        self.grid_btn.setText("\u25A6")
        self.grid_btn.setCheckable(True)
        self.grid_btn.setToolTip("Toggle grid / list view")
        self.grid_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.grid_btn.clicked.connect(lambda on: self.set_project_grid(on))
        p.head_lay.addWidget(self.grid_btn)
        self.plist = ProjectList()
        self.plist.itemDoubleClicked.connect(
            lambda it: self.insert_media([self.by_path[it.data(Qt.ItemDataRole.UserRole)]]))
        self.plist.filesDropped.connect(self.import_paths)
        self.plist.customContextMenuRequested.connect(self.project_menu)
        p.body_lay.addWidget(self.plist)
        return p

    grid_view = False

    # [MAP] Sets the QListWidgetItem size hint for a Project row (grid: fixed-width card; list: full viewport width).
    # Must run after set_grid() and after the row is created. ProjectList.resizeEvent keeps list rows full width.
    def _size_item(self, m):
        row, it = self.rows[m], self.items[m]
        if self.grid_view:
            it.setSizeHint(QSize(row.width() if row.width() > 100 else 76, row.sizeHint().height() + 4))
        else:
            it.setSizeHint(QSize(self.plist.viewport().width(), max(54, row.sizeHint().height() + 8)))

    def set_project_grid(self, on):
        self.grid_view = bool(on)
        pl = self.plist
        if on:
            pl.setViewMode(QListWidget.ViewMode.IconMode)
            pl.setResizeMode(QListWidget.ResizeMode.Adjust)
            pl.setMovement(QListWidget.Movement.Static)
            pl.setWrapping(True)
            pl.setSpacing(6)
        else:
            pl.setViewMode(QListWidget.ViewMode.ListMode)
            pl.setWrapping(False)
            pl.setSpacing(0)
        for m in self.medias:
            self.rows[m].set_grid(on)
            self._size_item(m)
        pl.doItemsLayout()

    # [MAP] Video stage + control bar. Tools are an exclusive QButtonGroup: V select, C razor, X crop, R resize.
    # IMPORTANT: Snap and Precise are two checkboxes forced mutually exclusive by another QButtonGroup - exactly one is
    # always on. Precise is turned on last, so the editor STARTS in Precise (Snap off) when ffprobe exists.
    # The camera button grabs the current frame from the sink (untransformed source frame - crop/rotate are not applied).

    def build_video_column(self):
        wrap = QWidget()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        self.video = VideoView()
        self.video.setStyleSheet("background: #000000;")
        self.stage = VideoStage(self.video)
        self.stage.setMinimumHeight(160)
        self.stage.committed.connect(self.on_stage_commit)
        self.stage.dragStarted.connect(lambda: self.engine.pause())
        self.stage.resetRequested.connect(self.reset_xf)
        self.stage.okClicked.connect(self.exit_tool)
        self.stage.rotateRequested.connect(self.rotate_clip_90)
        self.stage.cancelClicked.connect(self.exit_tool)
        vl.addWidget(self.stage, 1)

        bar = QFrame()
        bar.setObjectName("controlbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 10, 6)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        self.b_sel = self.tool_btn("select", "Selection Tool (V)", lambda: self.set_tool("select"), True)
        self.b_razor = self.tool_btn("razor", "Razor / Cut Tool (C)", lambda: self.set_tool("razor"), True)
        self.b_crop = self.tool_btn("crop", "Crop Tool (X) - drag the corners / sides of the selection",
                                    lambda: self.set_tool("crop"), True)
        self.b_resize = self.tool_btn("resize", "Resize Tool (R) - drag the corners / sides to scale the picture; "
                                      "empty space becomes black", lambda: self.set_tool("resize"), True)
        self.b_sel.setChecked(True)
        for b in (self.b_sel, self.b_razor, self.b_crop, self.b_resize):
            grp.addButton(b)
            row.addWidget(b)
        self.plugin_stack = PluginToolStack()       # [52.0] hidden until a "tool" mod is loaded
        grp.addButton(self.plugin_stack)
        row.addWidget(self.plugin_stack)
        self.plugin_stack.hide()
        self.cam_btn = self.tool_btn("camera", "Screenshot current frame", self.take_screenshot)
        row.addWidget(self.cam_btn)
        row.addSpacing(6)
        # [51.9] ONE button whose TEXT is the current mode ("Precise" default / "Keyframes"); clicking switches mode.
        self.mode_btn = QPushButton("Precise")
        self.mode_btn.setEnabled(bool(FFPROBE))
        self.mode_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_btn.setToolTip("Click to switch trim mode.\n\n"
                                 "Precise: trim to any exact frame. Export re-encodes a clip only when its start is not on "
                                 "a keyframe; everything else stays an untouched, lossless copy.\n"
                                 "Keyframes: cut points snap to keyframes, so the preview matches a fully lossless export "
                                 "exactly.\n"
                                 + ("" if FFPROBE else "(ffprobe.exe not found - place it next to this program)"))
        self.mode_btn.clicked.connect(lambda: self.set_trim_mode(not self.seq.snap))
        if FFPROBE:
            self.seq.snap, self.seq.precise = False, True      # start in Precise
        row.addWidget(self.mode_btn)
        row.addStretch(1)
        self.tc_label = QLabel("00:00:00:00")
        self.tc_label.setObjectName("tc")
        row.addWidget(self.tc_label)
        row.addSpacing(10)
        self.play_btn = self.tool_btn("play", "Play / Pause (Space, hold while playing for 2x)", self.toggle_play)
        for b in (self.tool_btn("prev_edit", "Previous edit (Up)", lambda: self.goto_edit(-1)),
                  self.tool_btn("step_back", "Back 1 frame (Left)", lambda: self.step(-1)),
                  self.play_btn,
                  self.tool_btn("step_fwd", "Forward 1 frame (Right)", lambda: self.step(1)),
                  self.tool_btn("next_edit", "Next edit (Down)", lambda: self.goto_edit(1))):
            row.addWidget(b)
        row.addSpacing(10)
        self.total_label = QLabel("00:00:00:00")
        self.total_label.setObjectName("tcDim")
        row.addWidget(self.total_label)
        row.addStretch(1)
        vl.addWidget(bar)
        return wrap

    # [MAP] Timeline + scrollbar + zoom slider (log mapping pps = 2 * 400^(v/100) ... 2..800 px/s) + Fit. Wires every
    # Timeline signal to a MainWindow slot. The zoom slider passes animate=False (already continuous), Fit animates.
    def build_timeline_panel(self):
        p = self.tl_panel = Panel("Timeline")
        self.tl = Timeline(self.seq)
        self.seq.blocked.connect(lambda k: self.statusBar().showMessage(
            f"The {k} row is locked - click its lock icon (left of the row) to edit it.", 4000))     # [52.25]
        # [FEATURE] Simple audio VU meter to the left of the timeline, reacting to whatever audio the Engine is
        # currently decoding (Engine.audioLevel -> VUMeter.set_level). Preview/monitoring only - never exported.
        self.vu = VUMeter()
        self.vu.setFixedWidth(34)   # [FEATURE] 30% wider than the original 26px
        tl_row = QHBoxLayout()
        tl_row.setContentsMargins(0, 0, 0, 0)
        tl_row.setSpacing(4)
        tl_row.addWidget(self.vu)
        tl_row.addWidget(self.tl, 1)
        p.body_lay.addLayout(tl_row, 1)
        nav = QFrame()
        nav.setObjectName("nav")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(8, 3, 8, 3)
        bar = QScrollBar(Qt.Orientation.Horizontal)
        self.tl.bar = bar
        bar.valueChanged.connect(self.tl.on_scroll)
        nl.addWidget(bar, 1)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(0, 100)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.setValue(50)
        self.zoom_slider.valueChanged.connect(lambda v: self.tl.set_zoom(2 * 400 ** (v / 100), animate=False))
        self.tl.zoomChanged.connect(self.sync_zoom)
        self.tl.loopToggled.connect(lambda on: setattr(self.engine, "loop", on))
        fit = QPushButton("Fit")
        fit.setFixedWidth(46)
        fit.clicked.connect(lambda: self.tl.zoom_fit())
        nl.addWidget(QLabel("Zoom"))
        nl.addWidget(self.zoom_slider)
        nl.addWidget(fit)
        p.body_lay.addWidget(nav)
        self.tl.seekRequested.connect(self.on_timeline_seek)
        self.tl.splitRequested.connect(self.split_at)
        self.tl.blankRequested.connect(self.add_blank_clip)
        self.tl.deleteRequested.connect(self.delete_selected)
        self.tl.copyRequested.connect(self.copy_selected)
        self.tl.duplicateRequested.connect(self.duplicate_selected)
        self.tl.pasteRequested.connect(self.paste_clip)
        self.tl.clipOptionsRequested.connect(self.edit_clip_options)
        self.tl.filesDropped.connect(self.on_files_dropped)
        self.tl.trimBlocked.connect(
            lambda name: self.statusBar().showMessage(
                f"Still analyzing {name} for exact cut points - trimming its start will be available in a "
                f"moment (or switch to Precise to trim freely, at the cost of frame-exact export).", 7000))
        return p

    # [KNOWN ISSUE F4] These QActions carry the global shortcuts. "Save-Over" (Ctrl+S) is NOT gated by app_mode:
    # in GIF mode the Save-Over BUTTON is hidden (request_mode) but Ctrl+S still calls save_over(), which then
    # overwrites the primary file with a lossless VIDEO export. Two confirm dialogs still guard it, but it is a
    # mode leak. Fix: return early in save_over() when app_mode == "GIF" (or disable the action in request_mode).
    # [PITFALL] Shortcut keys are window-wide; new single-letter keys can collide with build_shortcuts below.
    def build_actions(self):
        """[52.0] Every binding is a QShortcut whose key comes from Preferences (utils.KEYBIND_DEFS ids). play_pause,
        tool_ok and tool_cancel have no QShortcut: keyPressEvent matches them via _key_is (tap/hold logic, tool-only)."""
        self._bind_fns = {
            "import": self.import_dialog, "save_over": self.save_over, "export": self.export,
            "select_all": self._select_all,
            "undo": self.seq.undo, "redo": self.seq.redo,
            "split": lambda: self.split_at(self.engine.playhead), "copy": self.copy_selected,
            "paste": self.paste_clip, "duplicate": self.duplicate_selected, "delete": self.handle_delete_key, "rename": self.f2_rename, "quit": self.close,
            "tool_select": lambda: (self.b_sel.setChecked(True), self.set_tool("select")),
            "tool_razor": lambda: (self.b_razor.setChecked(True), self.set_tool("razor")),
            "tool_crop": lambda: (self.b_crop.setChecked(True), self.set_tool("crop")),
            "tool_resize": self._r_shortcut,
            "tool_plugin": lambda: self.plugin_stack.cycle(),
            "move_left": lambda: self.move_clip(-1), "move_right": lambda: self.move_clip(1),
            "step_back": lambda: self.step(-1), "step_fwd": lambda: self.step(1),
            "step_back5": lambda: self.step(-5), "step_fwd5": lambda: self.step(5),
            "prev_edit": lambda: self.goto_edit(-1), "next_edit": lambda: self.goto_edit(1),
            "go_start": lambda: self._locked_seek(0.0), "go_end": lambda: self._locked_seek(self.seq.total())}
        self._shortcuts = {}
        for kid, fn in self._bind_fns.items():
            sc = QShortcut(QKeySequence(), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            self._shortcuts[kid] = sc
        self.apply_keybinds()

    def apply_keybinds(self):
        self._keys = {k: QKeySequence(v) for k, v in keybind_map().items()}
        for kid, sc in self._shortcuts.items():
            sc.setKey(self._keys[kid])

    def _key_is(self, kid, event):
        from PySide6.QtCore import QKeyCombination
        ks = self._keys.get(kid)
        if ks is None or ks.isEmpty():
            return False
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if QKeySequence(QKeyCombination(mods, Qt.Key(event.key()))) == ks:
            return True
        return event.key() == Qt.Key.Key_Enter and ks == QKeySequence(QKeyCombination(mods, Qt.Key.Key_Return))

    def apply_prefs(self):
        """Called by PreferencesDialog on Apply/OK/Reset: theme, keybinds, mods."""
        app = QApplication.instance()
        app.setPalette(dark_palette())
        app.setStyleSheet(build_qss())
        self.titlebar.restyle()
        self.apply_keybinds()
        return self.plugins.sync()          # names of mods that were unloaded (restart needed to fully drop their code)

    def export_default(self, src_path, ext, mode):
        """[52.0] Default path offered in the Save dialog (default export folder + naming scheme from Preferences)."""
        s = prefs()
        folder = os.path.dirname(src_path)
        d = s.value("pref_export_dir", "", str)
        if s.value("pref_export_dir_on", False, bool) and d and os.path.isdir(d):
            folder = d
        return format_out_name(name_scheme(), src_path, ext, folder, mode)

    def export_beside(self, src_path, ext, mode):
        """[52.4] Shift+click export: naming scheme, saved next to the source file (ignores the default export location);
        made unique with _2, _3... if taken."""
        s = prefs()
        base, e = os.path.splitext(format_out_name(name_scheme(), src_path, ext,
                                                   os.path.dirname(src_path), mode))
        out, n = base + e, 2
        while os.path.exists(out):
            out, n = f"{base}_{n}{e}", n + 1
        return out

    def export_auto(self, src_path, ext, mode):
        """[52.3] With "Use a default export location" on (and the folder existing): the path to export to WITHOUT asking
        (scheme name, made unique with _2, _3... so nothing is overwritten). None = ask with the Save dialog as before."""
        s = prefs()
        d = s.value("pref_export_dir", "", str)
        if not (s.value("pref_export_dir_on", False, bool) and d):
            return None
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
        base, e = os.path.splitext(self.export_default(src_path, ext, mode))
        out, n = base + e, 2
        while os.path.exists(out):
            out, n = f"{base}_{n}{e}", n + 1
        self.statusBar().showMessage("Exporting to default location: " + out, 6000)
        return out

    def show_help(self):
        QMessageBox.information(self, "Keyboard shortcuts", (
            "Space  Play / pause   (hold down while playing = 2x speed instead)\n"
            "Left / Right  Step one frame  (Shift = 5 frames)\n"
            "Up / Down  Previous / next edit\n"
            "Home / End  Go to start / end\n"
            "V  Selection tool     C  Razor / Cut tool\n"
            "Z  Plugin tool (press again to cycle plugin tools)\n"
            "X  Crop tool     R  Resize tool  (drag corners/sides; right-click the overlay to reset;\n"
            "   press R again while Resize is active to rotate 90\u00b0)\n"
            "Enter / Esc  While Crop/Resize is active: OK / Cancel\n"
            "Shift+C  Add edit (split) at playhead\n"
            "Ctrl+A  Select all timeline clips  (drag on empty timeline space = selection box, Ctrl = add)\n"
            "Ctrl+C / Ctrl+V  Copy / paste the selected timeline clip\n"
            "Ctrl+D  Duplicate the selected video / text / audio clip\n"
            "Delete  Remove selected Project item, or ripple-delete selected timeline clip\n"
            "F2  Rename the selected Project file (on disk)\n"
            "Ctrl+Z / Ctrl+Shift+Z  Undo / redo\n"
            "Ctrl+I  Import      Ctrl+M  Export      Ctrl+S  Save-Over\n\n"
            "Drag a clip's edge on the timeline to trim it, drag its body to reorder it.\n"
            "Ctrl+wheel to zoom, wheel to scroll, click the ruler to scrub.\n"
            "Drop files onto the Project panel or straight onto the timeline.\n\n"
            "The star on a Project item marks it as the PRIMARY file - click any other "
            "star to change it. Save-Over always overwrites the primary file.\n\n"
            "Keyframes keeps trims on keyframes so the preview always matches a fully lossless export.\n"
            "Precise lets you trim to any frame; export re-encodes only the small sliver that "
            "needs it, keeping the rest of every clip an untouched, lossless copy."))

    # ------------------------------------------------------------------ importing
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        self.import_paths(paths)
        e.acceptProposedAction()

    def import_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import media", "", VIDEO_FILTER)
        if paths:
            self.import_paths(paths)

    # [MAP] Import files into the Project (NOT the timeline). For each path: skip duplicates (returns the existing Media),
    # skip non-files, probe_media() (synchronous on the GUI thread, F12), create Media + QListWidgetItem + ProjectRow,
    # register in medias/by_path/items/rows, start the background MediaWorker, and make the first file primary.
    # Returns the list of Media (existing or new) - callers such as on_files_dropped insert exactly that list.
    # [COUPLING] A new per-media registry must be updated here AND in remove_medias, clear_project and rename_media.
    # [52.21] Wait for a worker-thread result WITHOUT freezing the window: paints/timers keep running, mouse and keyboard
    # events are excluded so a click during a long import can no longer re-enter the UI (that was the crash).
    def _await_fut(self, fut, label):
        if not fut.done():
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.statusBar().showMessage(f"Importing {label} ...")
            try:
                while not fut.done():
                    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 30)
                    time.sleep(0.01)
            finally:
                QApplication.restoreOverrideCursor()
                self.statusBar().clearMessage()
        try:
            return fut.result()
        except Exception:
            return None

    def import_paths(self, paths):
        out = []
        # [FIX] probe_media() runs synchronously on the GUI thread (up to 25 s/file, see [KNOWN ISSUE F12]).
        # Importing several files back-to-back used to freeze the window for the whole batch with no feedback.
        # Files are still probed one-by-one (unchanged) - this just shows progress and pumps the event loop
        # between files so the UI repaints/responds instead of looking hung, and lets the user cancel the rest.
        dlg = None
        if len(paths) > 1:
            dlg = QProgressDialog("Importing media...", "Cancel", 0, len(paths), self)
            dlg.setWindowTitle("Importing")
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumDuration(500)
            dlg.setAutoClose(True)
        # [PERF] Probe non-image files in parallel (4 ffmpeg -i at a time, no Qt objects touched); the loop below only
        # collects the results in order, so a big import takes ~1/4 of the time instead of N x latency.
        from concurrent.futures import ThreadPoolExecutor
        pool, futs = ThreadPoolExecutor(max_workers=4), {}
        for p in paths:
            ap = os.path.abspath(p)
            if ap not in self.by_path and ap not in futs and os.path.isfile(ap):
                is_i = os.path.splitext(ap)[1].lower() in IMAGE_EXTS
                futs[ap] = pool.submit(probe_image if is_i else probe_media, ap)    # [52.21] images bake off the GUI thread
        for i, p in enumerate(paths):
            if dlg is not None:
                dlg.setValue(i)
                dlg.setLabelText(f"Importing {os.path.basename(p)}  ({i + 1}/{len(paths)})")
                QApplication.processEvents()
                if dlg.wasCanceled():
                    break
            p = os.path.abspath(p)
            if p in self.by_path:
                out.append(self.by_path[p])
                continue
            if not os.path.isfile(p):
                continue
            is_img = os.path.splitext(p)[1].lower() in IMAGE_EXTS
            m = self._await_fut(futs[p], os.path.basename(p)) if p in futs else None
            if not m:
                self.statusBar().showMessage(f"Could not {'import image' if is_img else 'read'}: {os.path.basename(p)}", 6000)
                continue
            if is_img:
                m.name = os.path.basename(p)   # show the original picture's name in the Project tab
                m.is_image = True
                # [FIX B4] media.dur is the full IMAGE_MAX_DUR baked video so the timeline clip's edges can be
                # dragged freely to lengthen it; the clip itself still starts at the short default length.
                m.mark_out = min(IMAGE_CLIP_DUR, m.dur)
            self.medias.append(m)
            self.by_path[p] = m
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, p)
            row = ProjectRow(m, drag_path=p)
            row.starClicked.connect(self.set_primary)
            row.removeClicked.connect(lambda mm: self.request_remove_media(mm, confirm=True))
            self.plist.addItem(it)
            self.plist.setItemWidget(it, row)
            self.items[m] = it
            self.rows[m] = row
            row.set_grid(self.grid_view)
            self._size_item(m)
            self.worker.start(m)
            out.append(m)
        pool.shutdown(wait=False, cancel_futures=True)
        if dlg is not None:
            dlg.setValue(len(paths))
        if out and self.primary is None:
            self.set_primary(out[0])
        self.update_title()
        return out

    def on_media_ready(self, m):
        row = self.rows.get(m)
        if row and m.thumb_path and os.path.isfile(m.thumb_path):
            row.set_thumb(m.thumb_path)

    def on_perf_changed(self, bad):
        self.warnings.set("perf", "⚠️ GPU/CPU can't keep up" if bad else None,
                          "The preview is lagging or dropping frames. Close other heavy programs, "
                          "or scrub more slowly.")

    # [MAP] Polled every 3 s. RAM warning when used >= 90 % (clears at 85 %) or < 1 GB free; disk warning when free
    # space on the primary file's drive (or temp) is < 2 GB (clears at 2.5 GB). The disk check matters because export
    # and Save-Over write temporary files NEXT TO the output. Hysteresis prevents flicker.
    def check_system(self):
        """Poll RAM and free disk space; show/clear their warnings (with hysteresis)."""
        mem = system_memory()
        if mem:
            used, total = mem
            limit = 0.85 if self.warnings.has("ram") else 0.90
            if total and (used / total >= limit or total - used < 1024):
                self.warnings.set("ram", f"⚠️ Insufficient RAM {used}MB/{total}MB",
                                  "System memory is nearly full - preview may stutter. Close other programs.")
            else:
                self.warnings.set("ram", None)
        try:
            base = os.path.dirname(self.primary.path) if self.primary else tempfile.gettempdir()
            free = shutil.disk_usage(base).free / 2**30
        except Exception:
            free = None
        if free is not None:
            limit = 2.5 if self.warnings.has("disk") else 2.0
            if free < limit:
                self.warnings.set("disk", f"⚠️ Low disk space {free:.1f}GB free",
                                  "Export and Save-Over write temporary files next to the output - "
                                  "free some space before exporting.")
            else:
                self.warnings.set("disk", None)

    def on_media_progress(self, m, p):
        row = self.rows.get(m)
        if row:
            row.set_load_progress(p)

    # [MAP] Restart background loading for a media whose load was cancelled to free its file (rename/Save-Over failed).
    # Only restarts when keyframes are still missing and ffprobe exists.
    def _resume_load(self, m):
        """Restart background loading for a media whose load we cancelled to free its file."""
        if m in self.rows and m.has_video and m.keyframes is None and FFPROBE:
            self.worker.start(m)

    def set_primary(self, m):
        self.primary = m
        for mm, row in self.rows.items():
            row.set_primary(mm is m)
        self.update_title()

    def update_title(self):
        if self.primary is not None:
            self.setWindowTitle(f"{self.primary.name} - {self.APP_TITLE}")
        else:
            self.setWindowTitle(self.APP_TITLE)

    # [MAP] Right-click menu of the Project list. All items are disabled when the click missed a row.
    def project_menu(self, pos):
        it = self.plist.itemAt(pos)
        sel = self.plist.selectedItems()
        # [FIX] "Remove from Project" only ever removed the single row that was right-clicked, even when
        # several rows were selected (Delete key already removed the whole selection - see
        # handle_delete_key). Match that: if the click landed on a row that's part of a multi-selection,
        # the action targets the whole selection, same as most file managers.
        multi = it is not None and it in sel and len(sel) > 1
        menu = QMenu(self)
        a_ins = menu.addAction("Insert into Timeline at Playhead")
        a_star = menu.addAction("Set as Primary (Save-Over target)")
        a_ren = menu.addAction("Rename File...\tF2")
        menu.addSeparator()
        a_open = menu.addAction("Open File")
        a_loc = menu.addAction("Open File Location")
        menu.addSeparator()
        a_rm = menu.addAction(f"Remove {len(sel)} Files from Project" if multi else "Remove from Project")
        for a in (a_ins, a_star, a_ren, a_open, a_loc, a_rm):
            a.setEnabled(it is not None)
        act = menu.exec(self.plist.viewport().mapToGlobal(pos))
        if not it:
            return
        m = self.by_path[it.data(Qt.ItemDataRole.UserRole)]
        if act == a_ins:
            self.insert_media([m])
        elif act == a_star:
            self.set_primary(m)
        elif act == a_ren:
            self.rename_media(m)
        elif act == a_open:
            self.open_media_file(m, reveal=False)
        elif act == a_loc:
            self.open_media_file(m, reveal=True)
        elif act == a_rm:
            ms = [self.by_path[i.data(Qt.ItemDataRole.UserRole)] for i in sel] if multi else [m]
            self.remove_medias(ms, confirm=False)   # explicit menu action - no extra confirm

    def open_media_file(self, m, reveal):
        if not os.path.isfile(m.path):
            self.statusBar().showMessage("File no longer exists on disk.", 4000)
            return
        try:
            if reveal and sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(m.path)])
            elif reveal and sys.platform == "darwin":
                subprocess.Popen(["open", "-R", m.path])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(m.path) if reveal else m.path))
        except Exception as ex:  # noqa: BLE001
            self.statusBar().showMessage(f"Could not open: {ex}", 5000)

    def request_remove_media(self, m, confirm=True):
        self.remove_medias([m], confirm)

    # [INVARIANT] THE single removal path (row X button, context menu, Delete key). If any removed media is used on the
    # timeline it asks first, deletes those clips, and CLEARS both undo stacks (old snapshots would reference removed
    # media). Then for each media: worker.cancel + proxy.cancel_media + drop thumbnails + remove list item and all
    # registry entries. If the primary was removed, the first remaining file becomes primary (or None).
    # [COUPLING] Keep the worker.cancel / proxy.cancel_media pair (file-lock rule).
    def remove_medias(self, ms, confirm=True):
        ms = [m for m in ms if m in self.rows]
        if not ms:
            return
        used = [sg for sg in self.seq.segs if sg.media in ms]
        if confirm or used:
            what = f"'{ms[0].name}'" if len(ms) == 1 else f"{len(ms)} files"
            if used:
                msg = (f"Remove {what} from the project?\n\nIt is used by {len(used)} clip(s) on the "
                       f"timeline - those clips will be removed from the timeline too, and undo history "
                       f"will be cleared.\n\n(Files on disk are not touched.)")
            else:
                msg = (f"Remove {what} from the project?\n\n"
                       f"(This only removes it from SQVCE n4.0's list - the file on disk is not touched.)")
            r = QMessageBox.question(
                self, "Remove from Project", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        if used:
            self.engine.pause()
            self.seq.segs = [sg for sg in self.seq.segs if sg.media not in ms]
            self.seq.undo_stack.clear()       # old snapshots would point at removed media
            self.seq.redo_stack.clear()
            self.tl.sel = -1
            self.seq.edited.emit()            # preview re-seeks / clears
        for m in ms:
            self.worker.cancel(m)
            self.proxy.cancel_media(m.path)
            self.tl.clear_thumbs_for(m.path)
            self.plist.takeItem(self.plist.row(self.items[m]))
            self.medias.remove(m)
            for k in [k for k, v in self.by_path.items() if v is m]:
                del self.by_path[k]
            del self.items[m]
            del self.rows[m]
            if self.primary is m:
                self.primary = None
        if self.primary is None:
            self.set_primary(self.medias[0] if self.medias else None)
        else:
            self.update_title()

    # [DO NOT BREAK] Renames the file ON DISK. Order is essential: (1) if the Engine has this file open, pause and
    # release it (stop + setSource(QUrl())); (2) worker.cancel(m, wait=True) so ffprobe/thumbnail let go;
    # (3) proxy.cancel_media; (4) _replace_with_retry(old, new); on failure -> _resume_load and an error box;
    # (5) update m.path/m.name, by_path, the item's UserRole data, thumbnail caches, row text; (6) restart loading and
    # re-seek the Engine. Because Seg refers to the Media OBJECT (not the path) timeline clips survive the rename.
    def rename_media(self, m):
        old_path = m.path
        dirn, base = os.path.split(old_path)
        stem, ext = os.path.splitext(base)
        new_stem, ok = QInputDialog.getText(self, "Rename File", "New file name:", text=stem)
        if not ok:
            return
        new_stem = new_stem.strip().replace("/", "").replace("\\", "")
        if not new_stem or new_stem == stem:
            return
        new_path = os.path.join(dirn, new_stem + ext)
        if os.path.exists(new_path):
            QMessageBox.critical(self, "Rename File", f"A file named '{os.path.basename(new_path)}' "
                                                       f"already exists.")
            return
        if self.engine.path == old_path:      # release our own lock on it before renaming
            self.engine.pause()
            self.engine.player.stop()
            self.engine.player.setSource(QUrl())
            self.engine.path = None
        self.worker.cancel(m, wait=True)      # background loader must let go of the file too
        self.proxy.cancel_media(m.path)
        ok2, err2 = _replace_with_retry(old_path, new_path)
        if not ok2:
            self._resume_load(m)
            QMessageBox.critical(self, "Rename Failed", f"Could not rename the file:\n{err2}")
            return
        for k in [k for k, v in self.by_path.items() if v is m]:
            del self.by_path[k]
        m.path = new_path
        m.name = os.path.basename(new_path)
        self.by_path[new_path] = m
        self.items[m].setData(Qt.ItemDataRole.UserRole, new_path)
        self.tl.clear_thumbs_for(old_path)
        row = self.rows.get(m)
        if row:
            row.drag_path = new_path
            row.set_info(m)
        self.update_title()
        self.statusBar().showMessage(f"Renamed to: {m.name}", 5000)
        self._resume_load(m)
        self.engine.seek(self.engine.playhead, play=False)

    # [MAP] Delete key router: if the Project list has focus AND a selection -> remove those media (with confirm);
    # otherwise ripple-delete the selected timeline clip. Timeline uses ClickFocus so clicking it moves focus away from
    # the list.
    def handle_delete_key(self):
        lane_done = [getattr(l, "delete_key", lambda: False)() for l in self.tl.unlocked_lanes()]   # [52.10] selected lane items (text)
        if any(lane_done) and not (0 <= self.tl.sel < len(self.seq.segs)):
            return
        if self.plist.hasFocus():
            sel = self.plist.selectedItems()
            if sel:
                self.remove_medias([self.by_path[i.data(Qt.ItemDataRole.UserRole)] for i in sel],
                                   confirm=True)
                return
        self.delete_selected()

    def f2_rename(self):
        if self.plist.hasFocus():
            it = self.plist.currentItem()
            if it is not None:
                self.rename_media(self.by_path[it.data(Qt.ItemDataRole.UserRole)])
        elif self.tl.hasFocus():
            self.tl.rename_group()

    # [MAP] Full reset after confirmation: pause and release the player, cancel worker + proxies for every media,
    # clear list/registries/clipboard/segs/undo/redo/thumb caches, emit edited (preview goes blank).
    def clear_project(self):
        if not self.medias and not self.seq.segs:
            return
        shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)   # [52.15] Shift = no warning
        r = QMessageBox.StandardButton.Yes if shift else QMessageBox.question(
            self, "Clear Project", "Remove all clips and clear the timeline?\n\n"
            "Files on disk are not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        self.engine.pause()
        self.engine.player.stop()
        self.engine.player.setSource(QUrl())
        self.engine.path = None
        for mm in self.medias:
            self.worker.cancel(mm)
            self.proxy.cancel_media(mm.path)
        self.plist.clear()
        self.medias.clear()
        self.by_path.clear()
        self.items.clear()
        self.rows.clear()
        self.primary = None
        self.clipboard_seg = None
        self.seq.segs = []
        self.seq.undo_stack.clear()
        self.seq.redo_stack.clear()
        self.atrack.clear()                         # [52.19]
        self.plugins.broadcast("on_clear")          # [52.11] mods drop their own clips/overlays
        self.tl.sel = -1
        self.tl.thumb_pix.clear()
        self.tl.thumb_pending.clear()
        self.seq.edited.emit()
        self.update_title()
        self.statusBar().showMessage("Project cleared.", 4000)

    # ------------------------------------------------------------------ playback / timeline
    # [FIX] Crop/Resize edit a single frame - the playhead must not move and playback must not resume
    # while one of them is active (self.stage.tool is not None), or the frame being cropped/resized would
    # change out from under the user. Every user-facing way to move the playhead or (un)pause funnels
    # through here or through Timeline's own ruler-scrub guard (see Timeline.mousePressEvent/mouseMoveEvent).
    def _tool_locked(self):
        if self.stage.tool is not None:
            self.statusBar().showMessage("Finish or cancel Crop/Resize first (double-click OK/Cancel).", 3000)
            return True
        return False

    def _locked_seek(self, t):
        if self._tool_locked():
            return
        self.engine.seek(t, play=False)

    def toggle_play(self):
        if self._tool_locked():
            return
        self.engine.pause() if self.engine.playing else self.engine.play()

    # [FEATURE] Space is handled here (not as a QShortcut) so a quick tap can be told apart from a hold:
    #   - tap while paused    -> start playing (unchanged behaviour)
    #   - tap while playing   -> pause (unchanged behaviour)
    #   - HOLD while playing  -> after SPACE_HOLD_MS, switch to 2x speed for as long as it's held, then
    #                            back to normal speed (still playing) on release - instead of pause/unpause.
    # Enter/Esc are also handled here, but only while Crop/Resize is active: Enter = OK, Esc = Cancel
    # (same as clicking the tool's own OK/Cancel buttons, which VideoStage.committed/cancelClicked wire to
    # on_stage_commit/exit_tool - see build_video_column). Autorepeat key-press events are ignored so
    # holding a key doesn't retrigger anything.
    def keyPressEvent(self, event):
        key = event.key()
        if self.stage.tool is not None and not event.isAutoRepeat():
            if self._key_is("tool_ok", event):
                self.stage.b_ok.click()
                event.accept()
                return
            if self._key_is("tool_cancel", event):
                self.stage.b_cancel.click()
                event.accept()
                return
        if self._key_is("play_pause", event):
            if event.isAutoRepeat():
                event.accept()
                return
            if not self._tool_locked():
                self._space_key = key
                self._space_is_down = True
                self._space_was_playing = self.engine.playing
                self._space_boosted = False
                if self._space_was_playing:
                    QTimer.singleShot(self.SPACE_HOLD_MS, self._space_hold_check)
                else:
                    self.toggle_play()          # tap while paused: start playing right away
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if key == getattr(self, "_space_key", None) and self._space_is_down:
            if not event.isAutoRepeat():
                was_down, self._space_is_down = self._space_is_down, False
                if was_down and self._space_boosted:
                    self.engine.set_speed_boost(1.0)
                    self._space_boosted = False
                elif was_down and self._space_was_playing:
                    self.toggle_play()          # short tap while playing: pause (unchanged behaviour)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _space_hold_check(self):
        """SPACE_HOLD_MS after Space went down: if it's still held and still playing, this is a
        hold, not a tap - switch to 2x speed instead of ever toggling play/pause for it."""
        if self._space_is_down and self.engine.playing and not self._tool_locked():
            self._space_boosted = True
            self.engine.set_speed_boost(2.0)

    def step(self, n):
        if self._tool_locked():
            return
        self.engine.seek(self.engine.playhead + n / self.seq.fps(), play=False)

    def goto_edit(self, d):
        if self._tool_locked():
            return
        if len(self.tl.sel_layers()) > 1:             # [52.21] clips from several layers selected: Up/Down does nothing
            self.statusBar().showMessage("Up/Down needs the selected clips to be on one layer.", 3000)
            return
        for ln in self.tl._all_lanes():               # [52.17] a selected lane clip (text...) navigates within ITS layer
            f = getattr(ln, "nav_edit", None)
            t = f(d) if f else None
            if t is not None:
                self.engine.seek(t, play=False)
                return
        bounds = self.seq.starts() + [self.seq.total()]
        t = self.engine.playhead
        if d < 0:
            c = [b for b in bounds if b < t - 0.02]
            tt = max(c) if c else 0.0
        else:
            c = [b for b in bounds if b > t + 0.02]
            tt = min(c) if c else self.seq.total()
        self.engine.seek(tt, play=False)
        # [52.6] also select the clip that starts at that edit (the last clip when landing on the very end), one at a time
        n = len(self.seq.segs)
        if n:
            self.tl.select_only(min(bisect.bisect_left(self.seq.starts(), tt - 0.001), n - 1))

    # [52.6] Ctrl+A: timeline clips when the timeline has focus / the mouse is over it; otherwise behaves like a normal
    # "select all" for a focused list (e.g. the Project panel).
    def _select_all(self):
        fw = QApplication.focusWidget()
        if self.tl.underMouse() or fw is self.tl or not isinstance(fw, QListWidget):
            self.tl.select_all()
        else:
            fw.selectAll()

    def on_timeline_seek(self, t):
        if self._tool_locked():
            return
        self.engine.scrub(t)

    # [MAP] Engine.playheadChanged handler - runs up to ~66x per second during playback, so keep it cheap:
    # refresh_stage (cheap unless something changed), timeline follow, timecode label.
    # [STALE] The QTimer.singleShot(90, capture_still) is a no-op today (capture_still returns immediately).
    def on_playhead(self, t):
        self.refresh_stage()
        if self.stage.tool is not None:
            QTimer.singleShot(90, self.capture_still)
        self.tl.set_playhead(t, follow=self.engine.playing)
        self.tc_label.setText(fmt_tc(t, self.seq.fps()))

    # [MAP] Refreshes total/current timecode and the size/time estimate. Called at start-up (before the Engine exists,
    # hence the hasattr guard) and from on_seq_edited.
    def update_labels(self):
        self.total_label.setText(fmt_tc(self.seq.total(), self.seq.fps()))
        self.tc_label.setText(fmt_tc(self.engine.playhead if hasattr(self, "engine") else 0, self.seq.fps()))
        self.update_est()

    def _out_wh(self, segs):
        """First seg's effective (post crop/rotate) output width/height, for GIF/Transcode estimates."""
        s0 = segs[0]
        cw, ch = (s0.xf[0], s0.xf[1]) if s0.xf else (s0.media.w, s0.media.h)
        if s0.rot % 180.0 > 45:
            cw, ch = ch, cw
        return cw, ch

    # [MAP] Rough export SIZE estimate (heuristics only - tune the constants if users report bad accuracy):
    #   GIF ............ width*height*seconds*fps*bpp with bpp = 0.22*sqrt(colors/256)/(1+lossy/60)
    #   Transcode ...... bitrate mode: kbps*seconds; CQ mode: pixels*fps*0.08*2^((23-crf)/6) (halves every +6 CRF)
    #   lossless ....... proportional share of each source file's size (cached per path in self._sizes)
    # [KNOWN ISSUE F16] self._sizes is created lazily in update_est (hasattr pattern). est_bytes/est_secs raise
    # AttributeError if called before the first update_est. Initialise it in __init__ when refactoring.
    def est_bytes(self):
        segs = self.seq.segs
        if not segs:
            return 0
        if self.is_gif():
            p = self.gif_combo.currentData()
            if not p:
                return 0
            cw, ch = self._out_wh(segs)
            if cw <= 0 or ch <= 0:
                return 0
            w = min(p["w"], cw)
            h = w * ch / cw
            bpp = 0.22 * (p["colors"] / 256.0) ** 0.5 / (1.0 + p["lossy"] / 60.0)     # empirical bytes/pixel/frame
            return w * h * self.seq.total() * p["fps"] * bpp
        if self.hb_check.isChecked():
            p = self.hb_combo.currentData()
            if not p:
                return 0
            cw, ch = self._out_wh(segs)
            if cw <= 0 or ch <= 0:
                return 0
            h = min(p["h"], ch) if p.get("h") else ch
            w = h * cw / ch
            fps = p.get("fps") or (segs[0].media.fps or 30.0)
            dur = self.seq.total()
            audio_kbps = 128.0 if p.get("format") == "webm" else 192.0
            if p["mode"] == "bitrate":
                video_kbps = float(p["value"])
            else:                                            # constant quality: rough bits/pixel-frame model
                bpp = 0.08 * 2 ** ((23 - float(p["value"])) / 6.0)
                video_kbps = w * h * fps * bpp / 1000.0
            return (video_kbps + audio_kbps) * 1000.0 / 8.0 * dur
        tot = 0.0
        for sg in segs:
            m = sg.media
            sz = self._sizes.get(m.path)
            if sz is None:
                try:
                    sz = self._sizes[m.path] = os.path.getsize(m.path)
                except OSError:
                    sz = self._sizes[m.path] = 0
            tot += sz / max(m.dur, 0.001) * sg.src_dur
        return tot

    def update_est(self):
        if not hasattr(self, "est_label"):
            return
        if not hasattr(self, "_sizes"):
            self._sizes = {}
        b = self.est_bytes()
        if b <= 0:
            self.est_label.setText("")
            return
        sz = (f"{b / 2 ** 30:.1f}GB" if b >= 2 ** 30 else f"{b / 2 ** 20:.0f}MB" if b >= 2 ** 20
              else f"{max(1, b / 1024):.0f}KB")
        t = int(round(self.est_secs()))
        tm = f"{max(1, t)}s" if t < 60 else f"{t // 60}m {t % 60:02d}s"
        self.est_label.setText(f"est. {sz} \u00b7 ~{tm}")
        self.est_label.setToolTip("Rough estimate of the exported file size and encoding time")

    # [MAP] Very rough encode-TIME estimate: copy cuts ~250 MB/s + fixed cost; re-encoded clips ~120 Mpx*fps/s (x264
    # veryfast); GIF adds palette/dither cost (x1.5 with gifsicle); Transcode adds pixels*frames / encoder throughput
    # (nvenc 260e6, x264 45e6, vp9 9e6). Constants are guesses.
    def est_secs(self):
        """Very rough encode-time estimate (copy cuts are fast, re-encodes scale with pixels x frames)."""
        segs = self.seq.segs
        gif = self.is_gif()
        transcode = not gif and self.hb_check.isChecked()
        t = 1.0
        for sg in segs:
            m = sg.media
            if sg.xf or sg.opts:
                t += sg.src_dur * max(m.w * m.h, 1) * max(m.fps, 1) / 120e6         # x264 veryfast
            else:
                sz = self._sizes.get(m.path) or 0
                t += sz / max(m.dur, 0.001) * sg.src_dur / 250e6 + 0.4 + (1.0 if (self.seq.precise or gif) else 0)
        if gif and segs:
            p = self.gif_combo.currentData()
            cw, ch = self._out_wh(segs)
            if p and cw > 0 and ch > 0:
                w = min(p["w"], cw)
                t += self.seq.total() * p["fps"] * w * (w * ch / cw) / 20e6          # palette + dither
                t += sum(sg.src_dur * max(sg.media.w * sg.media.h, 1) * max(sg.media.fps, 1) for sg in segs) / 300e6
                if p["lossy"] > 0 and find_tool("gifsicle"):
                    t *= 1.5
        elif transcode and segs:
            p = self.hb_combo.currentData()
            cw, ch = self._out_wh(segs)
            if p and cw > 0 and ch > 0:
                h = min(p["h"], ch) if p.get("h") else ch
                w = h * cw / ch
                fps = p.get("fps") or (segs[0].media.fps or 30.0)
                # rough pixels x frames / sec throughput per encoder: nvenc (hw) fastest, x264
                # 'medium' preset next, vp9 single-pass slowest by a wide margin
                div = {"x264": 45e6, "nvenc": 260e6, "vp9": 9e6}.get(p["encoder"], 45e6)
                t += w * h * fps * self.seq.total() / div
        return t

    # [MAP] After EVERY committed edit: request a reverse proxy for each eligible clip (ReverseProxy.request is
    # idempotent), prune proxies nobody needs (except the file the player has open), and show a status hint while any
    # are pending.
    def ensure_proxies(self):
        segs = self.seq.segs
        for sg in segs:
            self.proxy.request(sg)
        self.proxy.prune({ReverseProxy.key(sg) for sg in segs if sg.rev}, self.engine.path)
        if any(self.proxy.pending(sg) for sg in segs):
            self.statusBar().showMessage("Rendering reverse preview... (plays forward until ready)", 6000)

    # [MAP] Proxy finished in the background. Ignored while scrubbing. If the playhead is inside a clip whose proxy just
    # became available and the player is not yet on it, re-seek so the preview switches from forward to reversed.
    def on_proxy_ready(self):
        e = self.engine
        if not self.seq.segs or e._scrub_active:
            return
        idx, _ = self.seq.locate(e.playhead)
        sg = self.seq.segs[idx]
        px = e.proxy_path(sg)
        if px and e.path != px:                      # playhead sits in a clip whose reverse preview just finished
            e.seek(min(e.playhead, self.seq.total()))
        self.statusBar().showMessage("Reverse preview ready.", 3000)

    # [INVARIANT] THE reaction to every committed timeline change (edit, undo, redo, drag release): proxies -> stage ->
    # Engine.seek(min(playhead,total), play=False) -> labels -> repaint. Playback is stopped by every edit (play=False).
    # Add "something must refresh after an edit" logic here, not at the call sites.
    def on_seq_edited(self):
        self.ensure_proxies()
        self.refresh_stage()
        self.engine.seek(min(self.engine.playhead, self.seq.total()), play=False)
        self.update_labels()
        self.tl.update()

    def sync_zoom(self, pps):
        v = int(round(100 * math.log(max(pps, 2) / 2) / math.log(400)))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(max(0, min(100, v)))
        self.zoom_slider.blockSignals(False)

    # [MAP] Tool switch (select/razor/crop/resize). Crop/Resize pause the Engine, hand the tool to VideoStage and, for
    # Crop, show a one-time hint when the clip already has a cropped-away "ghost" area. Non-video clips show a status
    # message but the tool still opens (the stage stays inactive: refresh_stage passes xf=None).
    def set_tool(self, tool):
        if getattr(self, "plugins", None):
            self.plugins.deactivate()
        self.tl.set_tool(tool)
        if tool in ("crop", "resize"):
            seg = self.current_seg()
            if seg is not None and not seg.media.w:
                self.statusBar().showMessage("Crop / resize only works on video clips.", 4000)
        if tool in ("crop", "resize"):
            self.engine.pause()
            self.capture_still()
        self.stage.set_tool(tool if tool in ("crop", "resize") else None)
        self.refresh_stage()
        if tool == "crop" and self.stage.picture_rect().united(self.stage.clip_rect()) != QRectF(self.stage.clip_rect()):
            self.statusBar().showMessage(
                "This clip was cropped before - drag the selection past the frame edge (dashed ghost) "
                "to bring back what was cropped out.", 6000)
        if tool in ("crop", "resize"):
            QTimer.singleShot(120, self.capture_still)

    # [STALE][KNOWN ISSUE F14] DEAD CODE. The method returns on its first line (the live transformable VideoView made
    #   the frozen-frame
    # approach unnecessary). Everything after `return` is unreachable and calls VideoStage.set_still, which does not
    # exist. Callers (set_tool, on_playhead) still schedule it - harmless. Do NOT "re-enable" it by deleting the
    # return; delete the whole method and its three call sites together, or leave it alone.
    def capture_still(self):
        return                                  # live (transformable) view is always shown now
        if self.tl.tool not in ("crop", "resize"):
            return
        try:
            frame = self.video.videoSink().videoFrame()
            if frame.isValid():
                self.stage.set_still(frame.toImage())
        except Exception:
            pass

    # [COUPLING] Applies ClipOptionsDialog results to the Seg IN PLACE inside Sequence.edit (undo works because
    # snapshot() copied the old values first). Returns silently if nothing changed. Shows a status hint depending on
    # whether the reverse preview is available (> 30 s clips preview forward).
    def edit_clip_options(self, idx):
        if not (0 <= idx < len(self.seq.segs)):
            return
        if "video" in self.seq.locked:                  # [52.25]
            self.seq.blocked.emit("video")
            return
        seg = self.seq.segs[idx]
        if getattr(seg.media, "blank", False):         # [52.17] blank clip: the only option is its colour
            self.recolor_blank(seg)
            return
        dlg = ClipOptionsDialog(self, seg.media.name, seg.mute, seg.speed, bool(seg.media.acodec.strip()),
                                seg.mirror, seg.media.has_video, seg.rev,
                                seg.media.audio_streams, seg.atracks, seg.vol_db, seg.track_type)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        mute, speed, mirror, rev, atracks, vol_db, track_type = dlg.values()
        if (mute == seg.mute and abs(speed - seg.speed) < 1e-9 and mirror == seg.mirror and rev == seg.rev
                and atracks == seg.atracks and abs(vol_db - seg.vol_db) < 1e-9 and track_type == seg.track_type):
            return
        # Grouped clips are edited as one unit: applying options to any member applies to all of them.
        group_segs = [s for s in self.seq.segs if seg.grp is not None and s.grp == seg.grp] or [seg]

        def do():
            for s in group_segs:
                s.mute, s.speed, s.mirror, s.rev = mute, speed, mirror, rev
                s.atracks, s.vol_db, s.track_type = atracks, vol_db, track_type
            return True
        self.seq.edit(do)
        if rev and seg.src_dur > ReverseProxy.MAX_SEC:
            self.statusBar().showMessage("Reverse applied - live preview only works up to 30 s per clip; this clip "
                                         "plays forward in the preview but is reversed on export.", 7000)
        else:
            self.statusBar().showMessage("Clip options applied - clips with speed / mute / mirror / reverse are "
                                         "re-encoded on export.", 5000)

    # [MAP] Alt+Left/Right [52.21]: moves the whole selection ONE spot, as ONE undo step. Same layer: video clips swap past
    # the neighbouring clip(s); text/audio items hop over the neighbouring item of their layer (hop_sel). Several layers
    # (box select): the VIDEO selection is the baseline - it hops on the video row and every selected text/audio item is
    # shifted by the same time delta (shift_sel; refused if it would overlap or leave 0). No video clip selected + several
    # layers = nothing happens.
    def move_clip(self, d):
        tl, segs = self.tl, self.seq.segs
        tl.expand_groups()                          # [52.22] never move part of a group (it would break it apart)
        layers = tl.sel_layers()
        if not layers:
            return
        vsel = {i for i in tl.multi_sel if 0 <= i < len(segs)} | ({tl.sel} if 0 <= tl.sel < len(segs) else set())
        lanes = [ln for ln in tl.unlocked_lanes() if getattr(ln, "sel_layers", None) and ln.sel_layers()]   # [52.25]
        if not vsel:
            if len(layers) > 1 or not lanes:
                self.statusBar().showMessage("Alt+Left/Right needs the selection to be on one layer (or include a video clip).", 4000)
                return
            self.engine.pause()
            self.seq.edit(lambda: bool(lanes[0].hop_sel(d)), layer=None)      # [52.25] lane items only
            tl.update()
            return
        n, flags, ns = len(segs), [i in vsel for i in range(len(segs))], list(segs)
        if (d < 0 and min(vsel) == 0) or (d > 0 and max(vsel) == n - 1):
            return
        for i in (range(1, n) if d < 0 else range(n - 2, -1, -1)):
            a, b = (i - 1, i) if d < 0 else (i, i + 1)
            if flags[b if d < 0 else a] and not flags[a if d < 0 else b]:
                ns[a], ns[b], flags[a], flags[b] = ns[b], ns[a], flags[b], flags[a]
        new = [i for i, f in enumerate(flags) if f]
        dt = sum(s.dur for s in ns[:new[0]]) - self.seq.starts()[min(vsel)]
        if lanes and not all(ln.shift_sel(dt, True) for ln in lanes):
            self.statusBar().showMessage("Can't move the group there - a text/audio clip would overlap another one.", 4000)
            return
        self.engine.pause()

        def do():
            segs[:] = ns
            getattr(self.seq, "_invalidate_geometry", lambda: None)()
            for ln in lanes:
                ln.shift_sel(dt)
            return True
        self.seq.edit(do)
        tl.multi_sel, tl.sel = set(new), new[0]
        self.engine.seek(self.seq.starts()[new[0]], play=False)
        tl.update()

    # [MAP] Adds 90 degrees clockwise to the clip under the PLAYHEAD (current_seg), through Sequence.edit. Shown live in
    # the preview; exported by _fx_args (transpose). Reached from the Resize tool's rotate button (rotateRequested).
    def rotate_clip_90(self):
        seg = self.current_seg()
        if seg is None or not seg.media.has_video:
            return

        def do():
            seg.rot = (seg.rot + 90.0) % 360.0
            return True
        self.seq.edit(do)
        self.statusBar().showMessage(f"Clip rotated to {seg.rot:g}\u00b0 (shown live in the preview).", 5000)

    # [FEATURE] The "R" shortcut: enters the Resize tool as before, but if Resize is ALREADY the active
    # tool, pressing R again rotates the clip 90\u00b0 instead (same action as the tool's own rotate button /
    # rotateRequested) - a quick way to spin through orientations without reaching for the mouse.
    def _r_shortcut(self):
        if self.stage.tool == "resize":
            self.rotate_clip_90()
        else:
            self.b_resize.setChecked(True)
            self.set_tool("resize")

    app_mode = "Video"
    adv_kind = None          # [52.33] None = normal Video/GIF modes decide; "Video"/"GIF" = Advanced's export toggle decides

    # [MAP 52.33] ONE rule for "is this a GIF export?": Advanced's toggle wins while it is shown, else the Video/GIF tab.
    # est_bytes / est_secs / export / _sync_export_bar all use it - never test app_mode == "GIF" directly.
    def is_gif(self):
        return (self.adv_kind or self.app_mode) == "GIF"

    def adv_kind_pref(self):
        k = QSettings("QuickCut", "QuickCut").value("adv_kind", "Video", str)
        return k if k in ("Video", "GIF") else "Video"

    # [MAP 52.33] The ONLY place that decides which preset controls are visible: GIF combo+cog (GIF), Transcode checkbox +
    # its combo+cog (not GIF), Save-Over button (not GIF). Called by request_mode, _on_hb_toggled, set_export_kind and
    # set_export_kind_toggle (Advanced mod via ModAPI.set_export_toggle).
    def _sync_export_bar(self):
        gif = self.is_gif()
        self.gif_combo.setVisible(gif)
        self.gif_cog.setVisible(gif)
        self.saveover_btn.setVisible(not gif)
        self.hb_check.setVisible(not gif)
        show = self.hb_check.isChecked() and not gif
        self.hb_combo.setVisible(show)
        self.hb_cog.setVisible(show)

    def set_export_kind(self, kind):
        if kind not in ("Video", "GIF"):
            return
        self.kind_btn.setText(kind)
        QSettings("QuickCut", "QuickCut").setValue("adv_kind", kind)
        if self.adv_kind is not None:
            self.adv_kind = kind
            self._sync_export_bar()
            self.update_est()

    def set_export_kind_toggle(self, on):
        """Advanced mod: show/hide the Video|GIF toggle next to Export. Off -> the Video/GIF tab rules again."""
        self.adv_kind = self.kind_btn.text() if on else None
        self.kind_btn.setVisible(bool(on))
        self._sync_export_bar()
        self.update_est()

    # [MAP] Preset persistence helpers (gif_customs / reload_gif_combo / edit_gif_presets and the hb_* twins).
    # Bad or partial JSON in settings is ignored (returns []), and reload_* selects the previous title if it still
    # exists, else index 0. The combos' currentIndexChanged handler stores the title and refreshes the estimate.
    def gif_customs(self):
        try:
            return [c for c in json.loads(self._gif_settings.value("gif_custom", "[]", str))
                    if all(k in c for k in ("w", "fps", "lossy", "colors"))]
        except Exception:
            return []

    def reload_gif_combo(self, select=""):
        self.gif_combo.blockSignals(True)
        self.gif_combo.clear()
        for p in gif_builtin() + self.gif_customs():
            self.gif_combo.addItem(gif_title(p), p)
        i = self.gif_combo.findText(select)
        self.gif_combo.setCurrentIndex(i if i >= 0 else 0)
        self.gif_combo.blockSignals(False)

    def edit_gif_presets(self):
        dlg = GifPresetDialog(self, self.gif_customs())
        dlg.exec()
        self._gif_settings.setValue("gif_custom", json.dumps(dlg.customs))
        self.reload_gif_combo(self.gif_combo.currentText())
        self.update_est()

    def hb_customs(self):
        try:
            return [c for c in json.loads(self._hb_settings.value("hb_custom", "[]", str))
                    if all(k in c for k in ("h", "fps", "encoder", "mode", "value", "format"))]
        except Exception:
            return []

    def reload_hb_combo(self, select=""):
        self.hb_combo.blockSignals(True)
        self.hb_combo.clear()
        for p in hb_builtin() + self.hb_customs():
            self.hb_combo.addItem(hb_title(p), p)
        i = self.hb_combo.findText(select)
        self.hb_combo.setCurrentIndex(i if i >= 0 else 0)
        self.hb_combo.blockSignals(False)

    def edit_hb_presets(self):
        dlg = HbPresetDialog(self, self.hb_customs())
        dlg.exec()
        self._hb_settings.setValue("hb_custom", json.dumps(dlg.customs))
        self.reload_hb_combo(self.hb_combo.currentText())
        self.update_est()

    def _on_hb_toggled(self, on):
        self._hb_settings.setValue("hb_on", bool(on))
        if hasattr(self, "kind_btn"):
            self._sync_export_bar()
        if hasattr(self, "est_label"):
            self.update_est()

    # [MAP] GIF export entry (called from export() in GIF mode): asks for a .gif path, runs ExportWorker(gif=preset).
    # The finished dialog adds a note when lossy > 0 but gifsicle is missing (lossy is then only approximated).
    def export_gif(self, parts):
        preset = self.gif_combo.currentData()
        self.engine.pause()
        base = os.path.splitext(parts[0][0].path)[0]
        # [FIX B5] Shift+click also skips the save dialog in GIF mode, same as the normal video export path.
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            out = self.export_beside(parts[0][0].path, ".gif", "GIF")
        else:
            out = self.export_auto(parts[0][0].path, ".gif", "GIF")
            if not out:
                out, _ = QFileDialog.getSaveFileName(self, "Export GIF", self.export_default(parts[0][0].path, ".gif", "GIF"), "GIF (*.gif)")
                if not out:
                    return
        if not out.lower().endswith(".gif"):
            out += ".gif"

        def done(ok, msg):
            if ok:
                note = ""
                if preset["lossy"] > 0 and not find_tool("gifsicle"):
                    note = "\n\n(gifsicle not found - lossy was approximated. Put gifsicle.exe next to SQVCE n4.0 for true lossy compression.)"
                self.statusBar().showMessage(f"Exported: {msg}", 10000)
                ExportDoneDialog(self, msg, note).exec()
            elif msg == "Cancelled":
                self.statusBar().showMessage("Export cancelled.", 5000)
            else:
                QMessageBox.critical(self, "Export failed", msg)

        self._run_export(parts, out, done, gif=preset)

    # [MAP] Video <-> GIF switch. To GIF with clips on the timeline: Yes = keep / No = clear (via Sequence.edit, so it
    # is undoable) / Cancel. To Video: plain confirmation. A refused switch re-highlights the current mode button.
    # Switching toggles: GIF combo+cog visible, Save-Over BUTTON hidden [but see F4], Transcode controls hidden,
    # estimates refreshed. app_mode is a plain string ("Video"/"GIF") read by est_*, export and _on_hb_toggled.
    def request_mode(self, name):
        self.plugins.mode_left()                # [52.24] tell the mod mode we are leaving (before the page switch)
        self.page_stack.setCurrentIndex(0)      # leave any mod tab
        if name == self.app_mode:
            self.titlebar.set_mode(self.app_mode)
            return
        keep = True                              # [52.4] no confirmation dialogs; the timeline is always kept
        SB = QMessageBox.StandardButton
        self.app_mode = name
        self.titlebar.set_mode(name)
        self._sync_export_bar()                  # [52.33] gif/transcode/save-over visibility in one place
        self._on_hb_toggled(self.hb_check.isChecked())
        self.update_est()
        if name == "GIF" and not keep:
            if self.stage.tool is not None:
                self.exit_tool()

            def clear():
                if not self.seq.segs:
                    return False
                self.seq.segs.clear()
                return True
            self.seq.edit(clear)
            self.tl.sel = -1
            self.tl.update()

    def exit_tool(self):
        self.b_sel.setChecked(True)
        self.set_tool("select")

    # [PITFALL] "Current clip" = the clip under the PLAYHEAD, not the timeline selection. Crop, Resize, rotate and reset
    # act on it. Returns None for an empty timeline.
    def current_seg(self):
        idx, _ = self.seq.locate(self.engine.playhead)
        return self.seq.segs[idx] if idx is not None and self.seq.segs else None

    # [MAP] Pushes the current clip's (xf, source size, (rotation, mirror)) into VideoStage. Audio-only clips or an
    # empty timeline -> stage.set_state(None, (0,0)).
    def refresh_stage(self):
        seg = self.current_seg()
        if seg is None or not seg.media.w:
            self.stage.set_state(None, (0, 0))
        else:
            self.stage.set_state(seg.xf, (seg.media.w, seg.media.h), (seg.rot % 360.0, bool(seg.mirror)))

    # [INVARIANT] Turns a finished crop/resize selection (stage pixels) into a new Seg.xf through Sequence.edit:
    #   crop (Video mode)   -> (sel_w, sel_h, px - sel_x, py - sel_y, pw, ph, color, 0, 0, sel_w, sel_h)
    #                          canvas AND clip_rect both become the selection, picture shifts - same visible
    #                          result as before clip_rect existed (canvas_rect = clip_rect on every Video-mode
    #                          commit, so growing back out - "un-crop", v4 - still works without special cases)
    #   crop (Canvas mode)  -> (sel_w, sel_h, px - sel_x, py - sel_y, pw, ph, color, clx - sel_x, cly - sel_y, clw, clh)
    #                          ONLY canvas (and, to keep it visually put, picture) re-base onto the selection's
    #                          new origin - clip_rect's own SIZE is untouched, which is what stops a Canvas-mode
    #                          resize from ever restoring or re-hiding the crop
    #   resize              -> (cw, ch, sel_x, sel_y, sel_w, sel_h, color, clx, cly, clw, clh)
    #                          canvas AND clip_rect unchanged (Resize never re-bases the coordinate origin),
    #                          picture = selection
    # All three pass norm_xf (even sizes). An unchanged result only re-lays-out; a result equal to the identity
    # transform is stored as None (so the clip returns to the lossless fast path). `color` is the blank/background
    # swatch chosen in VideoStage's bar; it rides along in xf's 7th slot, clip_rect in the 8th-11th (see
    # xf_color/xf_clip/norm_xf/xf_filter).
    def on_stage_commit(self, tool, sel, color):
        seg = self.current_seg()
        if seg is None or not seg.media.w or self.stage.scale() <= 0:
            return
        W, H = seg.media.w, seg.media.h
        ident = (W, H, 0, 0, W, H, DEFAULT_BG, 0, 0, W, H)
        old = norm_xf(seg.xf or ident)
        cw, ch, px, py, pw, ph, _old_color, clx, cly, clw, clh = old
        rc, k = self.stage.canvas_rect(), self.stage.scale()
        rx, ry = (sel.x() - rc.x()) / k, (sel.y() - rc.y()) / k
        rw, rh = sel.width() / k, sel.height() / k
        if tool == "crop" and self.stage.crop_mode == "canvas":
            new = norm_xf((rw, rh, px - rx, py - ry, pw, ph, color, clx - rx, cly - ry, clw, clh))
        elif tool == "crop":
            new = norm_xf((rw, rh, px - rx, py - ry, pw, ph, color, 0, 0, rw, rh))
        else:
            new = norm_xf((cw, ch, rx, ry, rw, rh, color, clx, cly, clw, clh))
        if new == old:
            self.stage.relayout()                     # snap the overlay/preview back
            return
        val = None if new == norm_xf(ident) else new

        def do():
            seg.xf = val
            return True
        self.seq.edit(do)
        self.statusBar().showMessage("Crop / resize applied - this clip will be re-encoded on export "
                                     "(the rest stay lossless).", 6000)

    # [MAP] Right-click "Reset crop && resize": sets the current clip's xf to None (also undoes a Resize) via
    #   Sequence.edit.
    def reset_xf(self):
        seg = self.current_seg()
        if seg is not None and seg.xf is not None:
            def do():
                seg.xf = None
                return True
            self.seq.edit(do)

    # [MAP] Sets Sequence.precise (Snap is toggled by its own lambda through the exclusive group) and shows the
    # explanatory hint. [KNOWN ISSUE F1] The hint - like the Help text and tooltip - says only "the small sliver"
    # is re-encoded; in reality the entire clip is re-encoded when its start is off-keyframe.
    def set_trim_mode(self, keyframes):
        self.seq.snap = bool(keyframes)
        self.mode_btn.setText("Keyframes" if keyframes else "Precise")
        self.on_precise_toggled(not keyframes)

    def on_precise_toggled(self, on):
        self.seq.precise = on
        if on:
            self.statusBar().showMessage(
                "Precise trimming on: you can drag a clip's start to any frame. Export will re-encode "
                "just the small sliver up to the next keyframe - the rest of the clip stays lossless.", 7000)

    # ------------------------------------------------------------------ frameless window chrome
    # [MAP] Frameless window: maximise is emulated by setting the geometry to the screen's availableGeometry and
    # remembering the previous rectangle; there is no native maximised state. Edge resizing: _resize_edges +
    # eventFilter (application-wide filter installed in __init__).
    def toggle_maximize(self):
        if self._is_maximized:
            if self._restore_geom is not None:
                self.setGeometry(self._restore_geom)
            self._is_maximized = False
        else:
            self._restore_geom = self.geometry()
            screen = self.screen() or QApplication.primaryScreen()
            if screen:
                self.setGeometry(screen.availableGeometry())
            self._is_maximized = True
        self.titlebar.set_maximized(self._is_maximized)

    def _resize_edges(self, global_pos):
        if self._is_maximized:
            return Qt.Edge(0)
        top_left = self.mapToGlobal(QPoint(0, 0))
        x, y = global_pos.x() - top_left.x(), global_pos.y() - top_left.y()
        w, h = self.width(), self.height()
        if x < -self.RESIZE_MARGIN or y < -self.RESIZE_MARGIN or x > w + self.RESIZE_MARGIN or y > h + self.RESIZE_MARGIN:
            return Qt.Edge(0)          # far from this window - not ours to handle
        m = self.RESIZE_MARGIN
        edges = Qt.Edge(0)
        if x <= m:
            edges |= Qt.Edge.LeftEdge
        elif x >= w - m:
            edges |= Qt.Edge.RightEdge
        if y <= m:
            edges |= Qt.Edge.TopEdge
        elif y >= h - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    _EDGE_CURSORS = {
        Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor, Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor, Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
    }

    def _cursor_for_edges(self, edges):
        if not edges:
            return None
        has = lambda e: bool(edges & e)
        if (has(Qt.Edge.LeftEdge) and has(Qt.Edge.TopEdge)) or (has(Qt.Edge.RightEdge) and has(Qt.Edge.BottomEdge)):
            return Qt.CursorShape.SizeFDiagCursor
        if (has(Qt.Edge.RightEdge) and has(Qt.Edge.TopEdge)) or (has(Qt.Edge.LeftEdge) and has(Qt.Edge.BottomEdge)):
            return Qt.CursorShape.SizeBDiagCursor
        for e, c in self._EDGE_CURSORS.items():
            if has(e):
                return c
        return None

    # [PITFALL] Installed on the whole QApplication, so it sees EVERY mouse event. Keep it fast and side-effect free;
    # it only starts a system resize when a left-press is within RESIZE_MARGIN (6 px) of this window's border, and
    # updates the resize cursor on mouse-move.
    def eventFilter(self, obj, ev):
        et = ev.type()
        if et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and isinstance(obj, QWidget) \
                and obj is QApplication.focusWidget() and obj.window() is self:
            # [52.15] A focused button/list/checkbox swallowed Space ("sometimes doesn't play"): route it to the window,
            # except while typing in a text field.
            from PySide6.QtWidgets import QAbstractSpinBox, QPlainTextEdit, QTextEdit
            typing = isinstance(obj, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)) or \
                (isinstance(obj, QComboBox) and obj.isEditable())
            if not typing:
                if et == QEvent.Type.KeyPress and self._key_is("play_pause", ev):
                    self.keyPressEvent(ev)
                    return True
                if et == QEvent.Type.KeyRelease and ev.key() == getattr(self, "_space_key", None) and self._space_is_down:
                    self.keyReleaseEvent(ev)
                    return True
        if et == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            edges = self._resize_edges(ev.globalPosition().toPoint())
            if edges:
                wh = self.windowHandle()
                if wh is not None:
                    try:
                        wh.startSystemResize(edges)
                        return True
                    except Exception:
                        pass
        elif et == QEvent.Type.MouseMove and not self._is_maximized:
            edges = self._resize_edges(ev.globalPosition().toPoint())
            cur = self._cursor_for_edges(edges)
            if cur is not None:
                self.setCursor(cur)
            elif self.cursor().shape() in self._EDGE_CURSORS.values() or self.cursor().shape() in (
                    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor):
                self.unsetCursor()
        return False

    # ------------------------------------------------------------------ screenshot
    # [MAP] On-demand frame grab from the video sink (DO NOT BREAK #4: no persistent frame subscription). Captures the
    # UNTRANSFORMED source frame (no crop/rotation/mirror). Saves PNG/JPG next to the primary file by default.
    def take_screenshot(self):
        frame = self.video.videoSink().videoFrame()
        img = frame.toImage() if frame.isValid() else None
        if img is None or img.isNull():
            QMessageBox.information(self, "Screenshot", "No video frame to capture right now - "
                                                         "play or scrub the preview first.")
            return
        base_dir = os.path.dirname(self.primary.path) if self.primary else os.getcwd()
        ts = fmt_tc(self.engine.playhead, self.seq.fps()).replace(":", "-")
        default = os.path.join(base_dir, f"screenshot_{ts}.png")
        out, _ = QFileDialog.getSaveFileName(self, "Save screenshot", default,
                                             "PNG Image (*.png);;JPEG Image (*.jpg)")
        if not out:
            return
        if img.save(out):
            self.statusBar().showMessage(f"Screenshot saved: {out}", 6000)
        else:
            QMessageBox.critical(self, "Screenshot", "Could not save the screenshot.")

    # ------------------------------------------------------------------ editing
    # [MAP] Insert one or more Media at time t (default: playhead) as whole-file Segs (mark_in..mark_out), consecutive,
    # in ONE undo step; then select the last inserted clip, fit the zoom if the timeline was empty, and seek to the end
    # of what was inserted. Used by Project double-click / context menu / file drops.
    def insert_media(self, medias, t=None, tol=0.1):
        aud = [m for m in medias if is_audio_media(m)]       # [52.19] audio files -> audio track above the video row
        if aud:
            if "audio" in self.seq.locked:                   # [52.25]
                self.seq.blocked.emit("audio")
            else:
                self.atrack.add(aud, self.engine.playhead if t is None else t)
            medias = [m for m in medias if m not in aud]
            if not medias:
                return
        was_empty = not self.seq.segs
        keep = self.engine.playhead                      # [52.12] playhead stays where it was
        t = keep if t is None else t
        result = {}

        def do():
            cursor = t
            for m in medias:
                seg = Seg(m, m.mark_in, m.mark_out)
                idx = self.seq.insert_at(cursor, seg, tol)
                cursor = self.seq.starts()[idx] + seg.dur
                result["idx"], result["end"] = idx, cursor
            return True

        self.seq.edit(do)
        self.tl.sel = result.get("idx", -1)
        if was_empty:
            self.tl.zoom_fit(animate=False)
        self.engine.seek(keep, play=False)

    # [52.17] Blank clip = a baked solid-colour video (probing.blank_clip) wrapped in a Media with blank=True. It is NOT
    # registered in medias/by_path (no Project row); one Media per colour, shared by all clips of that colour.
    def _blank_media(self, color):
        w = h = 0
        for s in self.seq.segs:
            if s.media.has_video and s.media.w and s.media.h:
                w, h = s.media.w, s.media.h
                break
        w, h = (w, h) if w and h else (1280, 720)
        key = (color.lower(), w, h)
        cache = self.__dict__.setdefault("_blank_cache", {})
        m = cache.get(key)
        if m is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                path = blank_clip(color, w, h)
            finally:
                QApplication.restoreOverrideCursor()
            m = probe_media(path) if path else None
            if not m:
                self.statusBar().showMessage("Could not create a blank clip (ffmpeg missing?)", 6000)
                return None
            m.name, m.blank, m.color = "Blank", True, QColor(color).name()
            m.fps = next((s.media.fps for s in self.seq.segs if not getattr(s.media, "blank", False)), 30.0)   # export bakes at this fps
            m.mark_in, m.mark_out = 0.0, min(4.0, m.dur)
            m.keyframes = [i / 30.0 for i in range(int(m.dur * 30) + 1)]
            cache[key] = m
        return m

    def add_blank_clip(self, t):
        m = self._blank_media("#000000")
        if m:
            self.insert_media([m], t, tol=8 / self.tl.pps)

    def recolor_blank(self, seg):
        c = QColorDialog.getColor(QColor(seg.media.color), self, "Blank clip color")
        if not c.isValid() or c.name() == seg.media.color:
            return
        m = self._blank_media(c.name())
        if not m:
            return

        def do():
            seg.media = m
            return True
        self.seq.edit(do)

    def on_files_dropped(self, paths, t):
        medias = self.import_paths(paths)
        if medias:
            self.insert_media(medias, t, tol=8 / self.tl.pps)

    # [MAP] Razor / Shift+C: Sequence.split_at through edit(). edit() returns the split time (float) or False; False
    # shows the "Can't cut here" hint. [PITFALL] Do not wrap this in `if r:` - see Sequence.edit.
    def split_at(self, t):
        r = self.seq.edit(lambda: self.seq.split_at(t))
        if r is False:
            self.statusBar().showMessage(
                "Can't cut here - no keyframe inside this clip near the cursor "
                "(turn off keyframe snapping to cut anywhere; the export may then not be exact).", 7000)
        else:
            self.statusBar().showMessage(f"Edit added at source time {fmt_tc(r, self.seq.fps())}", 4000)

    # [FIX B2] Delete/Ripple-Delete removes the WHOLE group (or multi-selection) the selected clip belongs to,
    # not just the one clip under it, mirroring how a plain click already selects the whole group.
    def delete_selected(self):
        i = self.tl.sel
        if not (0 <= i < len(self.seq.segs)):
            return
        grp = self.seq.segs[i].grp
        if grp is not None:
            idxs = [k for k, s in enumerate(self.seq.segs) if s.grp == grp]
        elif len(self.tl.multi_sel) > 1:
            idxs = list(self.tl.multi_sel)
        else:
            idxs = [i]
        if self.seq.edit(lambda: self.seq.delete_many(idxs)) is not False:
            self.tl.sel = -1
            self.tl.multi_sel.clear()
            self.tl.update()

    # [COUPLING] clipboard_seg is a positional 12-tuple copy of the Seg (media, in_s, out_s, xf, mute, speed,
    # mirror, rot, rev, atracks, vol_db, track_type) - same order as Seg.__init__ / snapshot (minus grp - a
    # copy/paste deliberately does NOT carry group membership). paste_clip unpacks it positionally.
    def copy_selected(self):
        i = self.tl.sel
        if not (0 <= i < len(self.seq.segs)):
            self.statusBar().showMessage("Select a clip on the timeline first (click it), then Ctrl+C.", 4000)
            return
        s = self.seq.segs[i]
        self.clipboard_seg = (s.media, s.in_s, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev,
                              s.atracks, s.vol_db, s.track_type)
        self.statusBar().showMessage(f"Copied {s.media.name} ({fmt_tc(s.dur, self.seq.fps())})", 3000)

    # [52.20] Ctrl+D. A selected lane clip (text / audio) duplicates itself (lane.duplicate_key); otherwise the selected
    # video clip(s) are copied right after the last selected one (magnetic: everything after ripples).
    def duplicate_selected(self):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QSpinBox, QDoubleSpinBox)) or hasattr(fw, "toPlainText") or self._tool_locked():
            return
        for ln in self.tl.unlocked_lanes():                  # [52.25] locked rows are skipped
            f = getattr(ln, "duplicate_key", None)
            if f and f():
                return
        segs = self.seq.segs
        idxs = sorted(i for i in (self.tl.multi_sel or {self.tl.sel}) if 0 <= i < len(segs))
        if not idxs:
            self.statusBar().showMessage("Select a clip first, then Ctrl+D.", 3000)
            return
        keep, res = self.engine.playhead, {}

        def do():
            cp = [Seg(s.media, s.in_s, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev, None, s.atracks, s.vol_db,
                      s.track_type) for s in (segs[i] for i in idxs)]
            segs[idxs[-1] + 1:idxs[-1] + 1] = cp
            res["new"] = set(range(idxs[-1] + 1, idxs[-1] + 1 + len(cp)))
            return True
        self.seq.edit(do)
        self.tl.multi_sel, self.tl.sel = res["new"], min(res["new"])
        self.tl._invalidate_content()
        self.engine.seek(keep, play=False)
        self.statusBar().showMessage("Duplicated clip" if len(idxs) == 1 else f"Duplicated {len(idxs)} clips", 3000)

    def paste_clip(self):
        if not self.clipboard_seg:
            self.statusBar().showMessage("Nothing to paste - select a clip and press Ctrl+C first.", 4000)
            return
        media, a, b, xf, mute, speed, mirror, rot, rev, atracks, vol_db, track_type = self.clipboard_seg
        t = keep = self.engine.playhead
        was_empty = not self.seq.segs
        result = {}

        def do():
            seg = Seg(media, a, b, xf, mute, speed, mirror, rot, rev, None, atracks, vol_db, track_type)
            idx = self.seq.insert_at(t, seg, tol=0.1)
            result["idx"] = idx
            result["end"] = self.seq.starts()[idx] + seg.dur
            return True

        self.seq.edit(do)
        self.tl.sel = result["idx"]
        if was_empty:
            self.tl.zoom_fit(animate=False)
        self.engine.seek(keep, play=False)
        self.statusBar().showMessage("Pasted clip", 3000)

    # ------------------------------------------------------------------ export / save-over
    # [52.34] Plays finish.mp3 when an export / GIF export / Save-Over succeeds. Looked up next to the .exe (frozen) or this
    # file, then the PyInstaller bundle dir; silently does nothing when the file is missing or audio fails.
    def play_finish_sound(self):
        try:
            dirs = []
            if getattr(sys, "frozen", False):
                dirs.append(os.path.dirname(sys.executable))
                if getattr(sys, "_MEIPASS", None): dirs.append(sys._MEIPASS)
            dirs.append(os.path.dirname(os.path.abspath(__file__)))
            f = next((os.path.join(d, "finish.mp3") for d in dirs if os.path.isfile(os.path.join(d, "finish.mp3"))), None)
            if not f: return
            if not hasattr(self, "_fin_player"):
                self._fin_player = QMediaPlayer(self); self._fin_out = QAudioOutput(self)
                self._fin_player.setAudioOutput(self._fin_out)
            self._fin_player.setSource(QUrl.fromLocalFile(f)); self._fin_player.play()
        except Exception:
            pass

    # [MAP] Shared launcher for Export, GIF export and Save-Over. Builds a WindowModal QProgressDialog sized
    # (1 part -> 1 step, n parts -> n+1) + 1 for GIF/Transcode, starts an ExportWorker (precise is FORCED on for GIF),
    # disables the Export/Save-Over BUTTONS while running and re-enables them in `finished`. The worker and dialog are
    # kept in self._exp / self._dlg so they are not garbage-collected while running.
    # [PITFALL] The Ctrl+M / Ctrl+S actions are not disabled; concurrent exports are (presumably) prevented only by the
    # dialog being window-modal - this was not verified. The `canceled` signal is disconnected before closing the
    #   dialog, otherwise closing
    # would trigger cancel() on a finished worker.
    def _run_export(self, parts, out_path, on_done, gif=None, transcode=None):
        steps = (1 if len(parts) == 1 else len(parts) + 1) + (1 if (gif or transcode) else 0)
        dlg = QProgressDialog("Preparing...", "Cancel", 0, steps, self)
        dlg.setWindowTitle("Exporting")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        w = ExportWorker(parts, out_path, precise=self.seq.precise or bool(gif), gif=gif, transcode=transcode)
        w.progress.connect(lambda i, s: (dlg.setValue(i), dlg.setLabelText(s)))
        self.export_btn.setEnabled(False)
        self.saveover_btn.setEnabled(False)

        def finished(ok, msg):
            try:
                dlg.canceled.disconnect()
            except Exception:
                pass
            dlg.close()
            self.export_btn.setEnabled(True)
            self.saveover_btn.setEnabled(True)
            if ok: self.play_finish_sound()
            on_done(ok, msg)

        w.done.connect(finished)
        dlg.canceled.connect(w.cancel)
        self._exp, self._dlg = w, dlg
        w.start()

    # [MAP] Validation shared by Export and Save-Over: ffmpeg present, timeline not empty, and a warning when clips differ
    # in codec/resolution/fps/audio without any crop/speed/mute part.
    # [KNOWN ISSUE F13] The warning text ("the result may not play correctly") is out of date: since _parts_match gates
    # the stream-copy join, mismatching clips are now re-encoded safely. It also appears for GIF/Transcode, where a
    # re-encode happens anyway. Wording only - behaviour is safe.
    def _checked_parts(self):
        """Common validation shared by Export and Save-Over. Returns parts or None."""
        parts = self.seq.export_parts()
        if not FFMPEG:
            QMessageBox.critical(self, "Export", "ffmpeg.exe not found.")
            return None
        if not parts:
            QMessageBox.information(self, "Export", "The timeline is empty. Add some clips first.")
            return None
        sigs = {(m.vcodec, m.w, m.h, round(m.fps, 2), m.acodec) for m, *_ in parts}
        if len(sigs) > 1 and not any(p[3] or p[4] for p in parts):   # crop/resize/speed/mute re-encodes anyway
            r = QMessageBox.question(
                self, "Clips don't match",
                "These clips differ in codec, resolution, frame rate or audio format.\n"
                "A lossless join needs them to match - the result may not play correctly.\n\nExport anyway?")
            if r != QMessageBox.StandardButton.Yes:
                return None
        return parts

    # [MAP] Export entry: GIF mode -> export_gif. Otherwise: pause, choose an output name (`_edit` suffix; extension from
    # the Transcode preset's format or the first source), refuse to overwrite a source clip, run the worker, show
    # ExportDoneDialog.
    # [KNOWN ISSUE F8] The "same as a source clip" guard uses abspath() equality, which is case-SENSITIVE; on Windows
    # "C:\A.MP4" vs "C:\a.mp4" would slip through and ffmpeg would write over its own input. Use os.path.normcase.
    def export(self):
        parts = self._checked_parts()
        if parts is None:
            return
        if self.is_gif():
            return self.export_gif(parts)
        self.engine.pause()
        first = parts[0][0]
        base = os.path.splitext(first.path)[0]
        transcode = self.hb_combo.currentData() if self.hb_check.isChecked() else None
        ext = ("." + transcode["format"]) if transcode else os.path.splitext(first.path)[1]
        filt = f"Video (*{ext})" if transcode else f"Video (*{ext});;All files (*.*)"
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            out = self.export_beside(first.path, ext, "Video")
        else:
            out = self.export_auto(first.path, ext, "Video")
            if not out:
                out, _ = QFileDialog.getSaveFileName(self, "Export sequence", self.export_default(first.path, ext, "Video"), filt)
                if not out:
                    return
        if not transcode and not os.path.splitext(out)[1]:
            out += ext
        elif transcode and not out.lower().endswith(ext):
            out += ext
        if any(os.path.abspath(out) == os.path.abspath(m.path) for m, *_ in parts):
            QMessageBox.critical(self, "Export", "Choose a different file name than the source clips "
                                                 "(use Save-Over if you want to replace the original).")
            return

        def done(ok, msg):
            if ok:
                self.statusBar().showMessage(f"Exported: {msg}", 10000)
                ExportDoneDialog(self, msg).exec()
            elif msg == "Cancelled":
                self.statusBar().showMessage("Export cancelled.", 5000)
            else:
                QMessageBox.critical(self, "Export failed", msg)

        self._run_export(parts, out, done, transcode=transcode)

    # [DO NOT BREAK] Overwrites the PRIMARY file with the current edit - always lossless (never transcodes). Sequence:
    # warn if the timeline is not entirely from the primary, hard confirm, pause, RELEASE the player's lock
    # (stop + setSource(QUrl())), worker.cancel(pm, wait=True), proxy.cancel_media, export to a hidden temp file
    # ".<name>.quickcut_tmp<ext>" in the SAME folder, then _replace_with_retry(tmp, original) (atomic swap on the same
    # volume). On failure the original is untouched (temp removed, loading resumed); if only the final replace fails
    # the temp file is kept and its path is shown. On success _reload_primary_after_saveover runs.
    # [KNOWN ISSUE F4] Not blocked in GIF mode when reached through Ctrl+S.
    def save_over(self):
        if self.primary is None:
            QMessageBox.information(self, "Save-Over", "Import a file first - the first file you import "
                                                        "is marked primary automatically (the star icon).")
            return
        parts = self._checked_parts()
        if parts is None:
            return
        srcs = {m.path for m, *_ in parts}
        path = self.primary.path
        if srcs != {path}:
            r = QMessageBox.question(
                self, "Save-Over",
                f"The current timeline doesn't come entirely from the primary file "
                f"({self.primary.name}).\n\nSave-Over will still overwrite {self.primary.name} with "
                f"whatever is currently on the timeline. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        r = QMessageBox.question(
            self, "Save-Over",
            f"This will permanently overwrite the original file:\n\n{path}\n\n"
            "This cannot be undone. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        self.engine.pause()
        # Fully release our own hold on the file before writing to it - on Windows, the media
        # player keeps a lock on whatever file is loaded, and that lock is the #1 cause of
        # Save-Over failing with a "file is in use" error even though QuickCut is the only
        # thing that had it open.
        self.engine.player.stop()
        self.engine.player.setSource(QUrl())
        self.engine.path = None
        pm = self.primary
        self.worker.cancel(pm, wait=True)     # background loader must let go of the file too
        self.proxy.cancel_media(pm.path)
        d, base = os.path.split(path)
        tmp = os.path.join(d, f".{base}.quickcut_tmp{os.path.splitext(base)[1]}")

        def done(ok, msg):
            if ok:
                ok2, err2 = _replace_with_retry(tmp, path)
                if not ok2:
                    self._resume_load(pm)
                    QMessageBox.critical(
                        self, "Save-Over failed",
                        f"Export succeeded but replacing the original file failed:\n{err2}\n\n"
                        f"The edited file is still saved here, nothing was lost:\n{tmp}\n\n"
                        f"This usually means another program (a media player, antivirus, cloud-sync "
                        f"tool) has the file open. Close it and try Save-Over again.")
                    return
                self._reload_primary_after_saveover()
                self.statusBar().showMessage(f"Saved over: {path}", 10000)
                QMessageBox.information(self, "Saved", f"Overwrote:\n{path}")
            else:
                self._resume_load(pm)
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                if msg != "Cancelled":
                    QMessageBox.critical(self, "Save-Over failed", msg)
                else:
                    self.statusBar().showMessage("Save-Over cancelled.", 5000)

        self._run_export(parts, tmp, done)

    # [MAP] The primary file's bytes changed. Re-probe it (mutating the SAME Media object so every reference stays
    # valid), reset marks and keyframes, replace the timeline with ONE whole-file clip, clear undo/redo, restart the
    # background load and zoom-fit.
    # [KNOWN ISSUE F5] Clip thumbnails are dropped from memory but the on-disk cache is keyed without mtime, so old
    # frames can reappear.
    def _reload_primary_after_saveover(self):
        """The primary file's bytes changed on disk - refresh everything derived from it and
        collapse the timeline to the new (now single, whole) file, ready for further editing."""
        m = self.primary
        self.tl.clear_thumbs_for(m.path)
        newm = probe_media(m.path)
        if newm:
            m.dur, m.fps, m.w, m.h = newm.dur, newm.fps, newm.w, newm.h
            m.vcodec, m.acodec, m.has_video = newm.vcodec, newm.acodec, newm.has_video
        m.mark_in, m.mark_out = 0.0, m.dur
        m.keyframes = None
        m.thumb_path = None
        self.seq.segs = [Seg(m, 0.0, m.dur)]
        self.seq.undo_stack.clear()
        self.seq.redo_stack.clear()
        self.seq.edited.emit()
        row = self.rows.get(m)
        if row:
            row.set_info(m)
        self.worker.start(m)
        self.tl.zoom_fit(animate=False)

    # [MAP] Confirm if clips are on the timeline, then release the player and shut down the reverse proxy (kills its
    # ffmpeg, deletes its temp folder). Does not cancel a running export (the window-modal progress dialog is expected to
    # block closing while one runs - not verified).
    def closeEvent(self, e):
        self.engine.pause()
        if self.seq.segs and not (QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier):   # [52.15] Shift+click X = no warning
            r = QMessageBox.question(self, "Close " + APP_NAME, "There are clips on the timeline.\n\nClose the program anyway?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.save_layout()
        self.engine.player.stop()
        self.engine.player.setSource(QUrl())
        self.proxy.shutdown()
        super().closeEvent(e)
        self.recovery.shutdown_clean()        # [52.7] normal exit: the recovery file is no longer needed


# [MAP] Entry point: Fusion style + dark palette + QSS, then MainWindow with any existing file paths from argv
# (imported on the first event-loop turn). Run:  python QuickCut.py [video files...]
