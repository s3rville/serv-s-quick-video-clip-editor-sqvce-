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


# [MAP] Aspect-ratio helpers for the crop tool. std_ratio(w,h) names a standard ratio (either orientation,
# 0.8 % tolerance) for the label pill; nearest_ratio(w,h) is what SHIFT-drag snaps to.
RATIOS = [(1, 1), (5, 4), (4, 3), (3, 2), (16, 10), (16, 9), (21, 9), (2, 1)]
_RAT = RATIOS + [(b, a) for a, b in RATIOS if a != b]          # horizontal + vertical


def std_ratio(w, h, tol=0.008):
    """'16:9' etc. if w:h is a standard/widely used ratio (either orientation), else None."""
    if w <= 0 or h <= 0:
        return None
    for a, b in _RAT:
        if abs((w / h) / (a / b) - 1) < tol:
            return f"{a}:{b}"
    return None


def nearest_ratio(w, h):
    r = max(w, 1e-6) / max(h, 1e-6)
    return min(_RAT, key=lambda ab: abs(math.log(r / (ab[0] / ab[1]))))


# [MAP] Transparent widget over the whole VideoStage that draws and edits the crop/resize selection rectangle.
# COORDINATES: self.sel and every mouse position are in STAGE WIDGET PIXELS (screen space). VideoStage converts
# to/from canvas SOURCE pixels (`pend`, via _rc and _k) - never store screen pixels as edit results.
# Interaction: 8 handles + move (hit test with SLOP 9 px), or drag empty space to PAN the view (VideoStage._pan,
# same one wheel-zoom uses - a no-op unless actually zoomed in). SHIFT snaps to standard ratios (_snap). Edge/corner
# snapping (CANVAS_SNAP, 10 SCREEN px, deliberately screen-space so it feels looser when zoomed out and tighter
# when zoomed in) targets whichever rect this drag actually edits: clip_rect for Video-mode crop, canvas_rect for
# Canvas-mode crop and for Resize. Bounds `b`: crop (Video mode, default) edits clip_rect and is bounded to
# picture united with clip_rect (allows "un-crop" past the current crop window, up to the recoverable picture);
# crop (Canvas mode, VideoStage.crop_mode) edits canvas_rect ONLY - clip_rect, and so the crop itself, never
# moves - and resize = whole overlay (Canvas mode additionally lets the drag go past the actual video into the
# background). Resize corner drags scale proportionally.
# Right-click menu = "Reset crop && resize for this clip" (emits stage.resetRequested -> MainWindow.reset_xf).
# Mouse wheel zooms the stage (VideoStage.wheel_zoom). NOTHING is applied until the stage's OK button.
# [STALE] The class docstring says crop is "limited to the frame" - that changed in v4 (un-crop ghost).
class CropOverlay(QWidget):
    """Selection rectangle drawn over the preview. Drag a corner/side to resize it, drag inside to
    move it (hold SHIFT to snap to standard aspect ratios). Crop: free-form, limited to the frame.
    Resize: corners keep aspect, sides stretch. Nothing is applied until the stage's OK is pressed."""
    MIN, SLOP, CANVAS_SNAP = 24.0, 9.0, 10.0    # CANVAS_SNAP stays constant in *screen* px, so it's
                                                 # naturally more forgiving zoomed out, tighter zoomed in
    _CUR = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
            "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
            "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
            "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor,
            "move": Qt.CursorShape.SizeAllCursor}

    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage
        self.sel = QRectF()
        self.drag = None
        self.setMouseTracking(True)
        self.hide()

    def reset_selection(self):
        st = self.stage
        p = st.pending_sel()
        if p is not None:
            self.sel = QRectF(p)
        elif st.tool == "crop":
            # Video mode edits clip_rect (the crop window); Canvas mode edits canvas_rect itself.
            self.sel = QRectF(st.clip_rect() if st.crop_mode == "video" else st.canvas_rect())
        else:
            self.sel = QRectF(st.picture_rect())
        self.update()

    def _hit(self, pos):
        r, s = self.sel, self.SLOP
        if not (r.left() - s <= pos.x() <= r.right() + s and r.top() - s <= pos.y() <= r.bottom() + s):
            return None
        h = ("l" if abs(pos.x() - r.left()) <= s else "r" if abs(pos.x() - r.right()) <= s else "") + \
            ("t" if abs(pos.y() - r.top()) <= s else "b" if abs(pos.y() - r.bottom()) <= s else "")
        return h or ("move" if r.contains(pos) else None)

    def mousePressEvent(self, e):
        # [FEATURE] Middle-click always pans (regardless of what's under the cursor - handle,
        # selection or empty space), same as the empty-space left-drag pan below.
        if e.button() == Qt.MouseButton.MiddleButton and self.stage.tool in ("crop", "resize"):
            self.drag = ("pan", e.position(), self.stage._pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        h = self._hit(e.position()) if e.button() == Qt.MouseButton.LeftButton else None
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(self)
            a = m.addAction("Reset crop && resize for this clip")
            if m.exec(e.globalPosition().toPoint()) is a:
                self.stage.resetRequested.emit()
            return
        if h is None:
            # [FEATURE] Empty space (not a handle, not inside the selection): pan the view instead of
            # doing nothing. Reuses VideoStage._pan (the same one wheel-zoom drives), so this is a no-op
            # at fit zoom - relayout() resets pan to centered whenever the view isn't actually zoomed in.
            if e.button() == Qt.MouseButton.LeftButton and self.stage.tool in ("crop", "resize"):
                self.drag = ("pan", e.position(), self.stage._pan)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return
            e.ignore()
            return
        self.stage.dragStarted.emit()
        self.drag = (h, e.position(), QRectF(self.sel))

    def _snap(self, r, r0, h, b):
        """Snap r to the nearest standard ratio, anchored on the side/corner opposite the handle."""
        a, c = nearest_ratio(r.width(), r.height())
        rho = a / c
        if len(h) == 2:
            ax = r0.right() if "l" in h else r0.left()
            ay = r0.bottom() if "t" in h else r0.top()
            wmax = (ax - b.left()) if "l" in h else (b.right() - ax)
            hmax = (ay - b.top()) if "t" in h else (b.bottom() - ay)
            w = max(r.width(), r.height() * rho, self.MIN, self.MIN * rho)
            w = min(w, wmax, hmax * rho)
            hh = w / rho
            return QRectF(ax - w if "l" in h else ax, ay - hh if "t" in h else ay, w, hh)
        if h in ("l", "r"):
            cy = r0.center().y()
            wmax = (r0.right() - b.left()) if h == "l" else (b.right() - r0.left())
            hh = min(r.width() / rho, 2 * min(cy - b.top(), b.bottom() - cy), wmax / rho)
            w = hh * rho
            return QRectF(r0.right() - w if h == "l" else r0.left(), cy - hh / 2, w, hh)
        cx = r0.center().x()
        hmax = (r0.bottom() - b.top()) if h == "t" else (b.bottom() - r0.top())
        w = min(r.height() * rho, 2 * min(cx - b.left(), b.right() - cx), hmax * rho)
        hh = w / rho
        return QRectF(cx - w / 2, r0.bottom() - hh if h == "t" else r0.top(), w, hh)

    # [PITFALL] Branch order matters: 'move' first; then the proportional resize-corner branch (Resize tool, 2-letter
    # handle); else generic edge/corner drag, where SHIFT ratio-snap and canvas-edge snap are mutually exclusive.
    # In Resize mode every move also calls stage.live_picture(r) so the preview shows the scaled picture live.
    def mouseMoveEvent(self, e):
        if not self.drag:
            h = self._hit(e.position())
            self.setCursor(self._CUR.get(h, Qt.CursorShape.ArrowCursor))
            return
        h, p0, r0 = self.drag
        if h == "pan":
            self.stage.pan_by(r0, e.position() - p0)
            return
        d = e.position() - p0
        st = self.stage
        crop_video = st.tool == "crop" and st.crop_mode == "video"
        # [FEATURE] Crop "Canvas" mode: bound the drag to the whole overlay (same freedom as Resize)
        # instead of picture-united-clip_rect, so edges/corners can be pulled out past the actual video
        # into the background - Video mode (default) keeps the existing un-crop-bounded behaviour, now
        # anchored on clip_rect (the actual crop window) rather than canvas_rect (which Canvas mode can
        # move independently).
        if st.tool == "crop":
            b = st.picture_rect().united(st.clip_rect()) if crop_video else QRectF(self.rect())
        else:
            b = QRectF(self.rect())
        # snap target: whichever rect this drag is actually editing (clip_rect in Video-mode crop,
        # canvas_rect for Canvas-mode crop and for Resize - Resize never touches clip_rect at all).
        snap_to = st.clip_rect() if crop_video else st.canvas_rect()
        r = QRectF(r0)
        if h == "move":
            r.translate(d)
            r.moveLeft(max(b.left(), min(r.left(), max(b.left(), b.right() - r.width()))))
            r.moveTop(max(b.top(), min(r.top(), max(b.top(), b.bottom() - r.height()))))
            c = snap_to
            if abs(r.left() - c.left()) < self.CANVAS_SNAP:
                r.moveLeft(c.left())
            elif abs(r.right() - c.right()) < self.CANVAS_SNAP:
                r.moveRight(c.right())
            if abs(r.top() - c.top()) < self.CANVAS_SNAP:
                r.moveTop(c.top())
            elif abs(r.bottom() - c.bottom()) < self.CANVAS_SNAP:
                r.moveBottom(c.bottom())
        elif len(h) == 2 and st.tool == "resize":          # proportional corner scale
            ax = r0.right() if "l" in h else r0.left()
            ay = r0.bottom() if "t" in h else r0.top()
            w = r0.width() + (-d.x() if "l" in h else d.x())
            k = max(self.MIN / r0.width(), self.MIN / r0.height(), w / r0.width())
            kmax = min(((ax - b.left()) if "l" in h else (b.right() - ax)) / r0.width(),
                       ((ay - b.top()) if "t" in h else (b.bottom() - ay)) / r0.height())
            k = min(k, max(kmax, 0.01))
            nw, nh = r0.width() * k, r0.height() * k
            r = QRectF(ax - nw if "l" in h else ax, ay - nh if "t" in h else ay, nw, nh)
        else:
            if "l" in h:
                r.setLeft(max(b.left(), min(r0.left() + d.x(), r0.right() - self.MIN)))
            if "r" in h:
                r.setRight(min(b.right(), max(r0.right() + d.x(), r0.left() + self.MIN)))
            if "t" in h:
                r.setTop(max(b.top(), min(r0.top() + d.y(), r0.bottom() - self.MIN)))
            if "b" in h:
                r.setBottom(min(b.bottom(), max(r0.bottom() + d.y(), r0.top() + self.MIN)))
            if h != "move" and (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                r = self._snap(r, r0, h, b)
            else:
                c = snap_to
                if "l" in h and abs(r.left() - c.left()) < self.CANVAS_SNAP:
                    r.setLeft(c.left())
                if "r" in h and abs(r.right() - c.right()) < self.CANVAS_SNAP:
                    r.setRight(c.right())
                if "t" in h and abs(r.top() - c.top()) < self.CANVAS_SNAP:
                    r.setTop(c.top())
                if "b" in h and abs(r.bottom() - c.bottom()) < self.CANVAS_SNAP:
                    r.setBottom(c.bottom())
        self.sel = r
        st.set_pending(r)                                   # also refreshes the W/H boxes
        if st.tool == "resize":
            st.live_picture(r)
        self.update()

    def mouseReleaseEvent(self, e):
        self.drag = None

    def wheelEvent(self, e):
        if self.stage.tool in ("crop", "resize"):
            self.stage.wheel_zoom(e.position(), e.angleDelta().y() or e.angleDelta().x())
            e.accept()
        else:
            e.ignore()

    def paintEvent(self, e):
        st = self.stage
        if self.sel.isNull() or st.tool is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, blue = self.sel, QColor("#2d8ceb")
        if st.tool == "crop":
            path = QPainterPath()
            path.addRect(QRectF(self.rect()))
            path.addRect(r)
            p.fillPath(path, QColor(0, 0, 0, 150))
            if st.crop_mode == "video":
                full, clip = st.picture_rect(), st.clip_rect()
                if full.width() > clip.width() + 0.5 or full.height() > clip.height() + 0.5:
                    p.setPen(QPen(QColor("#9a9a9a"), 1, Qt.PenStyle.DashLine))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRect(full)          # ghost: drag the selection out to here to un-crop
            else:
                # Canvas mode never touches the crop - show where it still sits so padding/framing
                # decisions can be made with the (untouched) crop window in view.
                p.setPen(QPen(QColor("#9a9a9a"), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(st.clip_rect())
        # [FEATURE] Canvas (fixed output frame) border: always drawn in both Crop and Resize (not just Resize),
        # stronger/more visible, and turns orange where the canvas would spill outside the actual video picture
        # (i.e. it "intersects" the picture's edge instead of sitting fully inside it - a letterboxing warning).
        canvas = st.canvas_rect()
        picture = st.picture_rect()
        canvas_overflows = not picture.contains(canvas)
        p.setPen(QPen(QColor("#ff9800") if canvas_overflows else QColor("#e8e8e8"), 2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(canvas)                                   # the fixed output frame
        p.setPen(QPen(blue, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(blue, 1.5))
        for x in (r.left(), r.center().x(), r.right()):
            for y in (r.top(), r.center().y(), r.bottom()):
                if (x, y) != (r.center().x(), r.center().y()):
                    p.drawRect(QRectF(x - 5, y - 5, 10, 10))
        k = st.scale() or 1.0
        p.setPen(QColor("#ffffff"))
        p.drawText(QPointF(r.left() + 8, r.top() + 18), f"{int(round(r.width() / k))} x {int(round(r.height() / k))}")
        lab = std_ratio(r.width(), r.height())
        if lab:                                             # aspect-ratio pill above the box
            f = p.font()
            f.setBold(True)
            p.setFont(f)
            fm = p.fontMetrics()
            tw, th = fm.horizontalAdvance(lab) + 14, fm.height() + 4
            y = r.top() - th - 6
            if y < 2:
                y = r.top() + 24
            pill = QRectF(r.center().x() - tw / 2, y, tw, th)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 20, 20, 210))
            p.drawRoundedRect(pill, 4, 4)
            p.setPen(QColor("#ffffff"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, lab)


# [MAP] The actual video surface: a QGraphicsView containing one QGraphicsVideoItem (chosen instead of
# QVideoWidget because an item can be rotated / mirrored / offset by a QTransform). Non-native widget, so
# overlays draw on top of it. Engine attaches its QMediaPlayer to `self.item`.
# [DO NOT BREAK #5] Video output is this view's item; Engine reads `video_widget.videoSink()` from it.
# Two layout modes (set_layout):
#   _lay is None  -> plain fit: item fills the widget with KeepAspectRatio (untouched clip).
#   _lay = (pre_canvas_wh, pic_xywh, rot, mirror, canvas_off) -> item is sized to the SCALED PICTURE and a
#         QTransform applies (bottom-up): picture offset -> centre on canvas -> mirror -> rotate -> place in the
#         widget. canvas_off is only non-zero while the crop tool widens the widget to reveal the "ghost".
class VideoView(QGraphicsView):
    """Video output that CAN be transformed (rotate / mirror / crop-offset), unlike QVideoWidget:
    a QGraphicsVideoItem in a scene. It is an ordinary (non-native) widget, so overlays sit on top
    of it and it is clipped by its own rect. Drop-in for the old QVideoWidget: `videoSink()`,
    `setAspectRatioMode()`; Engine attaches the player to `self.item`."""

    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.item = QGraphicsVideoItem()
        self._scene.addItem(self.item)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#000000"))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setInteractive(False)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self._ar = Qt.AspectRatioMode.KeepAspectRatio
        self._lay = None            # None = plain fit; else (pre_canvas_wh, pic_xywh, rot, mirror) in px

    def videoSink(self):
        return self.item.videoSink()

    def set_bg_color(self, color):
        """Blank/background color shown behind the picture (letterbox bars, un-crop ghost, resize
        pad area) - the live-preview counterpart of xf_filter's pad=...:color=. `color` is a hex
        string, e.g. "#000000".
        """
        self.setBackgroundBrush(QColor(color))

    def setAspectRatioMode(self, m):
        self._ar = m
        self._relayout()

    def set_layout(self, pre=None, pic=None, rot=0, mirror=False, canvas_off=(0.0, 0.0)):
        self._lay = None if pre is None else (pre, pic, rot, mirror, canvas_off)
        self._relayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        w, h = max(1, self.width()), max(1, self.height())
        self._scene.setSceneRect(0, 0, w, h)
        it = self.item
        if self._lay is None:
            it.setTransform(QTransform())
            it.setPos(0, 0)
            it.setAspectRatioMode(self._ar)
            it.setSize(QSizeF(w, h))
            return
        (cw, ch), (px, py, pw, ph), rot, mir, (ox, oy) = self._lay
        it.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
        it.setSize(QSizeF(max(1.0, pw), max(1.0, ph)))
        it.setPos(0, 0)
        # dw,dh = the canvas's own on-screen size (pre/post 90 deg swap). This widget's own w,h is
        # generally DIFFERENT now (VideoStage.relayout masks it to visible_rect() - picture ^ clip_rect
        # ^ canvas - or, while Video-mode crop editing widens it to the full picture for the ghost);
        # (ox, oy) is always the canvas's own top-left offset within whatever this widget's bounds are.
        dw, dh = (ch, cw) if rot in (90, 270) else (cw, ch)
        t = QTransform()
        t.translate(ox + dw / 2.0, oy + dh / 2.0)           # (applied bottom-up) picture -> canvas -> mirror -> rotate
        t.rotate(rot)
        t.scale(-1.0 if mir else 1.0, 1.0)
        t.translate(-cw / 2.0, -ch / 2.0)
        t.translate(px, py)
        it.setTransform(t)


# [MAP] Container for the video + crop/resize editing UI (OK/Cancel bar, W x H boxes, swap, rotate).
# Children: `canvas` (output-frame backdrop, painted in the clip's own bg_color - previously always black,
# before clip_rect could differ from canvas_rect), `vclip` (native-parent host that HARD-CLIPS the video, masked
# to visible_rect() = picture ^ clip_rect ^ canvas, so a crop stays hidden regardless of canvas size and an
# oversized picture can never spill over the rest of the program), `video` (VideoView), `overlay` (CropOverlay),
# `bar`.
# THREE COORDINATE SPACES (the main source of confusion in this class):
#   1 pre-rotation canvas units  = the Seg.xf space (source pixels, before rotation/mirror)
#   2 DISPLAYED canvas units     = after rotation/mirror (`_t()` maps 1 -> 2; `_disp_dims()` gives the size).
#                                  `pend` (the pending selection) and the W/H boxes live in THIS space.
#   3 screen pixels of the stage = `_rc` (canvas rect) and `_k` (screen px per canvas unit) map 2 -> 3.
# picture_rect()/clip_rect()/canvas_rect()/visible_rect() all live in space 3 (screen pixels), via the same
# `_rc`/`_k` mapping.
# `_ok()` converts displayed -> pre-rotation and emits committed(tool, rect-in-stage-pixels, bg-color-hex);
# MainWindow.
# on_stage_commit turns that into a new Seg.xf (through norm_xf) via Sequence.edit.
# [INVARIANT] `_rc` / `_k` are computed in relayout() and used by ALL crop maths; visible_rect()'s masking and
# the v4 Video-mode "ghost" widening only change the size of the native video widget (vclip/video) and
# `_canvas_off`, never `_rc`/`_k`.
# Zoom (wheel, tools only): _zoom 0.5..6.0 with zoom-to-cursor and clamped pan; reset on set_tool/set_state.
# [STALE] Docstring says "Holds the QVideoWidget" (it is a VideoView now); the `still` QLabel is a leftover of
# the removed frozen-frame approach (see MainWindow.capture_still).
class VideoStage(QWidget):
    """Holds the QVideoWidget on a black canvas so the crop/resize result is previewed live:
    the canvas is the output frame, the video widget is the (scaled/offset) picture inside it,
    and anything outside the canvas is clipped (widget mask) - the same maths the export uses.
    Crop/resize edits stay pending (self.pend, canvas source-pixel units) until OK is pressed."""
    committed = Signal(str, QRectF, str)  # tool, selection in stage pixels, bg color hex (emitted on OK)
    dragStarted = Signal()
    resetRequested = Signal()
    okClicked = Signal()
    cancelClicked = Signal()
    rotateRequested = Signal()
    MARGIN = 40                          # breathing room around the canvas while a tool is active

    def __init__(self, video):
        super().__init__()
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.canvas = QWidget(self)
        self.canvas.setPalette(pal)
        self.canvas.setAutoFillBackground(True)
        # Native host for the video: a native parent window hard-clips the native video surface, so an
        # oversized (zoomed/cropped) picture can never spill over the rest of the program. Hidden
        # while a tool is active (the frozen-frame QLabel in `canvas` is shown instead).
        self.vclip = QWidget(self)
        self.vclip.setPalette(pal)
        self.vclip.setAutoFillBackground(True)
        self.video = video
        video.setParent(self.vclip)
        video.show()
        self.still = QLabel(self.canvas)          # frozen frame shown instead of the native video while editing
        self.still.setScaledContents(True)
        self.still.hide()
        self.overlay = CropOverlay(self)
        self.tool = None
        self.crop_mode = "video"        # Crop tool only: "video" (default, un-crop bounded to the
                                         # recoverable picture) or "canvas" (drag past it into the
                                         # background - see set_crop_mode / CropOverlay.mouseMoveEvent)
        self.xf = None
        self.src = (0, 0)
        self.pend = None
        self.fx = (0.0, False)          # (rotation deg, mirror) of the current clip
        self._rc = QRectF()
        self._k = 1.0
        self._canvas_off = (0.0, 0.0)   # canvas top-left within the (possibly crop-ghost-widened) video widget
        self._zoom = 1.0                # >= 1.0; 1.0 = fit (can't zoom out further, so the canvas is never lost)
        self._pan = QPointF(0.0, 0.0)   # screen-px offset from centered, for zoom-to-cursor
        self.bg_color = DEFAULT_BG      # pending blank/background color (committed into Seg.xf on OK)
        # fixed bottom-right action bar: [Mode (Crop only)] ... [color swatch] ... [W] x [H] [Cancel] [OK]
        self.bar = QWidget(self)
        lay = QHBoxLayout(self.bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        # [FEATURE] Crop-only Mode toggle: "Video" (default) is the existing bounded un-crop behaviour;
        # "Canvas" lets the drag go past the recoverable picture into the background (see
        # CropOverlay.mouseMoveEvent's `b` bound). Kept at the OPPOSITE end of the bar from Cancel/OK,
        # with its own isolating gap, so it reads as a mode switch rather than one of the action buttons.
        self.b_mode = QPushButton("Mode: Video")
        self.b_mode.setToolTip("Crop mode: Video (drag is bounded to the recoverable frame) or Canvas "
                               "(drag past it to extend the output frame into the background color)")
        self.b_mode.setFixedWidth(96)
        self.b_mode.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.b_mode.setAutoDefault(False)
        self.b_mode.clicked.connect(self._toggle_crop_mode)
        lay.addWidget(self.b_mode)
        lay.addSpacing(18)                    # isolate the mode toggle from the rest of the bar
        self.b_color = QPushButton()          # opposite end of Cancel/OK: pick the blank/background color
        self.b_color.setFixedSize(28, 24)
        self.b_color.setToolTip("Background color (fills the blank area behind the video)")
        self.b_color.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_color.clicked.connect(self._pick_color)
        self._set_swatch(self.bg_color)
        lay.addWidget(self.b_color)
        lay.addSpacing(10)
        self.ed_w, self.ed_h = QLineEdit(), QLineEdit()
        for ed in (self.ed_w, self.ed_h):
            ed.setValidator(QIntValidator(1, 99999, ed))
            ed.setFixedWidth(62)
            ed.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ed.editingFinished.connect(self._apply_fields)
        x = QLabel("x")
        x.setStyleSheet("color:#cfd8e3;")
        self.b_swap = QPushButton("\u21c4")
        self.b_swap.setToolTip("Swap width and height")
        self.b_swap.setFixedWidth(32)
        self.b_swap.setStyleSheet("QPushButton{padding:4px 0;}")
        self.b_swap.clicked.connect(self._swap)
        self.b_rot = QPushButton()               # Resize tool only
        self.b_rot.setIcon(icon("rotate90", "#ffffff", size=16))
        self.b_rot.setIconSize(QSize(16, 16))
        self.b_rot.setToolTip("Rotate clip 90\u00b0 clockwise")
        self.b_rot.setFixedWidth(32)
        self.b_rot.setStyleSheet("QPushButton{padding:4px 0;}")
        self.b_rot.clicked.connect(self.rotateRequested)
        self.b_cancel, self.b_ok = QPushButton("Cancel"), QPushButton("OK")
        # [FEATURE] Reset button, between Cancel and OK: puts position AND size back to this clip's
        # original/starting point (no crop or resize at all), same action as the right-click menu's
        # "Reset crop && resize for this clip" (both wire to resetRequested -> MainWindow.reset_xf).
        # Requires two clicks: the first arms it (label -> "Confirm"), the second actually fires it,
        # so a stray click can't wipe out a crop/resize by accident (see _on_reset_clicked / _disarm_reset).
        self._reset_armed = False
        self.b_reset = QPushButton("Reset")
        self.b_reset.setToolTip("Reset position and size back to this clip's original starting point")
        self.b_reset.clicked.connect(self._on_reset_clicked)
        for b in (self.b_cancel, self.b_ok, self.b_swap, self.b_rot, self.b_color, self.b_reset):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoDefault(False)
        self.b_cancel.clicked.connect(self._disarm_reset)
        self.b_cancel.clicked.connect(self.cancelClicked)
        self.b_ok.clicked.connect(self._ok)
        for w in (self.ed_w, x, self.ed_h, self.b_swap, self.b_rot, self.b_cancel, self.b_reset, self.b_ok):
            lay.addWidget(w)
        self.bar.setStyleSheet(
            "QLineEdit{background:#1b1f26;color:#fff;border:1px solid #3a4250;border-radius:4px;padding:3px;}"
            "QLineEdit:focus{border-color:#2d8ceb;}"
            "QPushButton{background:#2a303a;color:#fff;border:1px solid #3a4250;border-radius:4px;padding:4px 14px;}"
            "QPushButton:hover{background:#343c49;}")
        self.b_ok.setStyleSheet("QPushButton{background:#2d8ceb;border-color:#2d8ceb;font-weight:bold;}"
                                "QPushButton:hover{background:#4a9cf0;}")
        self.bar.hide()

    def _set_swatch(self, color):
        """Paint the b_color button as a swatch of `color` (hex string)."""
        self.b_color.setStyleSheet(
            f"QPushButton{{background:{color};border:1px solid #3a4250;border-radius:4px;}}"
            f"QPushButton:hover{{border-color:#2d8ceb;}}")

    # [FEATURE] Reset requires two clicks: first arms it ("Reset" -> "Confirm"), second actually
    # fires resetRequested and disarms. Any other bar action (Cancel/OK/mode/color/swap/rotate) or
    # leaving the tool disarms it too, via _disarm_reset, so "Confirm" never lingers stale.
    def _on_reset_clicked(self):
        if not self._reset_armed:
            self._reset_armed = True
            self.b_reset.setText("Confirm")
            return
        self._disarm_reset()
        self.resetRequested.emit()

    def _disarm_reset(self):
        if self._reset_armed:
            self._reset_armed = False
            self.b_reset.setText("Reset")

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.bg_color), self, "Pick background color")
        if not c.isValid():
            return
        self.bg_color = c.name()
        self._set_swatch(self.bg_color)
        self.video.set_bg_color(self.bg_color)   # live preview, even before OK is pressed

    def _eff(self):
        w, h = self.src
        return self.xf or ((w, h, 0, 0, w, h) if w and h else None)

    # [MAP] Called by MainWindow.refresh_stage on EVERY playhead tick and edit. Cheap by design: it only relayouts
    # when (xf, source size, (rot, mirror)) actually changed; a change also resets zoom/pan and drops any pending
    # selection. Passing xf=None with src=(0,0) means "no video clip under the playhead".
    def set_state(self, xf, src, fx=(0.0, False)):
        if (xf, src, fx) != (self.xf, self.src, self.fx):
            self.xf, self.src, self.fx = xf, src, fx
            self.pend = None
            self.bg_color = xf_color(xf) if xf else DEFAULT_BG
            self._set_swatch(self.bg_color)
            self._zoom, self._pan = 1.0, QPointF(0.0, 0.0)
            self.relayout()

    # rotation/mirror: selection maths runs in DISPLAYED space; _t() maps pre-rotation canvas -> displayed
    def _rot(self):
        return int(round(self.fx[0] / 90.0)) * 90 % 360

    def _disp_dims(self):
        xf = self._eff()
        if not xf:
            return 0, 0
        return (xf[1], xf[0]) if self._rot() in (90, 270) else (xf[0], xf[1])

    def _t(self):
        xf = self._eff()
        dw, dh = self._disp_dims()
        t = QTransform()
        t.translate(dw / 2.0, dh / 2.0)
        t.rotate(self._rot())
        t.scale(-1.0 if self.fx[1] else 1.0, 1.0)
        t.translate(-xf[0] / 2.0, -xf[1] / 2.0)
        return t

    def set_tool(self, tool):
        self.tool = tool
        self.pend = None
        self._disarm_reset()                # never leave "Confirm" armed across tool sessions
        self.crop_mode = "video"            # always start a fresh Crop/Resize session in "Video" mode
        self.b_mode.setText("Mode: Video")
        self._zoom, self._pan = 1.0, QPointF(0.0, 0.0)
        self.relayout()

    # [FEATURE] Crop-only Mode toggle (b_mode). Switching mode mid-edit discards any uncommitted drag
    # (self.pend) so the user starts the new mode from the clip's last committed crop/resize, never a
    # half-finished selection from the other mode; relayout() then redraws the overlay/bar for it.
    def _toggle_crop_mode(self):
        self.set_crop_mode("canvas" if self.crop_mode == "video" else "video")

    def set_crop_mode(self, mode):
        if mode == self.crop_mode:
            return
        self.crop_mode = mode
        self.b_mode.setText(f"Mode: {'Canvas' if mode == 'canvas' else 'Video'}")
        self.pend = None
        self.relayout()

    def canvas_rect(self):
        return self._rc

    def scale(self):
        return self._k

    def picture_rect(self):
        xf = self._eff()
        if not xf:
            return QRectF(self._rc)
        _, _, px, py, pw, ph = xf[:6]
        k = self._k
        d = self._t().mapRect(QRectF(px, py, pw, ph))
        return QRectF(self._rc.x() + d.x() * k, self._rc.y() + d.y() * k, d.width() * k, d.height() * k)

    # [MAP] Screen rect of the persistent clip_rect - the crop window Video-mode edits and Canvas mode
    # leaves untouched. Same coordinate mapping as picture_rect()/canvas_rect() (see the class docstring's
    # THREE COORDINATE SPACES) - if that mapping ever changes, update it here too.
    def clip_rect(self):
        xf = self._eff()
        if not xf:
            return QRectF(self._rc)
        clx, cly, clw, clh = xf_clip(xf)
        k = self._k
        d = self._t().mapRect(QRectF(clx, cly, clw, clh))
        return QRectF(self._rc.x() + d.x() * k, self._rc.y() + d.y() * k, d.width() * k, d.height() * k)

    # [MAP] Screen rect of V = picture INTERSECT clip_rect INTERSECT canvas: the part of the source video
    # that is EVER shown, independent of how big the canvas itself is. Mirrors xf_filter's V in utils.py -
    # if you change one, change the other. Used by relayout() to mask the native video widget so a
    # Canvas-mode resize can never restore (or re-hide) the crop.
    def visible_rect(self):
        return self.picture_rect().intersected(self.clip_rect()).intersected(self._rc)

    # ---- pending (uncommitted) selection, in displayed canvas source-pixel units
    def _cur_units(self):
        if self.pend is not None:
            return QRectF(self.pend)
        xf = self._eff()
        if not xf:
            return QRectF()
        if self.tool == "crop":
            dw, dh = self._disp_dims()
            return QRectF(0, 0, dw, dh)
        _, _, px, py, pw, ph = xf[:6]
        return self._t().mapRect(QRectF(px, py, pw, ph))

    def pending_sel(self):
        if self.pend is None or self._k <= 0:
            return None
        k, rc, q = self._k, self._rc, self.pend
        return QRectF(rc.x() + q.x() * k, rc.y() + q.y() * k, q.width() * k, q.height() * k)

    def set_pending(self, sel):
        k, rc = self._k or 1.0, self._rc
        self.pend = QRectF((sel.x() - rc.x()) / k, (sel.y() - rc.y()) / k, sel.width() / k, sel.height() / k)
        self._sync_fields()

    def _sync_fields(self):
        r = self._cur_units()
        self.ed_w.setText(str(int(round(r.width()))))
        self.ed_h.setText(str(int(round(r.height()))))

    def _apply_fields(self):
        xf = self._eff()
        if not xf or self.tool is None or not self.bar.isVisible():
            return
        try:
            w, h = int(self.ed_w.text()), int(self.ed_h.text())
        except ValueError:
            self._sync_fields()
            return
        cw, ch = self._disp_dims()
        r = self._cur_units()
        if self.tool == "crop" and self.crop_mode == "video":
            _, _, px, py, pw, ph = xf[:6]
            full = self._t().mapRect(QRectF(px, py, pw, ph))    # full recoverable extent, display units
            fx0, fy0 = min(0.0, full.x()), min(0.0, full.y())
            fx1 = max(cw, full.x() + full.width())
            fy1 = max(ch, full.y() + full.height())
            w, h = max(16, min(w, fx1 - fx0)), max(16, min(h, fy1 - fy0))
            x, y = max(fx0, min(r.x(), fx1 - w)), max(fy0, min(r.y(), fy1 - h))
        elif self.tool == "crop":                # Canvas mode: same unbounded (up to 4x) allowance as Resize
            w, h = max(16, min(w, cw * 4)), max(16, min(h, ch * 4))
            x, y = r.x(), r.y()
        else:
            w, h = max(16, min(w, cw * 4)), max(16, min(h, ch * 4))
            x, y = r.x(), r.y()
        self.pend = QRectF(x, y, w, h)
        self.relayout()

    def _swap(self):
        w, h = self.ed_w.text(), self.ed_h.text()
        self.ed_w.setText(h)
        self.ed_h.setText(w)
        self._apply_fields()

    def _ok(self):
        self._disarm_reset()
        if self.ed_w.hasFocus() or self.ed_h.hasFocus():
            self._apply_fields()
        orig_color = xf_color(self.xf) if self.xf else DEFAULT_BG
        # emit on a geometry change OR a color-only change (pend stays None if only the swatch was used)
        if self.pend is not None or self.bg_color != orig_color:
            cur = self.pend if self.pend is not None else self._cur_units()
            pre = self._t().inverted()[0].mapRect(cur)            # displayed -> pre-rotation units
            k, rc = self._k, self._rc
            self.committed.emit(self.tool, QRectF(rc.x() + pre.x() * k, rc.y() + pre.y() * k,
                                                  pre.width() * k, pre.height() * k), self.bg_color)
        self.okClicked.emit()

    def _layout_pic(self, p, canvas_off=(0.0, 0.0)):
        """p = picture rect in pre-rotation canvas units."""
        xf, k = self._eff(), self._k
        self.video.set_layout((xf[0] * k, xf[1] * k), (p.x() * k, p.y() * k, p.width() * k, p.height() * k),
                              self._rot(), bool(self.fx[1]), canvas_off=canvas_off)

    def live_picture(self, r):
        k, rc = self._k or 1.0, self._rc
        u = QRectF((r.x() - rc.x()) / k, (r.y() - rc.y()) / k, r.width() / k, r.height() / k)
        self._layout_pic(self._t().inverted()[0].mapRect(u))

    # [MAP] Central layout routine. Branch 1 - untouched clip (no xf, no tool, no rotation/mirror): everything
    # fills the stage and the plain-fit VideoView mode is used (the "fast path"; keep it byte-for-byte cheap, it
    # runs during normal playback). Branch 2 - clip with crop/resize/rotate/mirror or a tool active: compute the
    # canvas rect (MARGIN 40 px while editing), apply zoom/pan, lay the picture out with the QTransform mode.
    # The native video widget is masked to visible_rect() (picture ^ clip_rect ^ canvas) so a crop stays
    # hidden regardless of canvas size; Video-mode crop editing additionally widens it to the full picture
    # (the un-crop "ghost") since that's the maximal recoverable extent.
    # Ends by showing/raising overlay + bar only while a tool is active.
    def relayout(self):
        xf, S = self._eff(), self.rect()
        fx_on = self._rot() != 0 or bool(self.fx[1])
        editing = self.tool is not None and xf is not None
        # while editing, preview the swatch's PENDING color (even pre-OK); otherwise show the committed one
        bgc = self.bg_color if editing else (xf_color(self.xf) if self.xf else DEFAULT_BG)
        self.video.set_bg_color(bgc)
        # `canvas` (behind vclip/video) now needs its own matching color too: once clip_rect can be
        # smaller than canvas_rect (Canvas mode), the area between them is no longer always covered by
        # video's own background brush (see the vrc/visible_rect() split below), so it has to show
        # through from this layer instead. Was hardcoded black before clip_rect existed.
        pal = self.canvas.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(bgc))
        self.canvas.setPalette(pal)
        if xf is None or (self.xf is None and not editing and not fx_on):     # untouched clip: fill like before
            self.canvas.setGeometry(S)
            self.vclip.setGeometry(S)
            self.video.setGeometry(0, 0, S.width(), S.height())
            self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            self.video.set_layout(None)
            self._rc, self._k = QRectF(S), 1.0
            self._canvas_off = (0.0, 0.0)
        else:
            cw, ch, px, py, pw, ph = xf[:6]
            dw, dh = self._disp_dims()
            m = self.MARGIN if editing else 0
            av = S.adjusted(m, m, -m, -m)
            k0 = min(av.width() / dw, av.height() / dh)
            zoom = self._zoom if editing else 1.0
            k = k0 * zoom
            # [FEATURE] Pan is unbounded ("infinite") while a tool is active, at any zoom level
            # (including fit) - no wall to hit. Only reset to centered when no tool is editing.
            if not editing:
                self._pan = QPointF(0.0, 0.0)
            cx, cy = av.center().x() + self._pan.x(), av.center().y() + self._pan.y()
            rc = QRectF(cx - dw * k / 2, cy - dh * k / 2, dw * k, dh * k).toRect()
            self.canvas.setGeometry(rc)
            self._k = rc.width() / dw
            self._rc = QRectF(rc)
            if editing and self.tool == "crop" and self.crop_mode == "video":
                # Let a ghost of anything already cropped away show through: widen the native video
                # widget to the full recoverable picture extent instead of clipping it to the
                # (already-cropped) clip_rect. self._rc/self._k - the canvas<->screen mapping every
                # crop selection maths runs in - stay exactly as computed above. visible_rect() is
                # always a subset of picture_rect(), so widening to the picture is also the maximal
                # bound - equivalent to the old rc.united(picture_rect()) now that clip_rect (not
                # canvas_rect) is what's being widened away from. Canvas mode never ghosts: clip_rect
                # doesn't move there, so there's nothing to recover.
                vrc = self.picture_rect().toRect()
            else:
                # Normal playback, Resize tool, or Canvas-mode crop editing: mask the video down to
                # visible_rect() - picture INTERSECT clip_rect INTERSECT canvas - so anything the crop
                # hid stays hidden no matter how big the canvas itself is. This is what decouples a
                # Canvas-mode resize from the crop: growing/shrinking canvas_rect only changes rc
                # (below) and the pad math in `canvas`'s own background, never this rect.
                vrc = self.visible_rect().toRect()
            self._canvas_off = (rc.x() - vrc.x(), rc.y() - vrc.y())
            self.vclip.setGeometry(vrc)
            self.video.setGeometry(0, 0, vrc.width(), vrc.height())
            self.video.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
            if editing and self.tool == "resize" and self.pend is not None:
                pic = self._t().inverted()[0].mapRect(self.pend)
            else:
                pic = QRectF(px, py, pw, ph)
            self._layout_pic(pic, self._canvas_off)
        self.vclip.show()                       # the video view is a normal widget: overlay sits on top
        self.vclip.raise_()
        self.still.hide()
        self.overlay.setGeometry(self.rect())
        self.overlay.setVisible(editing)
        self.overlay.raise_()
        self.overlay.reset_selection()
        self.bar.setVisible(editing)
        if editing:
            self.b_rot.setVisible(self.tool == "resize")
            self.b_mode.setVisible(self.tool == "crop")
            self.b_color.setVisible(self.tool == "crop")   # [FEATURE] no bg-color swatch for Resize
            self._sync_fields()
            self.bar.adjustSize()
            self.bar.move(S.width() - self.bar.width() - 10, S.height() - self.bar.height() - 6)
            self.bar.raise_()
        self.overlay.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.relayout()

    # [MAP] Wheel zoom for Crop/Resize, anchored on the cursor (same technique as Timeline.set_zoom(anchor_x)). Clamped to
    # 0.5..6.0 so the canvas can never be zoomed out of reach; pan is derived so the point under the cursor stays put.
    def wheel_zoom(self, pos, dy):
        """Mouse-wheel zoom for the crop/resize tools, anchored on the cursor. Clamped to
        [0.5 (half of fit) .. 6.0] so the canvas can still never be zoomed away entirely."""
        if self.tool not in ("crop", "resize") or self._rc.width() <= 0 or self._rc.height() <= 0:
            return
        fx = (pos.x() - self._rc.x()) / self._rc.width()
        fy = (pos.y() - self._rc.y()) / self._rc.height()
        new_zoom = max(0.5, min(6.0, self._zoom * (1.12 if dy > 0 else 1 / 1.12)))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        self._zoom = new_zoom
        dw, dh = self._disp_dims()
        if dw <= 0 or dh <= 0:
            return
        S = self.rect()
        av = S.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        k = min(av.width() / dw, av.height() / dh) * self._zoom
        new_w, new_h = dw * k, dh * k
        cx = pos.x() - fx * new_w + new_w / 2
        cy = pos.y() - fy * new_h + new_h / 2
        self._pan = QPointF(cx - av.center().x(), cy - av.center().y())
        self.relayout()

    # [FEATURE] Pan the view by dragging empty space (CropOverlay.mousePressEvent/mouseMoveEvent's "pan"
    # drag) - same _pan the wheel-zoom above already uses, so relayout()'s existing zoom>1 clamp (and the
    # "always centered at fit zoom" reset) applies here too: no-op unless the view is actually zoomed in.
    def pan_by(self, base_pan, delta):
        self._pan = QPointF(base_pan.x() + delta.x(), base_pan.y() + delta.y())
        self.relayout()


# [MAP] Amber warning pills next to Help. set(key, text, tip) shows/updates, set(key, None) clears. At most MAX (3)
# pills; if more are active ORDER ["perf","ram","disk"] decides which win. Keys in use: perf (Engine.perfChanged),
# ram + disk (MainWindow.check_system, with hysteresis so pills don't flicker).
class WarningBar(QWidget):
    """Small amber warning pills shown next to the Help button. Up to MAX side by side;
    if more are active, the highest-priority ones (ORDER) win. Use set(key, text, tip) / set(key, None)."""
    MAX = 3
    ORDER = ["perf", "ram", "disk"]

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._active = {}
        self._pills = []
        for _ in range(self.MAX):
            p = QLabel()
            p.setObjectName("warnPill")
            p.hide()
            lay.addWidget(p)
            self._pills.append(p)

    def set(self, key, text=None, tip=""):
        if text is None:
            if self._active.pop(key, None) is None:
                return
        else:
            self._active[key] = (text, tip)
        keys = sorted(self._active, key=lambda k: self.ORDER.index(k) if k in self.ORDER else 99)
        for i, p in enumerate(self._pills):
            if i < len(keys) and i < self.MAX:
                p.setText(self._active[keys[i]][0])
                p.setToolTip(self._active[keys[i]][1])
                p.show()
            else:
                p.hide()

    def has(self, key):
        return key in self._active


