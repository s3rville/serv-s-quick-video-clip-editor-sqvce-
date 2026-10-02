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

# [STALE][KNOWN ISSUE F14] Unused imports (safe to delete): QRect, QRegion, QCursor (QtCore/QtGui) and
# QVideoWidget (QtMultimediaWidgets - replaced by VideoView/QGraphicsVideoItem). Left as-is on purpose.
from PySide6.QtCore import (Qt, QUrl, QTimer, QObject, Signal, QRectF, QPointF, QLineF,
                            QSize, QMimeData, QThread, QPoint, QEvent, QRect, QSizeF, QSettings,
                            QVariantAnimation, QEasingCurve)
from PySide6.QtGui import (QLinearGradient, QAction, QColor, QPainter, QPen, QPixmap, QIcon, QPalette, QFont,
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

# ----------------------------------------------------------------------------- small widgets
# [MAP] Dark framed container: optional header row (title on the left; MainWindow adds buttons to head_lay) +
# `body`/`body_lay` for content. Styled by QSS ids #panel / #panelHead / #panelTitle.
class Panel(QFrame):
    """Dark panel: optional header with a title, then a body."""

    def __init__(self, title=None):
        super().__init__()
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head_lay = None
        if title is not None:
            head = QFrame()
            head.setObjectName("panelHead")
            self.head_lay = QHBoxLayout(head)
            self.head_lay.setContentsMargins(10, 0, 6, 0)
            self.title = QLabel(title)
            self.title.setObjectName("panelTitle")
            self.head_lay.addWidget(self.title)
            self.head_lay.addStretch(1)
            lay.addWidget(head)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(0)
        lay.addWidget(self.body, 1)


# ----------------------------------------------------------------------------- audio VU meter
# [FEATURE] Simple two-channel (L/R) audio level meter that sits to the left of the Timeline panel
# (MainWindow.build_timeline_panel). Driven by Engine.audioLevel(l, r) - each a linear RMS amplitude,
# roughly 0..1 (can exceed 1.0 briefly when a clip's own positive-dB Volume is applied; the bar just
# clips visually at the top). Purely a live-preview monitoring aid - never touches export.
# [FEATURE] While actually playing, the bars use normal VU ballistics (instant attack, gradual decay in
# _tick) and fall back towards zero on their own once playback stops sending new levels. While PAUSED
# (set_playing(False), via Engine.playStateChanged) the meter instead holds exactly the last level it was
# given - no decay - so pausing shows that frame's level instead of fading to silence. A seek made while
# paused (dragging the playhead) still calls set_level with whatever Engine.audioLevel reports for the
# new position, so the meter updates live while scrubbing too (best-effort: whether a paused seek actually
# yields a fresh audio buffer to report is up to the Qt multimedia backend, same as the paused video frame
# preview it rides alongside).
class VUMeter(QWidget):
    """Two-bar (L/R) audio level meter with peak-hold, for the left edge of the timeline."""

    FLOOR_DB = -54.0           # [51.3] dB scale: -54 dB = empty bar, 0 dB = full (linear RMS barely moved the bar)
    DECAY_PER_TICK = 0.90      # displayed level multiplies by this every tick unless a louder level arrives
    PEAK_DECAY_PER_TICK = 0.985
    TICK_MS = 33

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(20)
        self._l = self._r = 0.0
        self._peak_l = self._peak_r = 0.0
        self._playing = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.TICK_MS)

    # [MAP] Wired to Engine.playStateChanged. Switches between "live VU ballistics" and "hold the last
    # level" (see class docstring above).
    def set_playing(self, playing):
        self._playing = bool(playing)

    # [MAP] Called from Engine.audioLevel. While playing: instant attack, decay/peak fall happen only in
    # _tick (so a burst is visible even between two ticks). While paused: the exact level reported is shown
    # immediately with no smoothing at all, so scrubbing to a quieter/louder spot updates right away.
    def _frac(self, lin):
        """Linear RMS amplitude -> 0..1 bar fraction on a dB scale (much more sensitive to quiet audio)."""
        if lin <= 1e-6:
            return 0.0
        return max(0.0, min(1.0, 1.0 - math.log10(min(lin, 1.0)) * 20.0 / self.FLOOR_DB))

    def set_level(self, l, r):
        l = self._frac(l)
        r = self._frac(r)
        if self._playing:
            self._l = max(self._l, l)
            self._r = max(self._r, r)
            self._peak_l = max(self._peak_l, self._l)
            self._peak_r = max(self._peak_r, self._r)
        else:
            self._l, self._r = l, r
            self._peak_l, self._peak_r = l, r
            self.update()

    def _tick(self):
        if not self._playing:
            return          # paused: hold, don't decay (see class docstring)
        self._l *= self.DECAY_PER_TICK
        self._r *= self.DECAY_PER_TICK
        self._peak_l = max(self._l, self._peak_l * self.PEAK_DECAY_PER_TICK)
        self._peak_r = max(self._r, self._peak_r * self.PEAK_DECAY_PER_TICK)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        p.fillRect(rect, QColor(16, 18, 24))
        w = rect.width()
        bw = max(2.0, (w - 6) / 2.0)
        for i, (lvl, pk) in enumerate(((self._l, self._peak_l), (self._r, self._peak_r))):
            x = 2 + i * (bw + 2)
            bar_rect = QRectF(x, 2, bw, rect.height() - 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(30, 32, 40))
            p.drawRoundedRect(bar_rect, 2, 2)
            frac = max(0.0, min(1.0, lvl))
            fh = bar_rect.height() * frac
            filled = QRectF(bar_rect.x(), bar_rect.bottom() - fh, bar_rect.width(), fh)
            if fh > 0:
                # [51.3] colour = absolute loudness: green (bottom) > yellow > red (top), gradient fixed to the full bar
                g = QLinearGradient(0, bar_rect.bottom(), 0, bar_rect.top())
                g.setColorAt(0.0, QColor("#22c55e"))
                g.setColorAt(0.6, QColor("#3ddc84"))
                g.setColorAt(0.8, QColor("#ffd23f"))
                g.setColorAt(1.0, QColor("#ff3b3b"))
                p.setBrush(g)
                p.drawRoundedRect(filled, 2, 2)
            pf = max(0.0, min(1.0, pk))
            py = bar_rect.bottom() - bar_rect.height() * pf
            p.setPen(QPen(QColor("#ffffff"), 1.2))
            p.drawLine(QLineF(bar_rect.x(), py, bar_rect.right(), py))
            p.setPen(Qt.PenStyle.NoPen)
        p.end()


# [MAP] Widget shown for one Media inside the Project QListWidget (via setItemWidget). Two layouts sharing ONE
# QGridLayout: LIST = [star][thumbnail][name + info]; GRID = small card (thumbnail with star top-left and X
# top-right overlaid, name underneath). set_grid()/_place() re-arrange the same widgets - never create
# a second row widget for grid mode.
# The star and remove buttons are children positioned MANUALLY in _overlay() (not in the layout) so they float
# over the row; resizeEvent re-positions them. Signals: starClicked(media), removeClicked(media).
# A 2 px blue line at the bottom shows background-load progress (set_load_progress; lingers 450 ms at 100 %).
# [COUPLING] After creating a row call row.set_grid(self.grid_view) and MainWindow._size_item(m) (import_paths does).
class ProjectRow(QFrame):
    """One row in the Project panel: star (primary marker) + thumbnail + name/info + remove."""
    starClicked = Signal(object)
    removeClicked = Signal(object)

    def __init__(self, media, drag_path=None):
        super().__init__()
        self.media = media
        # The path used to build the outgoing drag's file URL. For a normal video/audio file this is
        # just its own path; for an imported STILL IMAGE it's the original picture (media.path is the
        # baked temp video - see image_to_clip), so a drop lands on the same by_path entry as the
        # Project item. Kept in sync by MainWindow.rename_media.
        self.drag_path = drag_path if drag_path is not None else media.path
        self._drag_start = None
        self.setObjectName("projectRow")
        self._load = None                       # None = hidden, else 0..1 background-load progress
        self._load_hide = QTimer(self)
        self._load_hide.setSingleShot(True)
        self._load_hide.timeout.connect(self._clear_load)
        lay = self.glay = QGridLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(8)
        self._grid = False
        self.star_btn = QToolButton(self)
        self.star_btn.setIconSize(QSize(15, 15))
        self.star_btn.setToolTip("Set as primary file (Save-Over target)")
        self.star_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.star_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.star_btn.setStyleSheet("QToolButton{border:none;background:transparent;padding:0;}")
        self.star_btn.clicked.connect(lambda: self.starClicked.emit(self.media))
        self.thumb = QLabel()
        self.thumb.setFixedSize(62, 35)
        self.thumb.setStyleSheet("background:#101010; border-radius:2px;")
        self.thumb.setScaledContents(True)
        self.txt = QWidget()
        txt_wrap = QVBoxLayout(self.txt)
        txt_wrap.setContentsMargins(0, 0, 0, 0)
        txt_wrap.setSpacing(0)
        self.name_lbl = QLabel(media.name)
        self.name_lbl.setStyleSheet("font-weight:600;")
        self.info_lbl = QLabel()
        self.info_lbl.setStyleSheet("color:#9a9a9a; font-size: 8pt;")
        txt_wrap.addWidget(self.name_lbl)
        txt_wrap.addWidget(self.info_lbl)
        self.remove_btn = QToolButton(self)
        self.remove_btn.setIcon(icon("x_small", "#888888"))
        self.remove_btn.setIconSize(QSize(13, 13))
        self.remove_btn.setToolTip("Remove from project")
        self.remove_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.setStyleSheet("QToolButton{border:none;background:rgba(24,24,24,215);padding:0;border-radius:2px;}"
                                      "QToolButton:hover{background:#e0413a;}")
        self.remove_btn.clicked.connect(lambda: self.removeClicked.emit(self.media))
        self._place()
        self.set_info(media)
        self.set_primary(False)

    def _place(self):
        """List: [star][thumb][name/info ...........(X overlaid at right edge, always visible)].
        Grid: small card - thumbnail with star (top-left) / X (top-right) overlaid, name underneath."""
        g = self.glay
        for w in (self.thumb, self.txt):
            g.removeWidget(w)
        for c in range(3):
            g.setColumnStretch(c, 0)
        self.txt.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)   # long names get clipped, never widen the row
        if self._grid:
            g.setContentsMargins(4, 4, 4, 3)
            g.setSpacing(2)
            g.addWidget(self.thumb, 0, 0, Qt.AlignmentFlag.AlignHCenter)
            g.addWidget(self.txt, 1, 0)
            self.thumb.setFixedSize(66, 37)
            self.setFixedWidth(76)
            self.star_btn.setIconSize(QSize(11, 11))
            self.remove_btn.setIconSize(QSize(10, 10))
            self.star_btn.setStyleSheet("QToolButton{border:none;background:rgba(0,0,0,150);padding:1px;border-radius:2px;}")
            self.name_lbl.setStyleSheet("font-weight:600;font-size:8pt;")
        else:
            g.setContentsMargins(6, 4, 6, 4)
            g.setSpacing(8)
            g.addWidget(self.thumb, 0, 1)
            g.addWidget(self.txt, 0, 2)
            g.setColumnStretch(2, 1)
            g.addWidget(self.star_btn, 0, 0)
            self.thumb.setFixedSize(62, 35)
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.star_btn.setIconSize(QSize(15, 15))
            self.remove_btn.setIconSize(QSize(13, 13))
            self.star_btn.setStyleSheet("QToolButton{border:none;background:transparent;padding:0;}")
            self.name_lbl.setStyleSheet("font-weight:600;")
        if self._grid:
            g.removeWidget(self.star_btn)                # overlay (positioned in _overlay), not in the layout
        self.info_lbl.setVisible(not self._grid)
        self._overlay()

    def _overlay(self):
        rb = self.remove_btn
        rb.setFixedSize(rb.iconSize().width() + 6, rb.iconSize().height() + 6)
        if self._grid:
            sb = self.star_btn
            sb.setFixedSize(sb.iconSize().width() + 4, sb.iconSize().height() + 4)
            tx = self.thumb.x()
            sb.move(tx + 2, self.thumb.y() + 2)
            rb.move(tx + self.thumb.width() - rb.width() - 2, self.thumb.y() + 2)
            sb.raise_()
        else:
            self.star_btn.setFixedSize(QSize(16, 16))
            rb.move(max(0, self.width() - rb.width() - 6), max(0, (self.height() - rb.height()) // 2))
        rb.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._overlay()

    # [MAP] Manual drag-out, independent of QListWidget's own item-drag detection: with a custom
    # itemWidget filling the whole row, mouse presses land on THIS widget first, and relying on the
    # view to notice a drag in progress underneath it is unreliable (this is what left imported
    # IMAGES undraggable onto the timeline - same code path as video, just never actually exercised
    # reliably). Pressing and dragging past Qt's drag-start distance on any non-interactive part of
    # the row (not the star/remove buttons, which have their own click handling) starts a QDrag
    # carrying drag_path as a file URL, exactly like ProjectList.mimeData() would.
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._drag_start is not None and (e.buttons() & Qt.MouseButton.LeftButton)
                and (e.position() - self._drag_start).manhattanLength() >= QApplication.startDragDistance()):
            self._drag_start = None
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(self.drag_path)])
            d = QDrag(self)
            d.setMimeData(md)
            pm = self.thumb.pixmap()
            if pm and not pm.isNull():
                d.setPixmap(pm.scaled(48, 27, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
            d.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_start = None
        super().mouseReleaseEvent(e)

    def set_grid(self, on):
        self._grid = bool(on)
        self._place()
        self.set_info(self.media)

    def set_info(self, m):
        full = f"{fmt_tc(m.dur, m.fps)}   {m.w}x{m.h}   {m.fps:g} fps" if m.w else f"{fmt_tc(m.dur, m.fps)}   audio"
        self.name_lbl.setText(self.name_lbl.fontMetrics().elidedText(m.name, Qt.TextElideMode.ElideMiddle, 68)
                              if self._grid else m.name)
        self.info_lbl.setText(full)
        self.setToolTip(f"{m.name}\n{full}")

    def set_load_progress(self, p):
        """Drive the thin blue load line along the bottom edge (no text). Lingers briefly at full."""
        if p >= 1.0:
            self._load = 1.0
            self._load_hide.start(450)
        else:
            self._load_hide.stop()
            self._load = max(0.0, p)
        self.update()

    def _clear_load(self):
        self._load = None
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._load:
            w = int(round(self.width() * self._load))
            if w > 0:
                p = QPainter(self)
                p.fillRect(0, self.height() - 2, w, 2, QColor("#2d8ceb"))
                p.end()

    def set_thumb(self, path):
        pm = QPixmap(path)
        if not pm.isNull():
            self.thumb.setPixmap(pm)

    def set_primary(self, is_primary):
        self.star_btn.setIcon(icon("star", "#f4c430" if is_primary else "#666666"))
        self.setStyleSheet("QFrame#projectRow{background:#2a3a4c;}" if is_primary else "QFrame#projectRow{background:transparent;}")


# [MAP] The Project bin: drag-and-drop list that both ACCEPTS files (OS file drops -> filesDropped) and lets
# you drag items OUT (mimeData exports file URLs, which Timeline.dropEvent understands). Internal drops
# also arrive as URLs, are re-imported, and are de-duplicated by MainWindow.import_paths (by_path) - so
# dragging within the list never duplicates media. In list view resizeEvent forces every item to the viewport
# width; grid (icon) mode is handled by MainWindow.set_project_grid/_size_item.
class ProjectList(QListWidget):
    """Drag-and-drop project bin shown on the left."""
    filesDropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("projectList")
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.viewMode() == QListWidget.ViewMode.ListMode:      # rows always exactly as wide as the panel
            w = self.viewport().width()
            for i in range(self.count()):
                it = self.item(i)
                sh = it.sizeHint()
                if sh.width() != w:
                    it.setSizeHint(QSize(w, sh.height()))

    def mimeData(self, items):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(i.data(Qt.ItemDataRole.UserRole)) for i in items])
        return md

    # [FIX] Qt's own item-drag machinery can arm itself on a mouse press regardless of which button was
    # pressed (only mouseMoveEvent's "did we move far enough" check is button-aware), which is how a
    # right-button drag over a row could show a drag pixmap that dropping never actually acts on ("visual
    # only"). Turning dragEnabled off for the duration of a right-button press blocks that path outright
    # without touching left-button dragging (ProjectRow's own manual QDrag, used to drop clips onto the
    # Timeline) or the context menu, which still opens normally on release.
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.setDragEnabled(False)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self.setDragEnabled(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.filesDropped.emit(paths)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = self.viewport().rect().adjusted(10, 10, -10, -10)
            pen = QPen(QColor("#2d8ceb"), 1.6, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRoundedRect(r, 8, 8)
            cx, cy = r.center().x(), r.center().y() - 14
            p.setPen(QPen(QColor("#2d8ceb"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QLineF(cx, cy - 12, cx, cy + 12))
            p.drawLine(QLineF(cx - 12, cy, cx + 12, cy))
            p.setPen(QColor("#8fb8de"))
            p.drawText(r.adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Drag and drop\nvideo files here")

