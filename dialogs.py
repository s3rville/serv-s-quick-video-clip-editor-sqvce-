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
                               QColorDialog, QStyle)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget, QGraphicsVideoItem

from utils import *
from media_model import *
from probing import *
from export_worker import *

# ----------------------------------------------------------------------------- icons
_ICON_CACHE = {}


def _star_points(cx, cy, r_out, r_in, n=5, rot=-90):
    pts = []
    for i in range(n * 2):
        ang = math.radians(rot + i * 360 / (n * 2))
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


# [MAP] All toolbar/title-bar icons are drawn in code (no image files). Pixmap is rendered at 2x with
# devicePixelRatio 2 for crisp HiDPI, and cached by (name, colour, size).
# [PITFALL] There is no `else`: an UNKNOWN icon name returns a blank icon without any error. To add an icon, add
# an `elif name == ...` branch drawing on a 20x20 grid (the painter is pre-scaled 2x). Icons currently used:
# play pause step_back step_fwd prev_edit next_edit select razor crop resize rotate90 star camera win_min
# win_max win_restore win_close x_small.
def icon(name, color="#d4d4d4", size=20):
    key = (name, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(2, 2)
    c = QColor(color)
    p.setPen(QPen(c, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(c)

    def poly(pts):
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))

    def line(pts):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))

    if name == "play":
        poly([(6, 4), (6, 16), (16, 10)])
    elif name == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(5, 4, 3.6, 12))
        p.drawRect(QRectF(11.4, 4, 3.6, 12))
    elif name == "step_back":
        line([(13, 4), (7, 10), (13, 16)])
    elif name == "step_fwd":
        line([(7, 4), (13, 10), (7, 16)])
    elif name == "prev_edit":
        p.drawRect(QRectF(4, 4, 2, 12))
        poly([(16, 4), (16, 16), (8, 10)])
    elif name == "next_edit":
        p.drawRect(QRectF(14, 4, 2, 12))
        poly([(4, 4), (4, 16), (12, 10)])
    elif name == "select":
        p.setPen(Qt.PenStyle.NoPen)
        poly([(5, 3), (5, 16), (8.5, 12.8), (11, 18), (13.2, 17), (10.7, 11.8), (15, 11.4)])
    elif name == "razor":
        p.setPen(Qt.PenStyle.NoPen)
        poly([(3, 14.5), (11.5, 3), (14, 4.6), (6.5, 16.5)])
        p.drawRect(QRectF(12, 12.5, 5, 2))
    elif name == "crop":
        line([(6, 2.5), (6, 14), (17.5, 14)])
        line([(2.5, 6), (14, 6), (14, 17.5)])
    elif name == "resize":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(3.5, 9.5, 7, 7))
        line([(9, 11), (16, 4)])
        line([(11, 4), (16, 4), (16, 9)])
    elif name == "rotate90":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(4, 4, 12, 12), 40 * 16, 270 * 16)
        p.setBrush(c)
        poly([(13.6, 1.8), (17.6, 5.8), (12.2, 6.8)])
    elif name == "star":
        p.setPen(Qt.PenStyle.NoPen)
        poly(_star_points(10, 10, 8, 3.3))
    elif name == "camera":
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(3, 6.5, 14, 9.5), 2, 2)
        p.drawRoundedRect(QRectF(7.5, 3, 5, 4), 1, 1)
        p.setBrush(QColor("#1c1c1c"))
        p.drawEllipse(QPointF(10, 11.3), 3.3, 3.3)
        p.setBrush(c)
        p.drawEllipse(QPointF(10, 11.3), 1.5, 1.5)
    elif name == "win_min":
        p.setBrush(Qt.BrushStyle.NoBrush)
        line([(4, 10), (16, 10)])
    elif name == "win_max":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(4.5, 4.5, 11, 11))
    elif name == "win_restore":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(6.5, 3.5, 9, 9))
        p.drawLine(QLineF(4.5, 6.5, 4.5, 15.5))
        p.drawLine(QLineF(4.5, 15.5, 13.5, 15.5))
        p.drawLine(QLineF(13.5, 15.5, 13.5, 12.5))
    elif name == "win_close":
        p.setBrush(Qt.BrushStyle.NoBrush)
        line([(5, 5), (15, 15)])
        line([(15, 5), (5, 15)])
    elif name == "x_small":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        line([(6, 6), (14, 14)])
        line([(14, 6), (6, 14)])
    p.end()
    pm.setDevicePixelRatio(2)
    ic = QIcon(pm)
    _ICON_CACHE[key] = ic
    return ic


# ----------------------------------------------------------------------------- per-clip options
# [MAP] QSlider that emits doubleClicked (used by ClipOptionsDialog to snap the speed back to 1x).
# [FEATURE] Also jumps straight to wherever the mouse is pressed/dragged (Qt's default groove-click only
# pages by a step) so the speed slider is precise to click on; the double-click-to-reset above still works
# since mouseDoubleClickEvent is handled separately and always wins on a real double-click.
class DblSlider(QSlider):
    doubleClicked = Signal()

    def _val_from_x(self, x):
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x), self.width())

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._val_from_x(e.position().x()))
            self.setSliderDown(True)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setValue(self._val_from_x(e.position().x()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()
        e.accept()


# [MAP] Modal dialog opened by double-clicking a clip (Timeline.clipOptionsRequested -> MainWindow.
# edit_clip_options). Organized into a "Video" section (speed, mirror, reverse) and an "Audio" section (mute,
# per-track checkboxes, volume, track type). Rotation is NOT here (the 90-degree button in the Resize tool bar
# does it). The speed slider is logarithmic 0.25x..10x (_s2v/_v2s) with a snap-to-1x zone; the spin box accepts
# 0.05x..100x and always wins.
# [KNOWN ISSUE F6] The spin box allows up to 100x but Engine._apply_fx clamps preview playback to 20x, so above
# 20x the preview desynchronises (playhead crawls, "can't keep up" warning fires). Export is unaffected.
# [COUPLING] values() returns (mute, speed, mirror, rev, atracks, vol_db, track_type) in that order -
# edit_clip_options unpacks it positionally. Checkbox is labelled "Reverse playback (Max 30 seconds)": the cap
# only limits the live PREVIEW proxy; export is not capped (details: ReverseProxy).
# [KNOWN ISSUE] atracks / vol_db / track_type are EXPORT-only - Engine._apply_fx previews `vol_db` (best-effort;
# QAudioOutput can't exceed unity gain so a positive value clamps in the live preview only) but does not preview
# individual track selection or the mono downmix; the live preview always plays the source's default audio.
class ClipOptionsDialog(QDialog):
    """Double-click a clip: Video (speed/mirror/reverse) + Audio (mute/tracks/volume/track type)."""

    @staticmethod
    def _s2v(x):
        return round(0.25 * 40 ** (x / 1000.0), 2)

    @staticmethod
    def _v2s(v):
        return int(round(1000 * math.log(max(min(v, 10.0), 0.25) / 0.25) / math.log(40)))

    def __init__(self, parent, name, mute, speed, has_audio, mirror, has_video, rev=False,
                 audio_streams=None, atracks=None, vol_db=0.0, track_type="stereo"):
        super().__init__(parent)
        self.setWindowTitle("Clip options")
        self.setModal(True)
        self.setMinimumWidth(360)
        audio_streams = audio_streams or []
        n_tracks = max(1, len(audio_streams))
        atracks = list(atracks) if atracks is not None else [True] * n_tracks
        v = QVBoxLayout(self)
        v.setSpacing(8)
        t = QLabel(self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 330))
        t.setStyleSheet("color:#eaeaea;font-weight:600;")
        v.addWidget(t)

        # ---------------------------------------------------------------- Video section
        vid_box = QFrame()
        vid_box.setObjectName("optSection")
        vid_lay = QVBoxLayout(vid_box)
        vid_lay.setContentsMargins(10, 8, 10, 10)
        vid_lay.setSpacing(6)
        hdr = QLabel("VIDEO")
        hdr.setStyleSheet("color:#9aa4b2;font-weight:700;font-size:10px;letter-spacing:1px;")
        vid_lay.addWidget(hdr)
        vid_lay.addWidget(QLabel("Speed"))
        row = QHBoxLayout()
        self.slider = DblSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setToolTip("Double-click to reset to 1x")
        self.spin = QDoubleSpinBox()
        self.spin.setRange(0.05, 100.0)
        self.spin.setDecimals(2)
        self.spin.setSingleStep(0.05)
        self.spin.setSuffix("x")
        self.spin.setFixedWidth(84)
        self.spin.setToolTip("Type any speed - overrides the slider")
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        vid_lay.addLayout(row)
        scale = QHBoxLayout()
        scale.setContentsMargins(0, 0, 90, 0)
        for txt in ("0.25x", "1x", "10x"):
            scale.addWidget(QLabel(txt))
            if txt != "10x":
                scale.addStretch(1)
        vid_lay.addLayout(scale)
        self.mirror = QCheckBox("Mirror video (flip horizontally)")
        self.mirror.setChecked(bool(mirror) and has_video)
        self.mirror.setEnabled(has_video)
        vid_lay.addWidget(self.mirror)
        self.rev = QCheckBox("Reverse playback (Max 30 seconds)")
        self.rev.setChecked(bool(rev) and has_video)
        self.rev.setEnabled(has_video)
        self.rev.setToolTip("Clip plays backwards. Live preview works for clips up to 30 s (a preview copy is\n"
                            "rendered in the background); longer clips still reverse on export.")
        vid_lay.addWidget(self.rev)
        v.addWidget(vid_box)

        # ---------------------------------------------------------------- Audio section
        aud_box = QFrame()
        aud_box.setObjectName("optSection")
        aud_lay = QVBoxLayout(aud_box)
        aud_lay.setContentsMargins(10, 8, 10, 10)
        aud_lay.setSpacing(6)
        ahdr = QLabel("AUDIO")
        ahdr.setStyleSheet("color:#9aa4b2;font-weight:700;font-size:10px;letter-spacing:1px;")
        aud_lay.addWidget(ahdr)
        self.mute = QCheckBox("Mute audio" + ("" if has_audio else "  (no audio track)"))
        self.mute.setChecked(bool(mute) and has_audio)
        self.mute.setEnabled(has_audio)
        aud_lay.addWidget(self.mute)

        aud_lay.addWidget(QLabel("Audio Tracks"))
        self.tracks = QListWidget()
        self.tracks.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tracks.setMaximumHeight(96)                 # a few rows visible; scrolls for more (per-clip; visual only, doesn't affect export beyond which tracks get mixed in)
        self.tracks.setEnabled(has_audio)
        for i in range(n_tracks):
            nm = (audio_streams[i].get("name") if i < len(audio_streams) else None) or f"Audio Track {i + 1}"
            it = QListWidgetItem(nm)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if (i < len(atracks) and atracks[i]) else Qt.CheckState.Unchecked)
            self.tracks.addItem(it)
        aud_lay.addWidget(self.tracks)

        aud_lay.addWidget(QLabel("Volume"))
        vol_row = QHBoxLayout()
        self.vol_slider = DblSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(-20, 20)       # [51.6] slider back to +-20 dB; the spin box beside it has no such limit
        self.vol_slider.setValue(max(-20, min(20, int(round(vol_db)))))
        self.vol_slider.setEnabled(has_audio)
        self.vol_slider.setToolTip("Double-click to reset to +0 dB")
        # [FEATURE 51.3] Type any gain in dB - beyond the slider's range too (slider just pins at its end).
        self.vol_spin = QDoubleSpinBox()
        self.vol_spin.setRange(-200.0, 200.0)
        self.vol_spin.setDecimals(1)
        self.vol_spin.setSingleStep(1.0)
        self.vol_spin.setSuffix(" dB")
        self.vol_spin.setFixedWidth(84)
        self.vol_spin.setEnabled(has_audio)
        self.vol_spin.setKeyboardTracking(False)
        self.vol_spin.setValue(float(vol_db))
        vol_row.addWidget(self.vol_slider, 1)
        vol_row.addWidget(self.vol_spin)
        aud_lay.addLayout(vol_row)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Track Type"))
        self.track_type = QComboBox()
        self.track_type.addItems(["Stereo", "Mono"])
        self.track_type.setCurrentIndex(1 if track_type == "mono" else 0)
        self.track_type.setEnabled(has_audio)
        type_row.addWidget(self.track_type, 1)
        aud_lay.addLayout(type_row)
        v.addWidget(aud_box)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_reset, b_c, b_ok = QPushButton("Reset"), QPushButton("Cancel"), QPushButton("OK")
        b_ok.setDefault(True)
        for b in (b_reset, b_c, b_ok):
            btns.addWidget(b)
        v.addLayout(btns)
        self.spin.setValue(speed)
        self.slider.setValue(self._v2s(speed))
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.doubleClicked.connect(lambda: self.spin.setValue(1.0))
        self.spin.valueChanged.connect(self._on_spin)
        self.vol_slider.valueChanged.connect(self._on_vol)
        self.vol_spin.valueChanged.connect(self._on_vol_spin)
        self.vol_slider.doubleClicked.connect(lambda: self.vol_slider.setValue(0))
        b_reset.clicked.connect(self._reset)
        b_c.clicked.connect(self.reject)
        b_ok.clicked.connect(self.accept)

    # [MAP] Centres the dialog on the program window (the window is frameless, so the OS would centre on screen).
    def showEvent(self, e):
        super().showEvent(e)
        w = self.parentWidget().window() if self.parentWidget() else None
        if w is not None:                                   # centre on the program window
            self.move(w.frameGeometry().center() - QPoint(self.width() // 2, self.height() // 2))

    def _on_slider(self, x):
        v = self._s2v(x)
        if abs(v - 1.0) < 0.03:
            v = 1.0
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)

    def _on_spin(self, val):
        self.slider.blockSignals(True)
        self.slider.setValue(self._v2s(val))
        self.slider.blockSignals(False)

    def _on_vol(self, val):
        self.vol_spin.blockSignals(True)
        self.vol_spin.setValue(float(val))
        self.vol_spin.blockSignals(False)

    def _on_vol_spin(self, val):
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(int(round(max(-20.0, min(20.0, val)))))
        self.vol_slider.blockSignals(False)

    def _reset(self):
        self.spin.setValue(1.0)
        self.mute.setChecked(False)
        self.mirror.setChecked(False)
        self.rev.setChecked(False)
        for i in range(self.tracks.count()):
            self.tracks.item(i).setCheckState(Qt.CheckState.Checked)
        self.vol_slider.setValue(0)
        self.vol_spin.setValue(0.0)
        self.track_type.setCurrentIndex(0)

    def values(self):
        atracks = tuple(self.tracks.item(i).checkState() == Qt.CheckState.Checked for i in range(self.tracks.count()))
        track_type = "mono" if self.track_type.currentIndex() == 1 else "stereo"
        return (self.mute.isChecked(), round(self.spin.value(), 2), self.mirror.isChecked(), self.rev.isChecked(),
                atracks, round(float(self.vol_spin.value()), 2), track_type)


# ----------------------------------------------------------------------------- custom title bar
GIF_BUILTIN = [(240, 15, 30, 200), (240, 24, 20, 256), (360, 15, 20, 200), (360, 24, 20, 256),
               (360, 30, 15, 256), (480, 15, 20, 200), (480, 24, 15, 256), (480, 30, 15, 256),
               (640, 15, 15, 256), (640, 24, 10, 256), (720, 24, 0, 256), (480, 30, 0, 256)]


# [MAP] GIF preset dict = {w, fps, lossy, colors[, name]}. Display title = name if present, else the automatic
# "x{w}p-{fps}fps-lossy{n}-{c}bit". The title (text) is what is stored in QSettings key "gif_sel" and looked up
# again with findText - so RENAMING a preset title makes the saved selection fall back to index 0.
def gif_title(p):
    return p.get("name") or f"x{p['w']}p-{p['fps']}fps-lossy{p['lossy']}-{p['colors']}bit"


# [MAP] Built-in presets = one named default first ("Default (x480p-24fps-lossy20-200bit)", selected on first
# run because it is index 0) followed by the GIF_BUILTIN grid. Built-ins are never written to settings; only
# user presets live in QSettings key "gif_custom" (JSON list). Keep index 0 as the default.
def gif_builtin():
    return [{"name": "Default (x480p-24fps-lossy20-200bit)", "w": 480, "fps": 24, "lossy": 20, "colors": 200}] + [{"w": w, "fps": f, "lossy": l, "colors": c} for w, f, l, c in GIF_BUILTIN]


# [MAP] Cog dialog: add / update / delete USER GIF presets. Works on a copy (self.customs) and MainWindow.
# edit_gif_presets persists whatever `customs` holds when the dialog closes - even after Close/Escape, there is
# no Cancel. Built-in presets are not editable.
class GifPresetDialog(QDialog):
    """Cog menu: add / update / delete your own GIF presets (built-in ones stay untouched)."""

    def __init__(self, parent, customs):
        super().__init__(parent)
        self.setWindowTitle("GIF presets")
        self.setMinimumWidth(360)
        self.customs = [dict(c) for c in customs]
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Your presets (built-in presets can't be edited):"))
        self.list = QListWidget()
        self.list.setFixedHeight(120)
        v.addWidget(self.list)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name (leave blank for automatic title)")
        v.addWidget(self.name)
        row = QHBoxLayout()
        self.sp = {}
        for key, lab, lo, hi, val, suf in (("w", "Width", 16, 4096, 480, " px"), ("fps", "FPS", 1, 60, 24, ""),
                                           ("lossy", "Lossy", 0, 200, 20, ""), ("colors", "Colors", 2, 256, 256, "")):
            col = QVBoxLayout()
            col.addWidget(QLabel(lab))
            b = QSpinBox()
            b.setRange(lo, hi)
            b.setValue(val)
            b.setSuffix(suf)
            col.addWidget(b)
            row.addLayout(col)
            self.sp[key] = b
        v.addLayout(row)
        self.hint = QLabel("Width only - height scales automatically. Lossy 0 = off.")
        self.hint.setStyleSheet("color:#8f8f8f;")
        v.addWidget(self.hint)
        btns = QHBoxLayout()
        self.b_add, self.b_upd, self.b_del, b_close = (QPushButton(t) for t in ("Add", "Update", "Delete", "Close"))
        for b in (self.b_add, self.b_upd, self.b_del):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(b_close)
        v.addLayout(btns)
        self.b_add.clicked.connect(self._add)
        self.b_upd.clicked.connect(self._upd)
        self.b_del.clicked.connect(self._del)
        b_close.clicked.connect(self.accept)
        self.list.currentRowChanged.connect(self._pick)
        self._fill()

    def _fill(self, row=-1):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([gif_title(c) for c in self.customs])
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self.b_upd.setEnabled(row >= 0)
        self.b_del.setEnabled(row >= 0)

    def _pick(self, r):
        self.b_upd.setEnabled(r >= 0)
        self.b_del.setEnabled(r >= 0)
        if 0 <= r < len(self.customs):
            c = self.customs[r]
            self.name.setText(c.get("name", ""))
            for k, b in self.sp.items():
                b.setValue(int(c[k]))

    def _read(self):
        p = {k: b.value() for k, b in self.sp.items()}
        if self.name.text().strip():
            p["name"] = self.name.text().strip()
        return p

    def _add(self):
        self.customs.append(self._read())
        self._fill(len(self.customs) - 1)

    def _upd(self):
        r = self.list.currentRow()
        if r >= 0:
            self.customs[r] = self._read()
            self._fill(r)

    def _del(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.customs[r]
            self._fill(min(r, len(self.customs) - 1))


# ----------------------------------------------------------------------------- HandBrake-lite (video transcode)
# A deliberately small subset of HandBrake's controls: Web Optimized, Format (MP4/WEBM),
# Video Encoder (x264 / NVENC H264 / VP9), Constant Quality *or* Avg Bitrate, and FPS.
# Only used when the user checks "Transcode"; otherwise the existing lossless pipeline runs.
# Bitrates for the Social/Creator presets are our own ballpark figures for the stated target
# size+duration (HandBrake doesn't publish exact internal values) - close enough for a "basic"
# clone; users can dial in exact numbers via a custom preset.
# [MAP] Transcode ("HandBrake-lite") data. VIDEO_BUILTIN rows are (name, height, fps, encoder, mode, value,
# format, web_opt). The Creator/Social bitrates are the previous developers' own ballpark figures, NOT
# HandBrake's real internal values - do not treat them as exact target-size guarantees. Only Export uses
# transcode; Save-Over never does.
VIDEO_ENCODERS = [("x264", "H.264 (x264)"), ("nvenc", "H.264 (NVENC)"), ("vp9", "VP9")]

VIDEO_BUILTIN = [
    # name, h, fps, encoder, mode('cq'/'bitrate'), value, format, web_opt
    ("Fast 1080p30", 1080, 30, "x264", "cq", 20, "mp4", True),
    ("Fast 720p30", 720, 30, "x264", "cq", 20, "mp4", True),
    ("Fast 480p30", 480, 30, "x264", "cq", 20, "mp4", True),
    ("Creator 720p60", 720, 60, "x264", "bitrate", 5000, "mp4", True),
    ("Creator 1080p60", 1080, 60, "x264", "bitrate", 8000, "mp4", True),
    ("Creator 1440p60 2.5K", 1440, 60, "x264", "bitrate", 16000, "mp4", True),
    ("Creator 2160p60 4K", 2160, 60, "x264", "bitrate", 35000, "mp4", True),
    ("Social 25 MB 30 Seconds 1080p60", 1080, 60, "x264", "bitrate", 6500, "mp4", True),
    ("Social 25 MB 1 Minute 720p60", 720, 60, "x264", "bitrate", 3200, "mp4", True),
    ("Social 25 MB 2 Minutes 540p60", 540, 60, "x264", "bitrate", 1500, "mp4", True),
    ("Social 25 MB 5 Minutes 360p60", 360, 60, "x264", "bitrate", 500, "mp4", True),
    ("Social 10 MB 30 Seconds 720p30", 720, 30, "x264", "bitrate", 2500, "mp4", True),
    ("Social 10 MB 1 Minute 540p30", 540, 30, "x264", "bitrate", 1200, "mp4", True),
    ("Social 10 MB 2 Minutes 360p30", 360, 30, "x264", "bitrate", 500, "mp4", True),
]


# [MAP] Preset display title: explicit name, else "{h}p{fps}-{encoder}-cq{v}|{kbps}kbps". Stored in QSettings
# "hb_sel" by TEXT, like the GIF presets.
def hb_title(p):
    if p.get("name"):
        return p["name"]
    q = f"cq{p['value']:g}" if p["mode"] == "cq" else f"{p['value']:g}kbps"
    return f"{p['h']}p{p['fps']:g}-{p['encoder']}-{q}"


def hb_builtin():
    return [{"name": n, "h": h, "fps": f, "encoder": e, "mode": m, "value": v, "format": fmt, "web_opt": w}
            for n, h, f, e, m, v, fmt, w in VIDEO_BUILTIN]


# [MAP] Cog dialog for USER Transcode presets (same pattern as GifPresetDialog): format mp4/webm, encoder
# x264/nvenc/vp9, height (0 = Source), fps (0 = Source), constant quality (0-51) OR average bitrate (kbps),
# Web Optimized. Preset dict keys: {name?, format, encoder, mode 'cq'|'bitrate', value, h, fps, web_opt}.
# MainWindow.hb_customs() silently drops stored presets that lack any of h/fps/encoder/mode/value/format.
class HbPresetDialog(QDialog):
    """Cog menu: add / update / delete your own Transcode presets (built-ins can't be edited)."""

    def __init__(self, parent, customs):
        super().__init__(parent)
        self.setWindowTitle("Transcode presets")
        self.setMinimumWidth(400)
        self.customs = [dict(c) for c in customs]
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Your presets (built-in presets can't be edited):"))
        self.list = QListWidget()
        self.list.setFixedHeight(110)
        v.addWidget(self.list)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name (leave blank for automatic title)")
        v.addWidget(self.name)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Format"))
        self.fmt = QComboBox()
        self.fmt.addItems(["mp4", "webm"])
        row1.addWidget(self.fmt)
        row1.addWidget(QLabel("Encoder"))
        self.enc = QComboBox()
        for key, lab in VIDEO_ENCODERS:
            self.enc.addItem(lab, key)
        row1.addWidget(self.enc)
        self.web_opt = QCheckBox("Web Optimized")
        self.web_opt.setChecked(True)
        row1.addWidget(self.web_opt)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Height"))
        self.h = QSpinBox()
        self.h.setRange(0, 4320)
        self.h.setValue(1080)
        self.h.setSpecialValueText("Source")
        row2.addWidget(self.h)
        row2.addWidget(QLabel("FPS"))
        self.fps = QSpinBox()
        self.fps.setRange(0, 120)
        self.fps.setValue(30)
        self.fps.setSpecialValueText("Source")
        row2.addWidget(self.fps)
        v.addLayout(row2)

        self.use_bitrate = QCheckBox("Use avg bitrate (kbps) instead of quality")
        self.use_bitrate.toggled.connect(self._toggle_mode)
        v.addWidget(self.use_bitrate)
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Constant quality"))
        self.cq = QSlider(Qt.Orientation.Horizontal)
        self.cq.setRange(0, 51)
        self.cq.setValue(20)
        self.cq_val = QSpinBox()
        self.cq_val.setRange(0, 51)
        self.cq_val.setValue(20)
        self.cq.valueChanged.connect(self.cq_val.setValue)
        self.cq_val.valueChanged.connect(self.cq.setValue)
        row4.addWidget(self.cq)
        row4.addWidget(self.cq_val)
        v.addLayout(row4)
        row5 = QHBoxLayout()
        row5.addWidget(QLabel("Avg bitrate"))
        self.bitrate = QSpinBox()
        self.bitrate.setRange(100, 100000)
        self.bitrate.setValue(6000)
        self.bitrate.setSuffix(" kbps")
        row5.addWidget(self.bitrate)
        row5.addStretch(1)
        v.addLayout(row5)
        self._toggle_mode(False)

        btns = QHBoxLayout()
        self.b_add, self.b_upd, self.b_del, b_close = (QPushButton(t) for t in ("Add", "Update", "Delete", "Close"))
        for b in (self.b_add, self.b_upd, self.b_del):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(b_close)
        v.addLayout(btns)
        self.b_add.clicked.connect(self._add)
        self.b_upd.clicked.connect(self._upd)
        self.b_del.clicked.connect(self._del)
        b_close.clicked.connect(self.accept)
        self.list.currentRowChanged.connect(self._pick)
        self._fill()

    def _toggle_mode(self, on):
        self.cq.setEnabled(not on)
        self.cq_val.setEnabled(not on)
        self.bitrate.setEnabled(on)

    def _fill(self, row=-1):
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([hb_title(c) for c in self.customs])
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self.b_upd.setEnabled(row >= 0)
        self.b_del.setEnabled(row >= 0)

    def _pick(self, r):
        self.b_upd.setEnabled(r >= 0)
        self.b_del.setEnabled(r >= 0)
        if 0 <= r < len(self.customs):
            c = self.customs[r]
            self.name.setText(c.get("name", ""))
            self.fmt.setCurrentText(c.get("format", "mp4"))
            i = self.enc.findData(c.get("encoder", "x264"))
            self.enc.setCurrentIndex(max(0, i))
            self.web_opt.setChecked(bool(c.get("web_opt", True)))
            self.h.setValue(int(c.get("h", 1080)))
            self.fps.setValue(int(c.get("fps", 30)))
            bitrate_mode = c.get("mode") == "bitrate"
            self.use_bitrate.setChecked(bitrate_mode)
            if bitrate_mode:
                self.bitrate.setValue(int(c["value"]))
            else:
                self.cq_val.setValue(int(c["value"]))

    def _read(self):
        p = {"format": self.fmt.currentText(), "encoder": self.enc.currentData(),
             "web_opt": self.web_opt.isChecked(), "h": self.h.value(), "fps": self.fps.value(),
             "mode": "bitrate" if self.use_bitrate.isChecked() else "cq",
             "value": self.bitrate.value() if self.use_bitrate.isChecked() else self.cq_val.value()}
        if self.name.text().strip():
            p["name"] = self.name.text().strip()
        return p

    def _add(self):
        self.customs.append(self._read())
        self._fill(len(self.customs) - 1)

    def _upd(self):
        r = self.list.currentRow()
        if r >= 0:
            self.customs[r] = self._read()
            self._fill(r)

    def _del(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.customs[r]
            self._fill(min(r, len(self.customs) - 1))


# [MAP] Four-arrow handle in the "Export finished" dialog: press-and-drag starts a QDrag carrying the exported
# file's URL (drop it into Explorer, a browser, a chat window...). Drag starts after 6 px (manhattan) movement.
class DragFileButton(QToolButton):
    """Four-way-arrow handle: press and drag it to drop the exported file anywhere (Explorer, chat, browser...)."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._p0 = None
        self.setFixedSize(38, 38)
        self.setToolTip("Drag this onto a folder, chat or website to copy the file there")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        pm = QPixmap(48, 48)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor("#e0e0e0")
        q.setPen(QPen(c, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        q.drawLine(QLineF(24, 8, 24, 40))
        q.drawLine(QLineF(8, 24, 40, 24))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(c)
        for pts in (((24, 3), (18, 11), (30, 11)), ((24, 45), (18, 37), (30, 37)),
                    ((3, 24), (11, 18), (11, 30)), ((45, 24), (37, 18), (37, 30))):
            q.drawPolygon(QPolygonF([QPointF(*a) for a in pts]))
        q.end()
        self.setIcon(QIcon(pm))
        self.setIconSize(QSize(26, 26))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._p0 = e.position()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._p0 is not None and (e.buttons() & Qt.MouseButton.LeftButton) \
                and (e.position() - self._p0).manhattanLength() > 6:
            self._p0 = None
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(self.path)])
            d = QDrag(self)
            d.setMimeData(md)
            d.setPixmap(self.icon().pixmap(32, 32))
            d.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._p0 = None
        super().mouseReleaseEvent(e)


# [MAP] Result dialog shown by export/export_gif: drag handle, "Open folder" (select-in-Explorer on Windows,
# `open -R` on macOS, folder URL elsewhere) and "Open file". `note` carries the gifsicle-missing hint.
class ExportDoneDialog(QDialog):
    """Export finished: drag the file out, open its folder, open the file. Closes only on OK / X."""

    def __init__(self, parent, path, note=""):
        super().__init__(parent)
        self.setWindowTitle("Export finished")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.path = path
        v = QVBoxLayout(self)
        v.setSpacing(10)
        lab = QLabel(f"Saved:\n{path}{note}")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(lab)
        row = QHBoxLayout()
        row.addWidget(DragFileButton(path))
        b_dir, b_file = QPushButton("Open folder"), QPushButton("Open file")
        b_dir.clicked.connect(self._folder)
        b_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.path)))
        row.addWidget(b_dir)
        row.addWidget(b_file)
        row.addStretch(1)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        v.addLayout(row)
        for b in (b_dir, b_file):
            b.setAutoDefault(False)

    def _folder(self):
        p = os.path.normpath(self.path)
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{p}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", p])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))
        except Exception:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))


# [MAP] Custom title bar for the FRAMELESS window (Qt.FramelessWindowHint set in MainWindow.__init__): Video/GIF
# mode buttons on the left, minimise / maximise / close on the right. Moving uses the OS via
# windowHandle().startSystemMove(); double-click toggles maximise. Resizing is done by MainWindow.eventFilter
# (edge hit-testing + startSystemResize) because a frameless window has no native borders.
# [PITFALL] The mode buttons call MainWindow.request_mode(name); the buttons are checkable in an exclusive
# group, so a REFUSED switch must put the highlight back with set_mode(current) (request_mode does).
class TitleBar(QFrame):
    """Replaces the OS title bar: drag-to-move, double-click to maximize, and its own
    minimize / maximize / close buttons. The window itself is created frameless."""

    def __init__(self, window):
        super().__init__()
        self.win = window
        self.setObjectName("titlebar")
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(0)
        self.mode_btns = {}
        grp = self._grp = QButtonGroup(self)
        grp.setExclusive(True)
        for name in ("Video", "GIF"):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFixedHeight(24)
            b.setStyleSheet(self._tab_qss())
            b.clicked.connect(lambda _=False, n=name: window.request_mode(n))
            grp.addButton(b)
            lay.addWidget(b)
            lay.addSpacing(4)
            self.mode_btns[name] = b
        self.mode_btns["Video"].setChecked(True)
        self._lay, self._plug_at, self.plug_tabs = lay, lay.count(), []
        lay.addStretch(1)
        self.min_btn = self._btn("win_min", "Minimize", window.showMinimized)
        self.max_btn = self._btn("win_max", "Maximize", window.toggle_maximize)
        self.close_btn = self._btn("win_close", "Close", window.close)
        self.close_btn.setObjectName("closeBtn")
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)
        self._drag_pos = None
        # project name: small grey label centred in the bar (same row as the Video / GIF tabs); mouse-transparent
        self.name_label = QLabel(APP_NAME, self)
        self.name_label.setStyleSheet("color:#6f6f6f;font-size:8pt;background:transparent;")
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.name_label.adjustSize()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        nl = self.name_label
        nl.move((self.width() - nl.width()) // 2, (self.height() - nl.height()) // 2)

    def _tab_qss(self):
        return ("QPushButton{background:transparent;border:1px solid transparent;border-radius:3px;"
                "padding:0 16px;font-weight:700;color:#9a9a9a;}"
                "QPushButton:hover:!checked{background:#2e2e2e;color:#e0e0e0;}"
                f"QPushButton:checked{{background:{theme_map()['accent']};color:#ffffff;}}")

    def restyle(self):
        for b in list(self.mode_btns.values()) + self.plug_tabs:
            b.setStyleSheet(self._tab_qss())

    def add_tab(self, name, slot):
        """[52.0] Mod tab button (same look/group as Video / GIF)."""
        b = QPushButton(name)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setFixedHeight(24)
        b.setStyleSheet(self._tab_qss())
        b.clicked.connect(lambda _=False: slot())
        self._grp.addButton(b)
        self._lay.insertWidget(self._plug_at + 2 * len(self.plug_tabs), b)
        self._lay.insertSpacing(self._plug_at + 2 * len(self.plug_tabs) + 1, 4)
        self.plug_tabs.append(b)
        return b

    def remove_tab(self, b):
        i = self._lay.indexOf(b)
        self._grp.removeButton(b)
        self._lay.removeWidget(b)
        if i >= 0 and self._lay.itemAt(i) is not None and self._lay.itemAt(i).spacerItem() is not None:
            self._lay.takeAt(i)
        self.plug_tabs.remove(b)
        b.deleteLater()
        self.mode_btns["Video" if self.win.app_mode == "Video" else "GIF"].setChecked(True)

    def set_mode(self, name):
        self.mode_btns[name].setChecked(True)

    def _btn(self, name, tip, slot):
        b = QToolButton()
        b.setObjectName("winBtn")
        b.setIcon(icon(name, "#c8c8c8", size=16))
        b.setIconSize(QSize(16, 16))
        b.setFixedSize(44, 34)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda _=False: slot())
        return b

    def set_maximized(self, is_max):
        self.max_btn.setIcon(icon("win_restore" if is_max else "win_max", "#c8c8c8", size=16))
        self.max_btn.setToolTip("Restore" if is_max else "Maximize")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            wh = self.win.windowHandle()
            if wh is not None:
                wh.startSystemMove()
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.toggle_maximize()




# ----------------------------------------------------------------------------- Preferences
# [FEATURE v52.0] Tabs: General (default export folder + file naming scheme), Keybinds (every binding, see utils.KEYBIND_DEFS),
# Interface (theme colours), Plug-ins (mods list), Credits. Footer: RESET (left, asks to confirm) | OK / Cancel / Apply (right).
# Settings are stored via utils.prefs() (pref_* keys); Apply/OK call parent.apply_prefs() (theme + keybinds + mods reload).
class PreferencesDialog(QDialog):
    PAGES = ("General", "Keybinds", "Interface", "Plug-ins", "Credits")
    PREF_KEYS = ("pref_export_dir_on", "pref_export_dir", "pref_name_scheme", "pref_keybinds", "pref_theme", "pref_mods_on", "pref_mods_off", "pref_mods_order", "pref_warn_leave_advanced")

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QStackedWidget
        self.win = parent
        self.setWindowTitle("Preferences - " + APP_NAME)
        self.resize(780, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.tabs = QListWidget()
        self.tabs.setFixedWidth(160)
        self.tabs.setStyleSheet("QListWidget{background:#1a1a1a;border:none;border-right:1px solid #101010;outline:0;}"
                                "QListWidget::item{padding:9px 14px;color:#b8b8b8;}"
                                "QListWidget::item:hover{background:#2a2a2a;}"
                                f"QListWidget::item:selected{{background:{theme_map()['accent']};color:#ffffff;}}")
        self.stack = QStackedWidget()
        self.theme_cur = {}
        for name in self.PAGES:
            self.tabs.addItem(name)
            self.stack.addWidget(getattr(self, "_pg_" + name.lower().replace("-", ""))())
        self.tabs.currentRowChanged.connect(self.stack.setCurrentIndex)
        body.addWidget(self.tabs)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        foot = QFrame()
        foot.setObjectName("controlbar")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(10, 8, 10, 8)
        b_reset = QPushButton("RESET")
        b_reset.setToolTip("Reset every preference to its default (asks first)")
        b_reset.setStyleSheet("QPushButton{color:#ff8a80;font-weight:700;}")
        b_reset.clicked.connect(self._reset)
        fl.addWidget(b_reset)
        fl.addStretch(1)
        b_ok, b_cancel, b_apply = QPushButton("OK"), QPushButton("CANCEL"), QPushButton("APPLY")
        b_ok.setObjectName("primary")
        b_ok.clicked.connect(lambda: self._apply() and self.accept())
        b_cancel.clicked.connect(self.reject)
        b_apply.clicked.connect(self._apply)
        for b in (b_ok, b_cancel, b_apply):
            b.setAutoDefault(False)
            fl.addWidget(b)
        root.addWidget(foot)
        self.tabs.setCurrentRow(0)
        self._load()

    def _wrap(self, title):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 16, 20, 16)
        h = QLabel(title)
        h.setStyleSheet("font-size:13pt;font-weight:700;color:#ffffff;")
        v.addWidget(h)
        return w, v

    @staticmethod
    def _dim(text):
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet("color:#8f8f8f;")
        return l

    # ---- pages
    def _pg_general(self):
        w, v = self._wrap("General")
        self.ex_on = QCheckBox("Use a default export location")
        v.addWidget(self.ex_on)
        row = QHBoxLayout()
        self.ex_dir = QLineEdit()
        self.ex_dir.setPlaceholderText("Folder the Export dialog opens in")
        self.ex_browse = QPushButton("Browse...")

        def browse():
            d = QFileDialog.getExistingDirectory(self, "Default export folder", self.ex_dir.text() or os.path.expanduser("~"))
            if d:
                self.ex_dir.setText(d)
        self.ex_browse.clicked.connect(browse)
        row.addWidget(self.ex_dir, 1)
        row.addWidget(self.ex_browse)
        v.addLayout(row)
        self.ex_on.toggled.connect(lambda on: (self.ex_dir.setEnabled(on), self.ex_browse.setEnabled(on)))
        v.addWidget(self._dim("Shift+click on Export ignores this and saves next to the source file (edit1, edit2...)."))
        v.addSpacing(14)
        h = QLabel("File naming scheme")
        h.setStyleSheet("font-weight:700;color:#eaeaea;")
        v.addWidget(h)
        self.scheme = QLineEdit()
        v.addWidget(self.scheme)
        tk = QHBoxLayout()
        for tok, desc in NAME_TOKENS:
            b = QToolButton()
            b.setText(tok)
            b.setToolTip(desc)
            b.clicked.connect(lambda _=False, t=tok: (self.scheme.insert(t), self.scheme.setFocus()))
            tk.addWidget(b)
        tk.addStretch(1)
        v.addLayout(tk)
        self.scheme_prev = self._dim("")
        v.addWidget(self.scheme_prev)
        self.scheme.textChanged.connect(self._scheme_preview)
        v.addWidget(self._dim("Tokens: " + "   ".join(f"{t} = {d}" for t, d in NAME_TOKENS) +
                              ".\nAdd your own text, hyphens (-) and underscores (_) between the % tokens. The extension "
                              "is added automatically; characters Windows disallows become '_'. Random tokens change on every export."))
        v.addStretch(1)
        return w

    def _scheme_preview(self, _=None):
        p = format_out_name(self.scheme.text(), "C:/Videos/holiday.mp4", ".mp4", "", "Video")
        self.scheme_prev.setText("Preview:  " + p)

    def _pg_keybinds(self):
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QKeySequenceEdit, QHeaderView
        w, v = self._wrap("Keybinds")
        v.addWidget(self._dim("Click a shortcut box and press the new key combination. Duplicates are highlighted."))
        self.kt = QTableWidget(len(KEYBIND_DEFS), 3)
        self.kt.setHorizontalHeaderLabels(["Action", "Shortcut", ""])
        self.kt.verticalHeader().hide()
        self.kt.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.kt.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.kt.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.kt.setColumnWidth(1, 170)
        self.kt.setColumnWidth(2, 64)
        self.key_edits = {}
        for r, (kid, label, dflt) in enumerate(KEYBIND_DEFS):
            self.kt.setItem(r, 0, QTableWidgetItem(label))
            ed = QKeySequenceEdit()
            try:
                ed.setMaximumSequenceLength(1)
            except Exception:
                pass
            ed.editingFinished.connect(self._check_conflicts)
            self.kt.setCellWidget(r, 1, ed)
            rb = QPushButton("Default")
            rb.setStyleSheet("padding:2px 6px;")
            rb.clicked.connect(lambda _=False, e=ed, d=dflt: (e.setKeySequence(QKeySequence(d)), self._check_conflicts()))
            self.kt.setCellWidget(r, 2, rb)
            self.key_edits[kid] = ed
        v.addWidget(self.kt, 1)
        return w

    def _check_conflicts(self):
        seen, dup = {}, set()
        for k, e in self.key_edits.items():
            s = e.keySequence().toString()
            if s:
                if s in seen:
                    dup |= {k, seen[s]}
                seen[s] = k
        for k, e in self.key_edits.items():
            e.setStyleSheet("border:1px solid #e0413a;" if k in dup else "")
        return dup

    def _pg_interface(self):
        w, v = self._wrap("Interface")
        v.addWidget(self._dim("Pick the program's colours. Applied with Apply / OK. (The timeline's clip and playhead "
                              "colours are fixed.)"))
        self.swatches = {}
        for k, label in THEME_LABELS.items():
            row = QHBoxLayout()
            row.addWidget(QLabel(label), 1)
            b = QPushButton()
            b.setFixedSize(90, 24)
            b.clicked.connect(lambda _=False, k=k: self._pick_color(k))
            row.addWidget(b)
            rb = QToolButton()
            rb.setText("\u21ba")
            rb.setToolTip("Reset to default (" + THEME_DEFAULTS[k] + ")")
            rb.setFixedSize(24, 24)
            rb.clicked.connect(lambda _=False, k=k: self._set_swatch(k, THEME_DEFAULTS[k]))
            row.addWidget(rb)
            v.addLayout(row)
            self.swatches[k] = b
        v.addStretch(1)
        return w

    def _set_swatch(self, k, col):
        self.theme_cur[k] = col
        self.swatches[k].setStyleSheet(f"QPushButton{{background:{col};border:1px solid #888;}}")
        self.swatches[k].setText(col)

    def _pick_color(self, k):
        c = QColorDialog.getColor(QColor(self.theme_cur[k]), self, THEME_LABELS[k])
        if c.isValid():
            self._set_swatch(k, c.name())

    def _pg_plugins(self):
        w, v = self._wrap("Plug-ins")
        v.addWidget(self._dim("Scripts (.py) found in the program's \"mods\" folder. A mod is a \"tool\" (icon in the tool "
                              "row; several share one stacked button), a \"mode\" (a tab next to Video / GIF) or a \"tab\" (a window like "
                              "Project). New scripts are OFF: nothing is loaded until you tick it and press Apply / OK. Mods run "
                              "with full permissions - only enable scripts you trust. Unticking a loaded mod removes it, but a "
                              "restart is needed to fully unload its code."))
        self.mod_list = QListWidget()
        v.addWidget(self.mod_list, 1)
        row = QHBoxLayout()
        b1, b2 = QPushButton("Open mods folder"), QPushButton("Rescan")
        b1.clicked.connect(lambda: (os.makedirs(MODS_DIR, exist_ok=True), QDesktopServices.openUrl(QUrl.fromLocalFile(MODS_DIR))))
        b2.clicked.connect(self._fill_mods)
        row.addWidget(b1)
        row.addWidget(b2)
        for txt, d, tip in (("\u25B2", -1, "Move the selected plug-in up"), ("\u25BC", 1, "Move the selected plug-in down")):
            bm = QPushButton(txt)                       # [52.23] load order: tool-stack / Z-cycle order, tab + mode order
            bm.setFixedWidth(36)
            bm.setToolTip(tip + " (press Apply / OK to use the new order)")
            bm.clicked.connect(lambda _=False, d=d: self._move_mod(d))
            row.addWidget(bm)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _move_mod(self, d):
        r = self.mod_list.currentRow()
        if r < 0 or not 0 <= r + d < self.mod_list.count():
            return
        it = self.mod_list.takeItem(r)                  # keeps its check state + data
        self.mod_list.insertItem(r + d, it)
        self.mod_list.setCurrentRow(r + d)

    def _fill_mods(self):
        import plugins as _pl
        on = set(pref_json("pref_mods_on", []))
        if self.mod_list.count():                       # keep unsaved checkbox state across a rescan
            on = {self.mod_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.mod_list.count())
                  if self.mod_list.item(i).checkState() == Qt.CheckState.Checked}
        loaded = getattr(self.win, "plugins", None).loaded if self.win is not None and hasattr(self.win, "plugins") else {}
        keep = [self.mod_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.mod_list.count())]   # unsaved order
        mods = _pl.scan_mods()
        if keep:
            mods.sort(key=lambda m: keep.index(m.file) if m.file in keep else len(keep))
        self.mod_list.clear()
        for m in mods:
            it = QListWidgetItem(f"{m.name}   [{m.type}]   -   {m.file}" + ("   (loaded)" if m.file in loaded else "")
                                 + (f"   (ERROR: {m.error})" if m.error else ""))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if m.file in on else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, m.file)
            it.setToolTip((m.desc or "No description") + (f"\nBy {m.author}" if m.author else ""))
            self.mod_list.addItem(it)

    def _pg_credits(self):
        w, v = self._wrap("Credits")
        a = theme_map()["accent"]
        lab = QLabel(
            f"<p><b>{APP_NAME}</b></p>"
            f"<p>Author: <a style='color:{a}' href='https://github.com/s3rville/serv-s-quick-video-clip-editor-sqvce-/tree/main'>serville</a></p>"
            "<p>Prepared with the help of the Claude</p>"
            "<p>Built on:</p><ul>"
            f"<li><a style='color:{a}' href='https://ffmpeg.org/'>FFmpeg</a></li>"
            f"<li><a style='color:{a}' href='https://github.com/kohler/gifsicle'>Gifsicle</a></li>"
            f"<li><a style='color:{a}' href='https://github.com/handbrake/handbrake'>HandBrake</a></li></ul>")
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setOpenExternalLinks(True)
        lab.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(lab, 1)
        return w

    # ---- load / save / apply / reset
    def _load(self):
        s = prefs()
        self.ex_on.setChecked(s.value("pref_export_dir_on", False, bool))
        self.ex_dir.setText(s.value("pref_export_dir", "", str))
        self.ex_dir.setEnabled(self.ex_on.isChecked())
        self.ex_browse.setEnabled(self.ex_on.isChecked())
        self.scheme.setText(name_scheme())
        self._scheme_preview()
        km = keybind_map()
        for k, e in self.key_edits.items():
            e.setKeySequence(QKeySequence(km[k]))
        self._check_conflicts()
        for k, c in theme_map().items():
            self._set_swatch(k, c)
        self.mod_list.clear()
        self._fill_mods()

    def _apply(self):
        if self._check_conflicts():
            QMessageBox.warning(self, "Keybinds", "Two actions share the same shortcut (highlighted in red). Change one first.")
            return False
        was_on = set(pref_json("pref_mods_on", []))                  # [52.32] newly ticked plug-ins -> recovery caveat
        now_on = [self.mod_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.mod_list.count())
                  if self.mod_list.item(i).checkState() == Qt.CheckState.Checked]
        new_mods = [m for m in now_on if m not in was_on]
        if new_mods:
            SB = QMessageBox.StandardButton
            if QMessageBox.warning(self, "New plug-ins", "Plug-ins may not be fully supported by crash recovery.\n\nNew:\n  " + "\n  ".join(new_mods)
                                   + "\n\nEnable them anyway?", SB.Ok | SB.Cancel, SB.Ok) != SB.Ok:
                return False
        s = prefs()
        s.setValue("pref_export_dir_on", self.ex_on.isChecked())
        s.setValue("pref_export_dir", self.ex_dir.text().strip())
        s.setValue("pref_name_scheme", self.scheme.text().strip() or DEFAULT_SCHEME)
        norm = lambda t: QKeySequence(t).toString()
        s.setValue("pref_keybinds", json.dumps({k: e.keySequence().toString() for (k, _, d), e in
                                                zip(KEYBIND_DEFS, self.key_edits.values()) if e.keySequence().toString() != norm(d)}))
        s.setValue("pref_theme", json.dumps({k: c for k, c in self.theme_cur.items() if c.lower() != THEME_DEFAULTS[k]}))
        s.setValue("pref_mods_on", json.dumps([self.mod_list.item(i).data(Qt.ItemDataRole.UserRole)
                                               for i in range(self.mod_list.count())
                                               if self.mod_list.item(i).checkState() == Qt.CheckState.Checked]))
        s.setValue("pref_mods_order", json.dumps([self.mod_list.item(i).data(Qt.ItemDataRole.UserRole)
                                                  for i in range(self.mod_list.count())]))
        s.sync()
        self._run_apply()
        return True

    def _run_apply(self):
        if self.win is not None and hasattr(self.win, "apply_prefs"):
            gone = self.win.apply_prefs()
            self._fill_mods()
            if gone:
                QMessageBox.information(self, "Restart required",
                                        "These plug-ins were switched off and their windows/buttons removed:\n\n  " + "\n  ".join(gone) +
                                        "\n\nTheir code stays in memory until you restart " + APP_NAME + " - restart to fully unload them.")

    def _reset(self):
        SB = QMessageBox.StandardButton
        if QMessageBox.question(self, "Reset preferences",
                                "Reset ALL preferences (export location, naming scheme, keybinds, colours, plug-in "
                                "selection) to their defaults?\n\nThis cannot be undone.", SB.Yes | SB.No, SB.No) != SB.Yes:
            return
        s = prefs()
        for k in self.PREF_KEYS:
            s.remove(k)
        s.sync()
        self.mod_list.clear()
        self._load()
        self._run_apply()
