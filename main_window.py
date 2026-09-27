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
    pal = QPalette()
    R = QPalette.ColorRole
    for role, col in ((R.Window, "#2b2b2b"), (R.WindowText, "#dcdcdc"), (R.Base, "#1e1e1e"),
                      (R.AlternateBase, "#262626"), (R.Text, "#dcdcdc"), (R.Button, "#383838"),
                      (R.ButtonText, "#dcdcdc"), (R.Highlight, "#2d8ceb"), (R.HighlightedText, "#ffffff"),
                      (R.ToolTipBase, "#2a2a2a"), (R.ToolTipText, "#dddddd")):
        pal.setColor(role, QColor(col))
    return pal


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
        self.APP_TITLE = "QuickCut"
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
        outer_lay.addWidget(root, 1)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.build_topbar())

        main_split = QSplitter(Qt.Orientation.Vertical)
        v.addWidget(main_split, 1)

        top_split = QSplitter(Qt.Orientation.Horizontal)
        top_split.addWidget(self.build_project())
        top_split.addWidget(self.build_video_column())
        top_split.setSizes([300, 1020])

        main_split.addWidget(top_split)
        main_split.addWidget(self.build_timeline_panel())
        main_split.setSizes([560, 220])

        self.engine = Engine(self.seq, self.video)
        self.proxy = ReverseProxy()
        self.engine.proxies = self.proxy.files
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
        self.build_shortcuts()
        self.update_labels()
        QApplication.instance().installEventFilter(self)
        self.statusBar().showMessage(
            "Drag videos into the Project panel or straight onto the timeline  |  drag a clip's edge to trim")

        if not FFMPEG:
            QMessageBox.warning(self, "ffmpeg not found",
                                "Could not find ffmpeg.exe.\n\nPlace ffmpeg.exe and ffprobe.exe next to this "
                                "program (or add them to PATH), then restart.")
        if files:
            QTimer.singleShot(0, lambda: self.import_paths(files))

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
        logo = QLabel("QuickCut")
        logo.setStyleSheet("font-size: 11pt; font-weight: 700; color: #ffffff;")
        l.addWidget(logo)
        l.addStretch(1)
        self.warnings = WarningBar()
        l.addWidget(self.warnings)
        l.addSpacing(8)
        help_btn = QPushButton("Help")
        help_btn.setObjectName("helpbtn")
        help_btn.clicked.connect(self.show_help)
        l.addWidget(help_btn)
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
                                 "QuickCut's default lossless export")
        self.hb_check.toggled.connect(self._on_hb_toggled)
        self.hb_combo = QComboBox()
        self.hb_combo.setMinimumWidth(190)
        self.hb_combo.setToolTip("Transcode preset: resolution, fps, encoder and quality/bitrate")
        self.hb_cog = QToolButton()
        self.hb_cog.setText("\u2699")
        self.hb_cog.setToolTip("Add / edit your own Transcode presets")
        self.hb_cog.clicked.connect(self.edit_hb_presets)
        l.addWidget(self.hb_check)
        l.addWidget(self.hb_combo)
        l.addWidget(self.hb_cog)
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
        l.addWidget(self.est_label)
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("primary")
        self.export_btn.setToolTip("Export sequence as a new file (Ctrl+M)")
        self.export_btn.clicked.connect(self.export)
        l.addWidget(self.export_btn)
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
        self.cam_btn = self.tool_btn("camera", "Screenshot current frame", self.take_screenshot)
        row.addWidget(self.cam_btn)
        row.addSpacing(6)
        self.snap_cb = QCheckBox("Snap")
        self.snap_cb.setChecked(bool(FFPROBE))
        self.snap_cb.setEnabled(bool(FFPROBE))
        self.snap_cb.setToolTip("Snap cuts to keyframes. Lossless cuts can only start on keyframes, so with "
                                "this on, cut points snap to them and the preview matches the export exactly.\n"
                                + ("" if FFPROBE else "(ffprobe.exe not found - place it next to this program)"))
        self.snap_cb.toggled.connect(lambda on: setattr(self.seq, "snap", on))
        row.addWidget(self.snap_cb)
        row.addSpacing(6)
        self.precise_cb = QCheckBox("Precise")
        self.precise_cb.setChecked(False)
        self.precise_cb.setEnabled(bool(FFPROBE))
        self.precise_cb.setToolTip("Trim to any exact frame, not just keyframes. Lets you drag a clip's start "
                                   "freely; on export, only the small sliver up to the next keyframe gets "
                                   "re-encoded (fast, barely-visible quality cost) - the rest of every clip "
                                   "still exports as an untouched, lossless copy.\n"
                                   + ("" if FFPROBE else "(ffprobe.exe not found - place it next to this program)"))
        self.precise_cb.toggled.connect(self.on_precise_toggled)
        self.mode_grp = QButtonGroup(self)                # Snap / Precise: exactly one is always on
        self.mode_grp.setExclusive(True)
        self.mode_grp.addButton(self.snap_cb)
        self.mode_grp.addButton(self.precise_cb)
        if FFPROBE:
            self.precise_cb.setChecked(True)          # editor starts in Precise mode
        row.addWidget(self.precise_cb)
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
        self.tl.deleteRequested.connect(self.delete_selected)
        self.tl.copyRequested.connect(self.copy_selected)
        self.tl.pasteRequested.connect(self.paste_clip)
        self.tl.clipOptionsRequested.connect(self.edit_clip_options)
        self.tl.filesDropped.connect(self.on_files_dropped)
        self.tl.trimBlocked.connect(
            lambda name: self.statusBar().showMessage(
                f"Still analyzing {name} for exact cut points - trimming its start will be available in a "
                f"moment (or turn off Snap to trim freely, at the cost of frame-exact export).", 7000))
        return p

    # [KNOWN ISSUE F4] These QActions carry the global shortcuts. "Save-Over" (Ctrl+S) is NOT gated by app_mode:
    # in GIF mode the Save-Over BUTTON is hidden (request_mode) but Ctrl+S still calls save_over(), which then
    # overwrites the primary file with a lossless VIDEO export. Two confirm dialogs still guard it, but it is a
    # mode leak. Fix: return early in save_over() when app_mode == "GIF" (or disable the action in request_mode).
    # [PITFALL] Shortcut keys are window-wide; new single-letter keys can collide with build_shortcuts below.
    def build_actions(self):
        """Keyboard-shortcut actions only - no visible menu bar."""
        def act(text, slot, key):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(lambda _=False: slot())
            self.addAction(a)
            return a
        act("Import Media", self.import_dialog, "Ctrl+I")
        act("Save-Over", self.save_over, "Ctrl+S")
        act("Export", self.export, "Ctrl+M")
        act("Undo", self.seq.undo, "Ctrl+Z")
        act("Redo", self.seq.redo, "Ctrl+Shift+Z")
        act("Add Edit at Playhead", lambda: self.split_at(self.engine.playhead), "Shift+C")
        act("Copy Clip", self.copy_selected, "Ctrl+C")
        act("Paste Clip", self.paste_clip, "Ctrl+V")
        act("Ripple Delete", self.handle_delete_key, "Delete")
        act("Rename File", self.f2_rename, "F2")
        act("Quit", self.close, "Ctrl+Q")

    # [MAP] Plain-key QShortcuts (tool letters, arrows, Space, Home/End, Alt+arrows to move a clip). Keep the Help text
    # (show_help) and tool tooltips in sync with any change here.
    def build_shortcuts(self):
        def sc(key, fn):
            QShortcut(QKeySequence(key), self, activated=fn)
        # Space is handled in keyPressEvent/keyReleaseEvent instead of a QShortcut, so a tap vs. a
        # hold can be told apart (hold = 2x speed while playing; see SPACE_HOLD_MS).
        sc("V", lambda: (self.b_sel.setChecked(True), self.set_tool("select")))
        sc("C", lambda: (self.b_razor.setChecked(True), self.set_tool("razor")))
        sc("X", lambda: (self.b_crop.setChecked(True), self.set_tool("crop")))
        sc("R", self._r_shortcut)
        sc("Alt+Left", lambda: self.move_clip(-1))
        sc("Alt+Right", lambda: self.move_clip(1))
        sc("Left", lambda: self.step(-1))
        sc("Right", lambda: self.step(1))
        sc("Shift+Left", lambda: self.step(-5))
        sc("Shift+Right", lambda: self.step(5))
        sc("Up", lambda: self.goto_edit(-1))
        sc("Down", lambda: self.goto_edit(1))
        sc("Home", lambda: self._locked_seek(0.0))
        sc("End", lambda: self._locked_seek(self.seq.total()))

    def show_help(self):
        QMessageBox.information(self, "Keyboard shortcuts", (
            "Space  Play / pause   (hold down while playing = 2x speed instead)\n"
            "Left / Right  Step one frame  (Shift = 5 frames)\n"
            "Up / Down  Previous / next edit\n"
            "Home / End  Go to start / end\n"
            "V  Selection tool     C  Razor / Cut tool\n"
            "X  Crop tool     R  Resize tool  (drag corners/sides; right-click the overlay to reset;\n"
            "   press R again while Resize is active to rotate 90\u00b0)\n"
            "Enter / Esc  While Crop/Resize is active: OK / Cancel\n"
            "Shift+C  Add edit (split) at playhead\n"
            "Ctrl+C / Ctrl+V  Copy / paste the selected timeline clip\n"
            "Delete  Remove selected Project item, or ripple-delete selected timeline clip\n"
            "F2  Rename the selected Project file (on disk)\n"
            "Ctrl+Z / Ctrl+Shift+Z  Undo / redo\n"
            "Ctrl+I  Import      Ctrl+M  Export      Ctrl+S  Save-Over\n\n"
            "Drag a clip's edge on the timeline to trim it, drag its body to reorder it.\n"
            "Ctrl+wheel to zoom, wheel to scroll, click the ruler to scrub.\n"
            "Drop files onto the Project panel or straight onto the timeline.\n\n"
            "The star on a Project item marks it as the PRIMARY file - click any other "
            "star to change it. Save-Over always overwrites the primary file.\n\n"
            "Snap keeps trims on keyframes so the preview always matches a fully lossless export.\n"
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
            src_for_probe = p
            if is_img:
                clip_path = image_to_clip(p, dur=IMAGE_MAX_DUR)
                if not clip_path:
                    self.statusBar().showMessage(f"Could not import image: {os.path.basename(p)}", 6000)
                    continue
                src_for_probe = clip_path
            m = probe_media(src_for_probe)
            if not m:
                self.statusBar().showMessage(f"Could not read: {os.path.basename(p)}", 6000)
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
                       f"(This only removes it from QuickCut's list - the file on disk is not touched.)")
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
        r = QMessageBox.question(
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
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.stage.b_ok.click()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.stage.b_cancel.click()
                event.accept()
                return
        if key == Qt.Key.Key_Space:
            if event.isAutoRepeat():
                event.accept()
                return
            if not self._tool_locked():
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
        if key == Qt.Key.Key_Space:
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
        bounds = self.seq.starts() + [self.seq.total()]
        t = self.engine.playhead
        if d < 0:
            c = [b for b in bounds if b < t - 0.02]
            self.engine.seek(max(c) if c else 0.0, play=False)
        else:
            c = [b for b in bounds if b > t + 0.02]
            self.engine.seek(min(c) if c else self.seq.total(), play=False)

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
        if self.app_mode == "GIF":
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
        gif = self.app_mode == "GIF"
        transcode = self.app_mode == "Video" and self.hb_check.isChecked()
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
        seg = self.seq.segs[idx]
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

    # [MAP] Alt+Left/Right: swap the selected clip with its neighbour through Sequence.edit, keep the selection on it,
    # seek to its start.
    def move_clip(self, d):
        """Alt+Left / Alt+Right: swap the selected clip with its neighbour (no mouse needed)."""
        i = self.tl.sel
        j = i + d
        segs = self.seq.segs
        if not (0 <= i < len(segs) and 0 <= j < len(segs)):
            return
        self.engine.pause()

        def do():
            segs[i], segs[j] = segs[j], segs[i]
            return True
        self.seq.edit(do)
        self.tl.sel = j
        self.engine.seek(self.seq.starts()[j], play=False)
        self.tl.update()

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
        show = on and self.app_mode != "GIF"
        self.hb_combo.setVisible(show)
        self.hb_cog.setVisible(show)
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
            folder = os.path.dirname(parts[0][0].path)
            n = 1
            while True:
                cand = os.path.join(folder, f"edit{n}.gif")
                if not os.path.exists(cand):
                    out = cand
                    break
                n += 1
        else:
            out, _ = QFileDialog.getSaveFileName(self, "Export GIF", base + "_edit.gif", "GIF (*.gif)")
            if not out:
                return
        if not out.lower().endswith(".gif"):
            out += ".gif"

        def done(ok, msg):
            if ok:
                note = ""
                if preset["lossy"] > 0 and not find_tool("gifsicle"):
                    note = "\n\n(gifsicle not found - lossy was approximated. Put gifsicle.exe next to QuickCut for true lossy compression.)"
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
        if name == self.app_mode:
            self.titlebar.set_mode(self.app_mode)
            return
        keep = True
        SB = QMessageBox.StandardButton
        if name == "GIF" and self.seq.segs:
            r = QMessageBox.question(self, "Switch to GIF mode",
                                     "Switch to GIF mode?\n\nKeep the current timeline?\n"
                                     "Yes = keep it, No = clear it (your files stay in the Project panel).",
                                     SB.Yes | SB.No | SB.Cancel, SB.Yes)
            if r == SB.Cancel:
                self.titlebar.set_mode(self.app_mode)         # put the highlight back
                return
            keep = r == SB.Yes
        elif name != "GIF":
            r = QMessageBox.question(self, f"Switch to {name} mode", "Switch to Video mode?", SB.Yes | SB.No, SB.No)
            if r != SB.Yes:
                self.titlebar.set_mode(self.app_mode)
                return
        self.app_mode = name
        self.titlebar.set_mode(name)
        self.gif_combo.setVisible(name == "GIF")
        self.gif_cog.setVisible(name == "GIF")
        self.saveover_btn.setVisible(name != "GIF")
        self.hb_check.setVisible(name != "GIF")
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
        was_empty = not self.seq.segs
        t = self.engine.playhead if t is None else t
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
        self.engine.seek(result.get("end", 0.0), play=False)

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

    def paste_clip(self):
        if not self.clipboard_seg:
            self.statusBar().showMessage("Nothing to paste - select a clip and press Ctrl+C first.", 4000)
            return
        media, a, b, xf, mute, speed, mirror, rot, rev, atracks, vol_db, track_type = self.clipboard_seg
        t = self.engine.playhead
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
        self.engine.seek(result["end"], play=False)
        self.statusBar().showMessage("Pasted clip", 3000)

    # ------------------------------------------------------------------ export / save-over
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
        if self.app_mode == "GIF":
            return self.export_gif(parts)
        self.engine.pause()
        first = parts[0][0]
        base = os.path.splitext(first.path)[0]
        transcode = self.hb_combo.currentData() if self.hb_check.isChecked() else None
        ext = ("." + transcode["format"]) if transcode else os.path.splitext(first.path)[1]
        filt = f"Video (*{ext})" if transcode else f"Video (*{ext});;All files (*.*)"
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            folder = os.path.dirname(first.path)
            n = 1
            while True:
                cand = os.path.join(folder, f"edit{n}{ext}")
                if not os.path.exists(cand):
                    out = cand
                    break
                n += 1
        else:
            out, _ = QFileDialog.getSaveFileName(self, "Export sequence", base + "_edit" + ext, filt)
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
        if self.seq.segs:
            r = QMessageBox.question(self, "Close QuickCut", "There are clips on the timeline.\n\nClose the program anyway?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.engine.player.stop()
        self.engine.player.setSource(QUrl())
        self.proxy.shutdown()
        super().closeEvent(e)


# [MAP] Entry point: Fusion style + dark palette + QSS, then MainWindow with any existing file paths from argv
# (imported on the first event-loop turn). Run:  python QuickCut.py [video files...]
