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
import collections
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

# ----------------------------------------------------------------------------- timeline (single video track)
# [MAP] Custom-painted single-track timeline: ruler, clip blocks with filmstrip thumbnails and option badges,
# playhead, and ALL mouse editing (scrub, select, trim, move, razor, drop files, context menu).
# Talks to the rest of the app ONLY through signals (seekRequested, splitRequested, deleteRequested,
# copyRequested, pasteRequested, filesDropped, zoomChanged, thumbReady, trimBlocked, clipOptionsRequested) and
# by reading/mutating `seq`. It never touches the Engine.
# COORDINATES: tx(t) = HW + t*pps - scroll_x  (timeline seconds -> widget x);  xt(x) is the inverse.
# `pps` = pixels per second (1..800). HW (6 px) is a small left gutter. `scroll_x` is in pixels.
# MOUSE MODES (self.mode, set on press, cleared on release):
#   "scrub"  drag in the ruler                 -> seekRequested (Engine.scrub, adaptive)
#   "trim_in"/"trim_out"  drag a clip edge (only if the clip is > 20 px wide, 6 px grab zone)
#   "move"   drag a clip body (reorders; the clip "lifts" and follows the mouse, its slot shows a ghost)
# [INVARIANT] Drag editing pattern: on press store `_snap0 = seq.snapshot()`; while dragging mutate Seg objects
# directly and emit seq.live (repaint only); on release, if the snapshot changed -> seq.commit(_snap0) +
# seq.edited.emit() (=> ONE undo step per drag, and only then does the Engine re-seek).
# [PITFALL] While a drag is running the Engine still shows the old state; do not read the Engine in drag code.
class Timeline(QWidget):
    seekRequested = Signal(float)
    splitRequested = Signal(float)
    duplicateRequested = Signal()       # [52.20] right-click > Duplicate Clip (Ctrl+D)
    blankRequested = Signal(float)      # [52.17] right-click > Add Blank Clip ([52.21] time = cut point nearest the CLICK)
    deleteRequested = Signal()
    copyRequested = Signal()
    pasteRequested = Signal()
    filesDropped = Signal(list, float)
    zoomChanged = Signal(float)
    thumbReady = Signal(str, str)
    trimBlocked = Signal(str)
    clipOptionsRequested = Signal(int)
    loopToggled = Signal(bool)
    locksChanged = Signal()             # [52.25] a row lock icon was toggled
    _hues = None

    HW, RULER_H = 6, 22
    HEADER_W = 96                          # [52.25] gutter width while row headers are on (instance attr `HW` then shadows the class 6)
    V_Y, V_H = RULER_H + 3, 92
    # [FEATURE 51.10] The clip row height (instance attr `V_H`, shadows this class default) follows the widget height:
    # V_H_FULL when the timeline is tall enough, shrinking down to a minimum widget height of 40% of the full one
    # (60% smaller). Below THUMB_MIN_H the filmstrip thumbnails are not drawn (or requested) at all.
    V_H_FULL = 92
    MIN_H_FRAC = 0.40
    THUMB_MIN_H = 58
    BAND_H = 16                            # top label strip inside each clip; rest is thumbnail
    TILE_W = 64                            # target width of one filmstrip thumbnail
    MOVE_ARM_PX = 4.0                      # mouse must move this far past a clip-body press before it
                                            # "lifts" for a drag - stops a plain/double click from jerking

    def __init__(self, seq):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)   # so keys (Delete...) go to the timeline once clicked
        self.seq = seq
        self.pps = 40.0
        self.scroll_x = 0.0
        self.playhead = 0.0
        self.sel = -1
        self.tool = "select"
        self.hover_x = None
        self.drop_x = None
        self.mode = None
        self._marq, self._marq_base = None, set()      # [52.6] marquee (rubber-band) selection rect / selection kept when Ctrl is held
        self.bar = None
        self.loop = False
        self.multi_sel = set()   # extra selected clip indices (Ctrl+click), alongside self.sel
        self._move_set = []      # indices being dragged together (single clip, multi-sel, or a named group)
        self.thumb_pix = collections.OrderedDict()   # key -> pre-scaled QPixmap (capped, see THUMB_CACHE_MAX)
        self.thumb_pending = set()
        # [PERF] Filmstrip thumbnails used to spawn one thread + one ffmpeg PER tile (up to 48 at once, hundreds in a
        # zoom animation) - the source of the ffmpeg.exe CPU spikes. Now: 2 worker threads pull from this deque,
        # NEWEST request first (what's on screen now beats what was on screen a second ago), capped at
        # THUMB_QUEUE_MAX (oldest dropped). Each ffmpeg runs at low priority with 1 thread (see gen_thumb_file).
        self._thumb_jobs = collections.deque()
        self._thumb_cv = threading.Condition()
        self._thumb_started = False
        # [PERF] Ruler+clips (the expensive part: one filmstrip + badge set per clip) are rendered into this
        # cached QPixmap and only re-rendered when something that actually changes their appearance happens
        # (see _invalidate_content). The playhead/hover/drop-marker overlay is cheap and still drawn fresh every
        # paintEvent, which is what keeps scrubbing (and normal 66x/sec playback ticks) from having to repaint
        # every clip's filmstrip on every single frame - the #1 cost on a busy (many-clip) timeline.
        self._content_pix = None
        self._content_size = (0, 0)
        self._content_dirty = True
        self._ph_x = -1             # last painted playhead x (partial-repaint bookkeeping, see set_playhead)
        # [PERF] Mouse-move coalescing: mouseMoveEvent only records the latest position; the real work (scrub
        # seek request, trim/move, hover/cursor) runs once per event-loop turn on the newest position via this
        # 0 ms single-shot timer, so a high-polling-rate mouse can't queue up hundreds of redundant scrub seeks
        # per frame. mousePress/Release flush or drop the pending position so clicks never act on a stale one.
        self._pending_pos = None
        self._move_timer = QTimer(self)
        self._move_timer.setSingleShot(True)
        self._move_timer.setInterval(0)
        self._move_timer.timeout.connect(self._flush_move)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.headers = False             # [52.25] Advanced mode: row headers (names + lock icons) in the left gutter, see set_headers
        self.lanes = []                  # [52.9] extra rows under the clip row (mods): see lane_h / refresh_lanes
        self.vtrack = None               # [52.27] overlay video tracks (video_track.VideoTrack) while Advanced is on
        self.atrack = None               # [52.19] audio track strip ABOVE the clip row (audio_track.AudioTrack)
        self._lane_grab = None
        self.ghost = None                # [52.36] cross-row drag feedback (dict: rect/target/new/ok/label/color) painted over the content
        self.V_H = self.V_H_FULL
        self.setMinimumHeight(int((self.V_Y + self.V_H_FULL + 8) * self.MIN_H_FRAC))
        self.setFont(QFont("Segoe UI", 8))
        seq.edited.connect(self._on_seq)
        seq.live.connect(self._on_seq)
        self._zoom_anchor_x, self._zoom_anchor_t = None, 0.0
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(180)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(self._apply_zoom)
        self._zoom_anim.finished.connect(self._invalidate_content)    # request thumbnails once zoom settles

    # --- coordinates
    def tx(self, t):
        return self.HW + t * self.pps - self.scroll_x

    def xt(self, x):
        return (x - self.HW + self.scroll_x) / self.pps

    def _clamp_t(self, t):
        return max(0.0, min(self.seq.total(), t))

    # [FEATURE] While scrubbing the ruler (seeking), snap the playhead onto a clip edge when it's
    # dragged within SNAP_PX screen pixels of one.
    def _snap_seek_t(self, t):
        st = self.seq.starts()
        extra = []
        for ln in self._all_lanes():           # [52.16] lane items (text, audio) start/end are snap targets too
            extra += getattr(ln, "snap_points", lambda: [])()
        if not st and not extra:
            return t
        i = bisect.bisect_left(st, t)          # nearest edge is one of t's two neighbours or the end
        cands = [self.seq.total()] + extra
        if i > 0:
            cands.append(st[i - 1])
        if i < len(st):
            cands.append(st[i])
        best = min(cands, key=lambda e: abs(e - t))
        return best if abs(best - t) <= 8.0 / self.pps else t

    # [PERF] Call whenever something that changes how the cached ruler+clips pixmap looks happens (segs added/
    # removed/reordered/retrimmed, selection, scroll, zoom, grouping, thumbnails arriving...). Cheap calls that
    # only move the playhead/hover/drop overlay (scrubbing, normal playback ticks) should keep calling plain
    # self.update() instead - see paintEvent/_render_content.
    def _invalidate_content(self):
        self._content_dirty = True
        self.update()

    # --- state
    def _on_seq(self):
        if self.sel >= len(self.seq.segs):
            self.sel = -1
        self.multi_sel = {i for i in self.multi_sel if i < len(self.seq.segs)}
        self._update_bar()
        self._invalidate_content()

    def set_tool(self, tool):
        self.tool = tool
        self.setCursor(Qt.CursorShape.CrossCursor if tool == "razor" else Qt.CursorShape.ArrowCursor)
        self.update()

    # [MAP] Called from MainWindow.on_playhead. `follow=True` (only while the Engine is playing) auto-scrolls when
    # the playhead leaves the visible area; it is suppressed during a mouse drag (self.mode is not None).
    def set_playhead(self, t, follow=False):
        self.playhead = t
        if follow and self.mode is None:
            x = self.tx(t)
            if x > self.width() - 30 or x < self.HW:
                new_scroll = max(0.0, t * self.pps - 60)
                if new_scroll != self.scroll_x:
                    self.scroll_x = new_scroll
                    self._update_bar()
                    self._content_dirty = True    # view actually shifted - clips moved on screen
        # [PERF] Only the strip around the old and new playhead needs repainting (the cached pixmap supplies the rest);
        # a dirty cache (scroll shift etc.) still repaints everything.
        nx = int(self.tx(t))
        ox, self._ph_x = self._ph_x, nx
        if self._content_dirty or ox < 0:
            self.update()
        else:
            self.update(min(ox, nx) - 9, 0, abs(nx - ox) + 19, self.height())

    # [MAP] Keeps the external QScrollBar (created in MainWindow.build_timeline_panel and assigned to self.bar) in
    # sync: content width = total*pps + 240 px slack. Signals are blocked while syncing to avoid feedback loops.
    def _update_bar(self):
        view = max(1, self.width() - self.HW)
        content = int(self.seq.total() * self.pps + 240)
        mx = max(0, content - view)
        self.scroll_x = max(0.0, min(self.scroll_x, float(mx)))
        if self.bar:
            self.bar.blockSignals(True)
            self.bar.setRange(0, mx)
            self.bar.setPageStep(view)
            self.bar.setValue(int(self.scroll_x))
            self.bar.blockSignals(False)

    def on_scroll(self, v):
        self.scroll_x = float(v)
        self._invalidate_content()

    # [MAP] Zoom keeping the timeline time under `anchor_x` fixed. animate=True tweens pps over 180 ms (OutCubic) via
    # QVariantAnimation; _zoom_anchor_t/_zoom_anchor_x are captured ONCE per zoom so the anchor point stays fixed for
    # the whole animation. Callers that are already continuous (zoom slider drag) or run during load
    # (auto-fit on first clip) pass animate=False. zoomChanged fires every animation tick, which is what makes the
    # slider glide (MainWindow.sync_zoom).
    # [PITFALL] Do not connect a clicked(bool) signal straight to zoom_fit - the bool would land in `animate`.
    # (The Fit button uses a lambda for exactly this reason.)
    def set_zoom(self, pps, anchor_x=None, animate=True):
        pps = max(0.02, min(800.0, pps))
        if anchor_x is None:
            anchor_x = self.tx(self.playhead)  # keep the playhead fixed on screen by default
        self._zoom_anchor_x = anchor_x
        self._zoom_anchor_t = self.xt(anchor_x)          # timeline position under the anchor - held fixed
        if not animate or abs(pps - self.pps) < 0.05:
            self._zoom_anim.stop()
            self._apply_zoom(pps)
            return
        self._zoom_anim.stop()
        self._zoom_anim.setStartValue(self.pps)
        self._zoom_anim.setEndValue(pps)
        self._zoom_anim.start()

    def _apply_zoom(self, pps):
        anchor_x = self._zoom_anchor_x if self._zoom_anchor_x is not None else self.tx(self.playhead)
        self.pps = float(pps)
        self.scroll_x = max(0.0, self._zoom_anchor_t * self.pps - (anchor_x - self.HW))
        self._update_bar()
        self.zoomChanged.emit(self.pps)
        self._invalidate_content()

    def zoom_fit(self, animate=True):
        total = max(self.seq.total(), 1.0)
        view = max(50, self.width() - self.HW - 80)
        self.scroll_x = 0.0
        self.set_zoom(view / total, anchor_x=self.HW, animate=animate)

    # [52.9] LANES: objects (mods, e.g. the Text tool) that own a strip under the clip row. Lane API (height required, rest optional):
    #   height(tl) -> px   paint(p, tl, y, W)   press(e, tl) -> bool (True = handled; left button = lane keeps the drag)
    #   move(pos, tl)   release(e, tl)   dbl(e, tl) -> bool.  Painted into the cached content pixmap -> call tl._invalidate_content().
    def lane_h(self):
        return sum(int(l.height(self)) for l in self.lanes)

    def lane_y(self):
        return self.V_Y + self.V_H + 4

    # [52.20] Lane items (text, audio) snap to the VIDEO clip edges (every cut + the end) within SNAP_LANE_PX screen pixels,
    # both while moving (start OR end snaps) and while trimming (the edge being dragged).
    SNAP_LANE_PX = 8

    def snap_t(self, t):
        if not self.seq.segs:
            return t
        pts = self.seq.starts() + [self.seq.total()]
        b = min(pts, key=lambda q: abs(q - t))
        return b if abs(b - t) * self.pps <= self.SNAP_LANE_PX else t

    def snap_span(self, t0, dur):
        """New start for an item [t0, t0+dur] whose start or end snaps to a video edge (nearest wins)."""
        d = [x for x in (self.snap_t(t0) - t0, self.snap_t(t0 + dur) - (t0 + dur)) if abs(x) > 1e-9]
        return t0 + min(d, key=abs) if d else t0

    def lane_pts(self):
        """[52.21] Edges of every lane item (text rows + audio strip) - video clips snap to these too."""
        out = []
        for ln in self._all_lanes():
            f = getattr(ln, "snap_points", None)
            if f:
                out += f()
        return out

    def sel_layers(self):
        """[52.21] Set of layer keys that currently hold a selection ("video", ("text", row), "audio")."""
        L = {"video"} if (self.multi_sel or self.sel >= 0) else set()
        for ln in self._all_lanes():
            f = getattr(ln, "sel_layers", None)
            if f:
                L |= f()
        return L

    # ---- [52.25] row headers + locks (Advanced mode; a normal timeline never calls set_headers, so none of this is visible)
    # [MAP] Lock keys live in seq.locked: "video" (clip row), "audio" (strip above it), lane.lock_key (any lane object; default
    # ("lane", ClassName)). Enforcement: Sequence.edit(layer=...) refuses video edits; mouse presses on a locked row do nothing;
    # key handlers ask unlocked_lanes(). A lane that wants a header name sets `name`; a stable key sets `lock_key`.
    def set_headers(self, on):
        on = bool(on)
        if on == self.headers:
            return
        self.headers = on
        if on:
            self.HW = self.HEADER_W
        else:
            self.__dict__.pop("HW", None)
            if self.seq.locked:                          # normal mode must never carry hidden locks
                self.seq.locked.clear()
                self.locksChanged.emit()
        self._update_bar()
        self._invalidate_content()
        self.update()

    def lane_key(self, ln):
        return "audio" if ln is self.atrack else (getattr(ln, "lock_key", None) or ("lane", type(ln).__name__))

    def lane_locked(self, ln):
        return self.lane_key(ln) in self.seq.locked

    def unlocked_lanes(self):
        return [ln for ln in self._all_lanes() if not self.lane_locked(ln)]

    def rows_ex(self):
        """[(key, label, y, h, owner, idx)] of every visible row, top to bottom: audio strip (one row per track), video row,
        then the lanes. owner = the lane object when it supports add_track() (gets a "+" on its first row), else None.
        [52.26] Lane contract: optional add_track(), remove_track(i), track_rows(tl) -> [(label, y offset, h)]."""
        out = []
        at = self.atrack
        if at and at.height(self):
            y0 = self.RULER_H + 2
            for i, (lb, yo, h) in enumerate(at.track_rows(self)):
                out.append(("audio", lb, y0 + yo, int(h), at, i))
        vt = self.vtrack
        if vt and vt.height(self):                                  # [52.29] overlay tracks sit ABOVE the base Video row
            y0 = self.strip_y(vt)
            for i, (lb, yo, h) in enumerate(vt.track_rows(self)):
                out.append((self.lane_key(vt), lb, y0 + yo, int(h), vt, i))
        out.append(("video", "Video", self.V_Y, int(self.V_H), vt, 0))   # [52.27] own = overlay tracks -> "+" here
        y = self.lane_y()
        for ln in self.lanes:
            h = int(ln.height(self))
            if h:
                own = ln if hasattr(ln, "add_track") else None
                tr = ln.track_rows(self) if hasattr(ln, "track_rows") else [(getattr(ln, "name", None) or "Layer", 0, h)]
                for i, (lb, yo, rh) in enumerate(tr):
                    out.append((self.lane_key(ln), lb, y + yo, int(rh), own, i))
            y += h
        return out

    def rows(self):
        return [r[:4] for r in self.rows_ex()]

    def _plus_rect(self, y, h):
        return QRectF(self.HW - 37, y + max(1.0, (min(h, 22) - 12) / 2.0), 12, 12)

    @staticmethod
    def _draw_plus(p, r):
        p.save()
        p.setPen(QPen(QColor("#cfcfcf"), 1.6))
        c = r.center()
        p.drawLine(QLineF(r.left() + 2, c.y(), r.right() - 2, c.y()))
        p.drawLine(QLineF(c.x(), r.top() + 2, c.x(), r.bottom() - 2))
        p.restore()

    def _lock_rect(self, y, h):
        return QRectF(self.HW - 21, y + max(1.0, (min(h, 22) - 12) / 2.0), 12, 12)

    def toggle_lock(self, key):
        self.seq.locked.symmetric_difference_update({key})
        self.locksChanged.emit()
        self._invalidate_content()
        self.update()

    def _paint_headers(self, p, H):
        hw, fm = self.HW, p.fontMetrics()
        p.fillRect(0, 0, hw, H, QColor("#232323"))
        p.setPen(QPen(QColor("#111111"), 1))
        p.drawLine(QLineF(hw - 0.5, 0, hw - 0.5, H))
        for key, label, y, h, own, idx in self.rows_ex():
            locked = key in self.seq.locked
            plus = own is not None and idx == 0 and (key == "video" or getattr(own, "header_plus", True))   # [52.29] "+" on the base row
            tw = hw - (46 if plus else 30)
            p.fillRect(QRectF(1, y, hw - 3, h), QColor("#2c2c2c"))
            p.setPen(QColor("#6f6f6f" if locked else "#cfcfcf"))
            p.drawText(QRectF(6, y, tw, min(h, 22)), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(label, Qt.TextElideMode.ElideRight, tw))
            if plus:
                self._draw_plus(p, self._plus_rect(y, h))                # [52.26] add another track of this kind
            self._draw_lock(p, self._lock_rect(y, h), locked)

    @staticmethod
    def _draw_lock(p, r, locked):
        c = QColor("#e8a33d" if locked else "#7d7d7d")
        body = QRectF(r.left(), r.top() + r.height() * 0.45, r.width(), r.height() * 0.55)
        sx, ex, top = r.left() + r.width() * 0.25, r.right() - r.width() * 0.25, r.top() + 1
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(body, 2, 2)
        path = QPainterPath()
        path.moveTo(sx, body.top())
        path.lineTo(sx, top + 3)
        path.cubicTo(sx, top - 1.5, ex, top - 1.5, ex, top + 3)
        path.lineTo(ex, body.top() + 1 if locked else body.top() - 3)      # open padlock: right leg lifted
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c, 1.6))
        p.drawPath(path)
        p.restore()

    def top_h(self):
        return int(self.atrack.height(self)) if self.atrack else 0

    # [52.29] STRIPS above the clip row, top to bottom: audio strip (atrack), then the overlay video tracks (vtrack) directly above
    # the base Video row. strip_y(ln) = y of the first pixel row of that strip.
    def vtop_h(self):
        return int(self.vtrack.height(self)) if self.vtrack else 0

    def strip_y(self, ln):
        return self.RULER_H + 2 + (self.top_h() if ln is self.vtrack else 0)

    def strips(self):
        return [ln for ln in (self.atrack, self.vtrack) if ln is not None]

    def _all_lanes(self):
        return self.lanes + self.strips()

    def refresh_lanes(self):
        self.V_Y = self.RULER_H + 3 + self.top_h() + self.vtop_h()   # [52.19] clip row moves down under the audio strip ([52.29] and the overlay tracks)
        lh = self.lane_h()
        self.setMinimumHeight(int((self.V_Y + self.V_H_FULL + 8) * self.MIN_H_FRAC) + lh)
        self.V_H = max(self.BAND_H + 1, min(self.V_H_FULL, self.height() - self.V_Y - 8 - lh))
        self._invalidate_content()

    def resizeEvent(self, e):
        self._update_bar()
        vh = max(self.BAND_H + 1, min(self.V_H_FULL, self.height() - self.V_Y - 8 - self.lane_h()))
        if vh != self.V_H:
            self.V_H = vh
            self._invalidate_content()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.pps * (1.15 if dy > 0 else 1 / 1.15), anchor_x=self.tx(self.playhead))
        else:
            self.scroll_x = max(0.0, self.scroll_x - dy)
            self._update_bar()
            self._invalidate_content()

    # --- hit testing
    # [MAP] Hit test -> (segment index, edge) where edge is "in", "out" or None. Only inside the clip row
    # (V_Y..V_Y+V_H); the ruler is handled separately in mousePressEvent. Edges only exist on clips wider than 20 px.
    def hit(self, pos):
        x, y = pos.x(), pos.y()
        if x < self.HW or not (self.V_Y <= y <= self.V_Y + self.V_H):
            return None, None
        starts = self.seq.starts()
        for i, s in enumerate(self.seq.segs):
            x0, x1 = self.tx(starts[i]), self.tx(starts[i] + s.dur)
            if x0 <= x < x1:
                edge = None
                if x1 - x0 > 20:
                    if x - x0 <= 6:
                        edge = "in"
                    elif x1 - x <= 6:
                        edge = "out"
                return i, edge
        return None, None

    # --- clip grouping (visual only; never affects export)
    # [FIX] A selection may only be grouped if every EXISTING group it touches is fully contained in it -
    # otherwise part of that group would be pulled into a new/combined group while the rest of it stays
    # behind, leaving two groups interleaved on the timeline (a group must always be one contiguous run).
    # idxs must already be sorted + contiguous.
    def _group_selectable(self, idxs):
        idx_set = set(idxs)
        touched = {self.seq.segs[i].grp for i in idxs if self.seq.segs[i].grp is not None}
        return all({i for i, s in enumerate(self.seq.segs) if s.grp == gid} <= idx_set for gid in touched)

    # [52.22] Cross-layer groups: seg.grp / item.grp share ONE id space (Sequence.groups). _group_plan() returns
    # (video idxs, [(lane, item)], touched group ids) or None when grouping is not allowed: < 2 members, video part not
    # one contiguous run, or an existing group would be split (every touched group must be fully inside the selection).
    def _lane_sel(self):
        return [(ln, o) for ln in self._all_lanes() if getattr(ln, "sel_items", None) for o in ln.sel_items()]

    def _group_plan(self):
        segs = self.seq.segs
        v = sorted(i for i in (set(self.multi_sel) | ({self.sel} if self.sel >= 0 else set())) if 0 <= i < len(segs))
        ls = self._lane_sel()
        if len(v) + len(ls) < 2 or (v and v != list(range(v[0], v[-1] + 1))):
            return None
        touched = {segs[i].grp for i in v if segs[i].grp is not None} | {o.grp for _, o in ls if o.grp is not None}
        vset, lset = set(v), {id(o) for _, o in ls}
        for gid in touched:
            if not {i for i, s in enumerate(segs) if s.grp == gid} <= vset:
                return None
            for ln in self._all_lanes():
                if any(o.grp == gid and id(o) not in lset for o in getattr(ln, "all_items", lambda: [])()):
                    return None
        return v, ls, touched

    def _sel_gid(self):
        segs = self.seq.segs
        for i in sorted(set(self.multi_sel) | ({self.sel} if self.sel >= 0 else set())):
            if 0 <= i < len(segs) and segs[i].grp is not None:
                return segs[i].grp
        return next((o.grp for _, o in self._lane_sel() if o.grp is not None), None)

    def select_group(self, gid, add=False):
        """Select every member of group gid on every layer (add=True keeps the current selection)."""
        vs = {i for i, s in enumerate(self.seq.segs) if s.grp == gid}
        self.multi_sel = (set(self.multi_sel) | vs) if add else vs
        self.sel = min(self.multi_sel) if self.multi_sel else -1
        for ln in self._all_lanes():
            f = getattr(ln, "select_grp", None)
            if f:
                f(gid, add)
        self._keep_vsel = True
        self._invalidate_content()

    def expand_groups(self):
        """A selection that touches a group always means the WHOLE group (Ctrl+click on one member must not split it)."""
        segs = self.seq.segs
        g = {segs[i].grp for i in (set(self.multi_sel) | {self.sel}) if 0 <= i < len(segs)}
        g |= {o.grp for _, o in self._lane_sel()}
        for gid in g - {None}:
            self.select_group(gid, add=True)

    def group_selected(self):
        plan = self._group_plan()
        if not plan:
            return
        v, ls, touched = plan
        gid = next(iter(touched)) if len(touched) == 1 else self.seq.new_group()
        for i in v:
            self.seq.segs[i].grp = gid
        for _, o in ls:
            o.grp = gid
        self._invalidate_content()

    def ungroup_selected(self):
        gid = self._sel_gid()
        if gid is None:
            return
        for s in self.seq.segs:
            if s.grp == gid:
                s.grp = None
        for ln in self._all_lanes():
            for o in getattr(ln, "all_items", lambda: [])():
                if o.grp == gid:
                    o.grp = None
        self.seq.groups.pop(gid, None)
        self._invalidate_content()

    def rename_group(self, idx=None):
        if idx is None:
            idx = self.sel
        if not (0 <= idx < len(self.seq.segs)):
            return
        gid = self.seq.segs[idx].grp
        if gid is None:
            return
        cur = self.seq.groups.get(gid, {}).get("name", "")
        name, ok = QInputDialog.getText(self, "Rename Group", "Group name:", QLineEdit.EchoMode.Normal, cur)
        if ok:
            self.seq.groups.setdefault(gid, {"color": "#888888", "name": ""})["name"] = name.strip()
            self._invalidate_content()

    # [FEATURE] Right-click "Change Group Color": pops a color picker seeded with the group's current
    # color; picking one re-tints everything that shares this group id (bottom bar, name pill, and the
    # 20% body tint added in _draw_clip).
    def change_group_color(self, idx=None):
        if idx is None:
            idx = self.sel
        if not (0 <= idx < len(self.seq.segs)):
            return
        gid = self.seq.segs[idx].grp
        if gid is None:
            return
        cur = QColor(self.seq.groups.get(gid, {}).get("color", "#888888"))
        c = QColorDialog.getColor(cur, self, "Pick group color")
        if c.isValid():
            self.seq.groups.setdefault(gid, {"color": "#888888", "name": ""})["color"] = c.name()
            self._invalidate_content()

    # --- mouse
    # [MAP] Left button: ruler -> scrub; Razor tool -> emit splitRequested(time under mouse) if over a clip; empty
    # area -> deselect; otherwise select the clip and enter trim_in / trim_out / move. Right button -> context menu
    # (add edit, copy, paste, ripple delete; same actions as the shortcuts). Focus is taken on click
    # (ClickFocus) so the Delete key is routed here - see MainWindow.handle_delete_key.
    # Starting a trim_in in Snap mode before keyframes are loaded emits trimBlocked (status-bar hint only; _trim
    # then ignores the drag).
    def mousePressEvent(self, e):
        self._keep_vsel = False                           # [52.22] set by lane presses that keep the video selection
        self._move_timer.stop()
        self._pending_pos = None
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = e.position()
        x, y = pos.x(), pos.y()
        if e.button() == Qt.MouseButton.MiddleButton:      # [FEATURE 51.1] middle-drag pans the timeline
            self.mode = "pan"
            self._pan_x0, self._pan_s0 = x, self.scroll_x
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self.headers and x < self.HW:                    # [52.25] gutter: the lock icons
            if e.button() == Qt.MouseButton.LeftButton:
                for key, _lb, ry, rh, own, idx in self.rows_ex():
                    if self._lock_rect(ry, rh).adjusted(-5, -5, 5, 5).contains(pos):
                        self.toggle_lock(key)
                        break
                    if own is not None and idx == 0 and (key == "video" or getattr(own, "header_plus", True)) and self._plus_rect(ry, rh).adjusted(-4, -4, 4, 4).contains(pos):
                        if self.lane_locked(own):
                            self.seq.blocked.emit(str(key))
                        else:
                            own.add_track()                         # [52.26]
                        break
            elif e.button() == Qt.MouseButton.RightButton:          # [52.26] right-click a track header: add / remove track
                for key, _lb, ry, rh, own, idx in self.rows_ex():
                    if own is not None and ry <= y < ry + rh:
                        m = QMenu(self)
                        a_add = m.addAction("Add track")
                        a_rem = m.addAction("Remove this track (must be empty)") if (hasattr(own, "remove_track") and key != "video") else None
                        act = m.exec(e.globalPosition().toPoint())
                        if self.lane_locked(own) and act is not None:
                            self.seq.blocked.emit(str(key))
                        elif act is a_add:
                            own.add_track()
                        elif a_rem is not None and act is a_rem:
                            own.remove_track(idx)
                        break
            return
        if x >= self.HW and e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            for st in self.strips():                         # [52.19] audio strip / [52.29] overlay video tracks above the clip row
                y0 = self.strip_y(st)
                if not (st.height(self) and y0 <= y < y0 + st.height(self)):
                    continue
                if self.lane_locked(st):                     # [52.25] locked strip: clicks do nothing
                    return
                if st.press(e, self):
                    if not self._keep_vsel:
                        self.sel, self.multi_sel = -1, set()
                    self._invalidate_content()
                    self._lane_grab = st if e.button() == Qt.MouseButton.LeftButton else None
                    return
        if self.lanes and y > self.V_Y + self.V_H and x >= self.HW and e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            ly = self.lane_y()
            for ln in self.lanes:                            # [52.25] a click on a locked lane's rows is swallowed
                lh = int(ln.height(self))
                if self.lane_locked(ln) and ly <= y < ly + lh:
                    return
                ly += lh
            for ln in self.lanes:
                if self.lane_locked(ln):
                    continue
                if ln.press(e, self):
                    if not self._keep_vsel:
                        self.sel, self.multi_sel = -1, set()  # [52.14] one selection across all layers
                        for st in self.strips():
                            st.clear_sel(self)
                    self._invalidate_content()
                    self._lane_grab = ln if e.button() == Qt.MouseButton.LeftButton else None
                    return
        h0, _e0 = self.hit(pos)
        keep_l = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier) or (
            e.button() == Qt.MouseButton.RightButton and h0 is not None and h0 in self.multi_sel)
        if (self.lanes or self.strips()) and not keep_l:
            for ln in self._all_lanes():                   # a click outside the lanes drops their selection
                getattr(ln, "clear_sel", lambda tl: None)(self)
        if e.button() == Qt.MouseButton.RightButton:
            idx, _ = self.hit(pos)
            if idx is not None:
                if idx not in self.multi_sel:
                    self.multi_sel = {idx}
                self.sel = idx
                self._invalidate_content()
            menu = QMenu(self)
            a_split = menu.addAction("Add Edit at Playhead\tShift+C")
            a_copy = menu.addAction("Copy Clip\tCtrl+C")
            a_copy.setEnabled(idx is not None)
            a_paste = menu.addAction("Paste Clip\tCtrl+V")
            a_dup = menu.addAction("Duplicate Clip\tCtrl+D")
            a_dup.setEnabled(idx is not None)
            a_blank = menu.addAction("Add Blank Clip")
            a_ov = menu.addAction("Send to overlay Video track") if (self.vtrack is not None and idx is not None) else None   # [52.27]
            a_del = menu.addAction("Ripple Delete\tDel")
            a_del.setEnabled(idx is not None)
            menu.addSeparator()
            cur_grp = self.seq.segs[idx].grp if idx is not None else None
            a_group = a_ungroup = a_rename = a_color = None
            plan = self._group_plan()                      # [52.22] works across video + text + audio layers
            if plan:
                a_group = menu.addAction("Add to Group" if len(plan[2]) == 1 else "Group Selected Clips")
            if cur_grp is not None:
                a_rename = menu.addAction("Rename Group...\tF2")
                a_color = menu.addAction("Change Group Color...")
            if self._sel_gid() is not None:
                a_ungroup = menu.addAction("Ungroup")
            if a_group or a_rename:
                menu.addSeparator()
            a_loop = menu.addAction("Loop Playback")
            a_loop.setCheckable(True)
            a_loop.setChecked(self.loop)
            act = menu.exec(e.globalPosition().toPoint())
            if act == a_split:
                self.splitRequested.emit(self.playhead)
            elif act == a_copy:
                self.copyRequested.emit()
            elif act == a_paste:
                self.pasteRequested.emit()
            elif act == a_del:
                self.deleteRequested.emit()
            elif act is not None and act == a_dup:
                self.duplicateRequested.emit()
            elif a_ov is not None and act is a_ov:
                self.vtrack.send_seg(idx)
            elif act is not None and act == a_blank:
                ct = self.xt(x)                      # [52.21] exception: insert at the gap/cut nearest the right-click, not the playhead
                pts = (self.seq.starts() + [self.seq.total()]) if self.seq.segs else [0.0]
                self.blankRequested.emit(min(pts, key=lambda q: abs(q - ct)))
            elif act == a_loop:
                self.loop = a_loop.isChecked()
                self.loopToggled.emit(self.loop)
            elif act is not None and act == a_group:
                self.group_selected()
            elif act is not None and act == a_ungroup:
                self.ungroup_selected()
            elif act is not None and act == a_rename:
                self.rename_group(idx)
            elif act is not None and act == a_color:
                self.change_group_color(idx)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if y < self.RULER_H and x >= self.HW:
            self.mode = "scrub"
            self.seekRequested.emit(self._clamp_t(self._snap_seek_t(self.xt(x))))
            return
        idx, edge = self.hit(pos)
        if self.tool == "razor":
            if idx is not None:
                self.splitRequested.emit(self.xt(x))
            return
        if idx is None:
            keep = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if self.tool not in ("crop", "resize") and y >= self.RULER_H and x >= self.HW:
                # [52.6] Hold left click on empty space and drag = selection box (Ctrl = add to the current selection)
                self.mode = "marquee"
                self._marq_base = set(self.multi_sel) if keep else set()
                self._marq_keep = keep
                self._marq0 = QPointF(max(x, self.HW), y)
                self._marq = QRectF(self._marq0, self._marq0)
                if not keep:
                    self.sel = -1
                    self.multi_sel.clear()
                self._invalidate_content()
                return
            self.sel = -1
            self.multi_sel.clear()
            self._invalidate_content()
            return
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier and edge is None:
            # Ctrl+click toggles exactly this clip. Keep sel inside multi_sel so logical and visual
            # selection can never diverge.
            if idx in self.multi_sel:
                self.multi_sel.discard(idx)
                self.sel = min(self.multi_sel) if self.multi_sel else -1
            else:
                self.multi_sel.add(idx)
                self.sel = idx
            self._invalidate_content()
            return
        seg = self.seq.segs[idx]
        # Plain click always replaces the previous selection. Grouped clips expand to their group;
        # ungrouped clips select only themselves.
        if seg.grp is not None:
            self.multi_sel = {i for i, s in enumerate(self.seq.segs) if s.grp == seg.grp}
        else:
            self.multi_sel = {idx}
        self.sel = idx
        if seg.grp is not None:
            self.select_group(seg.grp)                     # [52.22] ...and the group's text/audio members
            self.sel = idx
        if self.tool in ("crop", "resize"):
            # [FIX] Crop/Resize edits a single frame in place - starting a trim or move here would
            # change the very clip being edited out from under the tool. Selection still works
            # (so the right-click menu / group actions keep functioning), dragging doesn't.
            self._invalidate_content()
            return
        if "video" in self.seq.locked:                      # [52.25] locked: selecting is fine, trim/move is not
            self._invalidate_content()
            return
        self._snap0 = self.seq.snapshot()
        self._press_x = x
        if edge == "in":
            self.mode, self._orig = "trim_in", seg.in_s
            if self.seq.snap and not self.seq.precise and seg.media.keyframes is None:
                self.trimBlocked.emit(seg.media.name)
        elif edge == "out":
            self.mode, self._orig = "trim_out", seg.out_s
        else:
            self.mode = "move"
            # Dragging a grouped clip drags every clip that shares its group id together.
            if seg.grp is not None:
                self._move_set = sorted(i for i, s in enumerate(self.seq.segs) if s.grp == seg.grp)
            else:
                self._move_set = sorted(self.multi_sel) if len(self.multi_sel) > 1 else [idx]
            # [FIX B3] _grab is the mouse's pixel offset from the LEFT EDGE OF THE WHOLE DRAGGED BLOCK, fixed at
            # HALF the block's own width - not at wherever inside it was clicked - so the block's CENTRE tracks
            # the mouse for the whole drag, regardless of which clip/spot inside it was grabbed.
            self._mv_orig = list(self.seq.segs)                # [52.35] original order: a drop onto an overlay row restores it first
            self._mv_t0 = sum(q.dur for q in self.seq.segs[:min(self._move_set)])     # [52.22] lane members follow on release
            bundle_dur = sum(self.seq.segs[k].dur for k in self._move_set)
            self._grab = bundle_dur * self.pps / 2
            # [FIX] Don't lift/follow the mouse yet - see mouseMoveEvent's MOVE_ARM_PX check. Lifting
            # (and switching the cursor) right on press made a plain or double click visibly "jerk" the
            # clip for one frame even though nothing was actually dragged.
            self._mx = None
        self._invalidate_content()

    def mouseMoveEvent(self, e):
        self._pending_pos = e.position()
        if not self._move_timer.isActive():
            self._move_timer.start()

    def _flush_move(self):
        pos, self._pending_pos = self._pending_pos, None
        if pos is not None:
            self._process_move(pos)

    def _process_move(self, pos):
        if self._lane_grab is not None:
            self._lane_grab.move(pos, self)
            return
        x = pos.x()
        if self.mode == "pan":
            self.scroll_x = max(0.0, self._pan_s0 - (x - self._pan_x0))
            self._update_bar()              # clamps scroll_x to the scrollable range and syncs the bar
            self._invalidate_content()
            return
        self.hover_x = x if self.tool == "razor" else None
        if self.mode == "marquee":
            self._marq = QRectF(self._marq0, QPointF(max(x, self.HW), pos.y())).normalized()
            self._marquee_select()
            self.update()
            return
        if self.mode == "scrub":
            self.seekRequested.emit(self._clamp_t(self._snap_seek_t(self.xt(x))))
        elif self.mode in ("trim_in", "trim_out"):
            self._trim(x)
        elif self.mode == "move":
            if self._mx is None:
                if abs(x - self._press_x) < self.MOVE_ARM_PX:
                    return   # still within click tolerance - don't lift yet (see MOVE_ARM_PX)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)   # drag is actually starting now
            self._mx = x
            self._move(x)
            self._ghost_base(pos)
        else:
            if self.tool == "razor":
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                idx, edge = self.hit(pos)
                lc = None
                for ln in self._all_lanes():               # [52.13] lanes may set their own hover cursor
                    lc = getattr(ln, "cursor", lambda p, t: None)(pos, self)
                    if lc is not None:
                        break
                self.setCursor(lc if lc is not None else
                               (Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.ArrowCursor))
        # [PERF] "scrub" only moves the playhead overlay (cheap, see paintEvent) and the hover-only "else"
        # branch only changes the cursor/razor hover line (also overlay-only) - both stay a plain update().
        # trim/move mutate the clip geometry or drag the lifted ghost, which live in the cached pixmap, so
        # those need a real invalidate even on moves too small to have committed a seq.live (e.g. the ghost
        # sliding under the mouse before any reorder actually happens).
        if self.mode in ("trim_in", "trim_out", "move"):
            self._invalidate_content()
        else:
            self.update()

    # [INVARIANT] The single place where a drag becomes an undo step: compares the live snapshot with _snap0 and
    # commits only if something really changed (a click without movement creates no undo entry).
    def mouseReleaseEvent(self, e):
        self.ghost = None
        self._move_timer.stop()
        self._flush_move()      # apply the final mouse position before the drag is committed
        if self._lane_grab is not None:
            g, self._lane_grab = self._lane_grab, None
            g.release(e, self)
            return
        sent = self.mode == "move" and self._drop_on_overlay(e)
        if sent:
            self.mode = None
        if self.mode == "move" and self._move_set and getattr(self, "_mv_t0", None) is not None:
            dt = sum(q.dur for q in self.seq.segs[:min(self._move_set)]) - self._mv_t0
            if abs(dt) > 1e-6:                             # [52.22] selected text/audio group members shift with the block
                for ln in self._all_lanes():
                    if getattr(ln, "sel_items", None) and ln.sel_items():
                        ln.shift_sel(dt)
        if self.mode in ("trim_in", "trim_out", "move") and not sent:
            if self.seq.snapshot() != self._snap0:
                self.seq.commit(self._snap0)
                self.seq.edited.emit()
        if self.mode in ("move", "pan") or sent:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.mode = None
        self._marq = None
        self._mx = None
        self._move_set = []
        self._invalidate_content()

    # [52.6] Clips touched by the marquee (grouped clips pull in their whole group, like a plain click) + the Ctrl base.
    def _marquee_select(self):
        r, segs, starts = self._marq, self.seq.segs, self.seq.starts()
        hit = set()
        if r.bottom() >= self.V_Y and r.top() <= self.V_Y + self.V_H:
            for i, s in enumerate(segs):
                if self.tx(starts[i] + s.dur) >= r.left() and self.tx(starts[i]) <= r.right():
                    hit.add(i)
        grps = {segs[i].grp for i in hit if segs[i].grp is not None}
        if grps:
            hit |= {i for i, s in enumerate(segs) if s.grp in grps}
        new = hit | self._marq_base
        for ln in self._all_lanes():               # [52.21] box select spans ALL layers (video + text + audio) at once
            getattr(ln, "marquee", lambda *a: None)(r, self, getattr(self, "_marq_keep", False))
        if new != self.multi_sel:
            self.multi_sel = new
            self.sel = min(new) if new else -1
            self._invalidate_content()

    def select_all(self):
        """[52.6] Ctrl+A: every clip."""
        self.multi_sel = set(range(len(self.seq.segs)))
        self.sel = 0 if self.multi_sel else -1
        self._invalidate_content()

    def select_only(self, idx):
        """[52.6] Select exactly one clip (ignores its group) - used by the Up/Down edit navigation."""
        for ln in self._all_lanes():               # [52.15] one selection across all layers
            getattr(ln, "clear_sel", lambda tl: None)(self)
        self.multi_sel = {idx}
        self.sel = idx
        self._invalidate_content()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.tool == "razor":
            return
        for st in self.strips():
            y0 = self.strip_y(st)
            if st.height(self) and y0 <= e.position().y() < y0 + st.height(self):
                if self.lane_locked(st):                  # [52.25]
                    return
                if st.dbl(e, self):                       # [52.19] audio-only clip options / [52.29] overlay clip options
                    return
        if self.lanes and e.position().y() > self.V_Y + self.V_H:
            for ln in self.lanes:
                if not self.lane_locked(ln) and ln.dbl(e, self):
                    return
        idx, _ = self.hit(e.position())
        if idx is not None and "video" in self.seq.locked:     # [52.25] clip options are an edit
            self.seq.blocked.emit("video")
            return
        if idx is not None:
            seg = self.seq.segs[idx]
            self.sel = idx
            self.multi_sel = ({i for i, s in enumerate(self.seq.segs) if s.grp == seg.grp}
                              if seg.grp is not None else {idx})
            self.mode = None
            self._invalidate_content()
            self.clipOptionsRequested.emit(idx)

    def leaveEvent(self, e):
        self.hover_x = None
        self.update()

    # [MAP] Trim maths: v = original + dx/pps * speed (dx in pixels converts to SOURCE seconds through speed).
    # trim_out: clamp to [in+MIN_DUR, media.dur] - no keyframe needed (a stream-copy END may be anywhere).
    # trim_in: clamp to [0, out-MIN_DUR]; in Snap mode it must land on a keyframe: keyframes not loaded yet ->
    # ignore the drag; no reachable keyframe -> ignore, so the preview never promises a cut the lossless export
    # cannot make. Precise mode skips snapping entirely (but see F1 for what that costs at export).
    # Emits seq.live only (repaint); commit happens on mouse release.
    def _trim(self, x):
        s = self.seq.segs[self.sel]
        m = s.media
        v = self._orig + (x - self._press_x) / self.pps * s.speed
        if self.mode == "trim_out":
            s.out_s = max(s.in_s + MIN_DUR, min(m.dur, v))
        else:
            v = max(0.0, min(s.out_s - MIN_DUR, v))
            if self.seq.snap and not self.seq.precise:
                if m.keyframes is None:
                    return  # keyframes still analyzing - ignore rather than show a trim the export can't match
                k = m.nearest_kf(v, -1e-3, s.out_s - MIN_DUR)
                if k is None:
                    return  # no reachable keyframe here - ignore so the preview never promises a cut the lossless export can't make
                v = k
            s.in_s = v
        if self.mode == "trim_out" or not (self.seq.snap and not self.seq.precise):
            self._snap_trim_end(s)
        self.seq.live.emit()

    def _snap_trim_end(self, s):
        # [52.21] the clip's END (the edge that moves while trimming) snaps to text/audio lane edges
        pts = self.lane_pts()
        if not pts:
            return
        end = sum(q.dur for q in self.seq.segs[:self.sel]) + s.dur
        p = min(pts, key=lambda q: abs(q - end))
        if abs(p - end) * self.pps > self.SNAP_LANE_PX:
            return
        dt = (p - end) * s.speed
        if self.mode == "trim_out":
            s.out_s = max(s.in_s + MIN_DUR, min(s.media.dur, s.out_s + dt))
        else:
            s.in_s = max(0.0, min(s.out_s - MIN_DUR, s.in_s - dt))

    # [MAP] Reorder while dragging: the dragged clip's centre is compared with the midpoints of the OTHER clips to
    # compute its new index; segs are re-ordered in place (list pop/insert) and `sel` follows. Live only.
    # A group (or an explicit multi-selection) is dragged as one contiguous block: the whole bundle is pulled out,
    # its combined centre is compared against the midpoints of the remaining clips, and it is reinserted together
    # (in its original relative order) at that position.
    def _move(self, x):
        segs = self.seq.segs
        move_set = self._move_set if len(self._move_set) > 1 else [self.sel]
        move_set_set = set(move_set)
        bundle = [segs[k] for k in move_set]
        bundle_dur = sum(s.dur for s in bundle)
        others = [s for k, s in enumerate(segs) if k not in move_set_set]
        # 'others' is grouped into atomic BLOCKS: a run of consecutive clips sharing one group id
        # moves and reinserts as a single unit, so the dragged clip/bundle can only land before,
        # after, or between whole blocks - never spliced into the middle of another group.
        blocks = []
        for s in others:
            if blocks and s.grp is not None and blocks[-1][-1].grp == s.grp:
                blocks[-1].append(s)
            else:
                blocks.append([s])
        c = self.xt(x - self._grab) + bundle_dur / 2
        acc, new_block_i = 0.0, 0
        for blk in blocks:
            blk_dur = sum(s.dur for s in blk)
            if acc + blk_dur / 2 < c:
                new_block_i += 1
            acc += blk_dur
        pts = self.lane_pts()                      # [52.21] snap the dragged block's start/end to text/audio lane edges
        if pts:
            cum = [0.0]
            for blk in blocks:
                cum.append(cum[-1] + sum(s.dur for s in blk))
            g0, tol, bd = self.xt(x - self._grab), self.SNAP_LANE_PX / self.pps, None
            for p_ in pts:
                for off in (0.0, bundle_dur):
                    gd = abs(g0 + off - p_)
                    if gd > tol or (bd is not None and gd >= bd[0]):
                        continue
                    ks = min(range(len(cum)), key=lambda k: abs(cum[k] + off - p_))
                    if abs(cum[ks] + off - p_) < abs(cum[new_block_i] + off - p_):
                        bd = (gd, ks)
            if bd:
                new_block_i = bd[1]
        new_segs = ([s for blk in blocks[:new_block_i] for s in blk] + bundle +
                    [s for blk in blocks[new_block_i:] for s in blk])
        if new_segs != segs:
            segs[:] = new_segs
            new_i = sum(len(blk) for blk in blocks[:new_block_i])
            self._move_set = list(range(new_i, new_i + len(bundle)))
            self.multi_sel = set(self._move_set)
            self.sel = self._move_set[0]
            self.seq.live.emit()

    # --- drag & drop of files
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        self.drop_x = e.position().x()
        self.update()
        e.acceptProposedAction()

    # [52.35] Advanced: a base-row clip dragged UP onto an overlay row (or just above the top one = new track) becomes an overlay
    # clip there (VideoTrack.send_seg). Returns True when it handled the drop (the base order is restored first, so the whole
    # thing is ONE undo step made by send_seg). A refused send (locked / not a plain clip) leaves the clip where it was.
    def _ghost_base(self, pos):
        """[52.36] While a base-row clip is dragged up into the overlay strip, show it under the mouse + the target row."""
        self.ghost = None
        vt = self.vtrack
        if (vt is None or not vt.enabled or len(self._move_set) != 1 or "video" in self.seq.locked or self.lane_locked(vt)
                or self._move_set[0] >= len(self.seq.segs) or pos.x() < self.HW):
            return
        y0, py = self.strip_y(vt), pos.y()
        if not (y0 - 20 <= py < self.V_Y - 3):
            return
        seg = self.seq.segs[self._move_set[0]]
        new = py < y0
        r = max(0, min(vt.ntracks - 1, int((py - y0) // (vt.H + 3)))) if vt.ntracks else 0
        tgt = QRectF(self.HW, y0 - 4, self.width() - self.HW, 6) if (new or not vt.ntracks) else QRectF(self.HW, y0 + r * (vt.H + 3), self.width() - self.HW, vt.H)
        w = max(8.0, seg.dur * self.pps)
        self.ghost = {"rect": QRectF(pos.x() - self._grab, py - vt.H / 2.0, w, vt.H), "target": tgt, "new": new or not vt.ntracks,
                      "label": seg.media.name, "color": self._clip_colors(seg)[0].name()}

    def _drop_on_overlay(self, e):
        vt, pos = self.vtrack, e.position()
        orig = getattr(self, "_mv_orig", None)
        if (vt is None or not vt.enabled or orig is None or len(self._move_set) != 1 or "video" in self.seq.locked
                or pos.x() < self.HW or self.lane_locked(vt)):
            return False
        y0, py = self.strip_y(vt), pos.y()
        if not (y0 - 20 <= py < self.V_Y - 3):
            return False
        seg = self.seq.segs[self._move_set[0]] if self._move_set[0] < len(self.seq.segs) else None
        if seg is None or seg not in orig:
            return False
        track = "new" if py < y0 else vt.row_track(max(0, min(vt.ntracks - 1, int((py - y0) // (vt.H + 3)))))
        idx, t = orig.index(seg), max(0.0, self.xt(pos.x() - self._grab))
        self.seq.segs[:] = orig
        self.multi_sel, self.sel, self._mv_orig = set(), -1, None
        self.seq.live.emit()
        vt.send_seg(idx, track, t)
        return True

    def dragLeaveEvent(self, e):
        self.drop_x = None
        self.update()

    # [MAP] Files (from Explorer or from the Project list) dropped on the timeline -> filesDropped(paths, time).
    # MainWindow.on_files_dropped imports them (dedup by path) and inserts at that time.
    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        t = self.xt(e.position().x())
        self.drop_x = None
        self.update()
        if paths and self.headers:                                  # [52.27] dropped onto an overlay track row -> that track
            py = e.position().y()
            for key, _lb, ry, rh, own, idx in self.rows_ex():
                if key != "video" and own is not None and hasattr(own, "drop_files") and ry <= py < ry + rh:
                    if self.lane_locked(own):
                        self.seq.blocked.emit(str(key))
                    else:
                        own.drop_files(paths, max(0.0, t), idx)
                    e.acceptProposedAction()
                    return
        if paths:
            self.filesDropped.emit(paths, max(0.0, t))
            e.acceptProposedAction()

    # --- clip thumbnails (bottom half of each clip block)
    # [MAP] Filmstrip thumbnails: at most 48 outstanding requests; each runs gen_thumb_file in its own short-lived
    # daemon thread and reports through thumbReady (connected in MainWindow.__init__ to _on_thumb_ready). Not
    # requested while a mouse drag is running (self.mode is not None) to keep dragging smooth.
    # [KNOWN ISSUE F5] The key is f"{media.path}|{t rounded to 0.1 s}" with no mtime/size, and gen_thumb_file caches
    # on disk by that key, so thumbnails are STALE after Save-Over or any external change of the file. If you change
    # the key format keep the "path|" prefix - clear_thumbs_for matches on it.
    THUMB_WORKERS = 2
    THUMB_QUEUE_MAX = 96
    THUMB_CACHE_MAX = 1200          # in-memory pixmaps; evicted ones come back from the on-disk jpg cache (no ffmpeg)

    def request_thumb(self, media, t, key):
        if key in self.thumb_pending or key in self.thumb_pix or not FFMPEG or not media.has_video:
            return
        self.thumb_pending.add(key)
        with self._thumb_cv:
            if not self._thumb_started:
                self._thumb_started = True
                for _ in range(self.THUMB_WORKERS):
                    threading.Thread(target=self._thumb_loop, daemon=True).start()
            self._thumb_jobs.append((media.path, t, key))
            while len(self._thumb_jobs) > self.THUMB_QUEUE_MAX:
                self.thumb_pending.discard(self._thumb_jobs.popleft()[2])     # stale: will be re-requested if visible
            self._thumb_cv.notify()

    def _thumb_loop(self):
        while True:
            with self._thumb_cv:
                while not self._thumb_jobs:
                    self._thumb_cv.wait()
                path, t, key = self._thumb_jobs.pop()          # newest first
            if key not in self.thumb_pending:                  # cleared meanwhile (media removed/renamed/dropped)
                continue
            try:
                fn = gen_thumb_file(path, t, key, "quickcut_clipthumbs", width=140)
                if fn:
                    self.thumbReady.emit(key, fn)
            except Exception:
                pass
            finally:
                self.thumb_pending.discard(key)

    def _on_thumb_ready(self, key, filepath):
        h = max(1, self.V_H_FULL - self.BAND_H)      # cached at full height; _draw_clip scales down when the row is shrunk
        pm = QPixmap(filepath)
        if not pm.isNull():
            self.thumb_pix[key] = pm.scaledToHeight(h, Qt.TransformationMode.SmoothTransformation)
            while len(self.thumb_pix) > self.THUMB_CACHE_MAX:
                self.thumb_pix.popitem(last=False)
        self._invalidate_content()

    # [MAP] Drops in-memory thumbnails (and pending markers) for one media path. Call whenever a file's contents or
    # path change (remove, rename, Save-Over, clear). It does NOT clear the on-disk cache [F5].
    def clear_thumbs_for(self, path):
        for k in [k for k in self.thumb_pix if k.startswith(path + "|")]:
            del self.thumb_pix[k]
        for k in [k for k in self.thumb_pending if k.startswith(path + "|")]:
            self.thumb_pending.discard(k)

    # --- painting
    # [PERF] Ruler+clips are rendered once into self._content_pix and reused until _invalidate_content() marks it
    # dirty (segs changed, selection/scroll/zoom/grouping changed, a thumbnail arrived, or the widget was
    # resized). scrubbing and normal playback ticks call plain self.update() - which does NOT dirty the cache -
    # so on every one of those repaints this is just a blit of the cached pixmap, no matter how many clips are on
    # the timeline. Keep any new per-clip drawing inside _paint_ruler/_paint_clips (the cached part); anything
    # that must track the mouse/playhead every frame belongs in paintEvent's overlay section below instead.
    def _render_content(self, W, H):
        dpr = self.devicePixelRatioF()
        if self._content_pix is not None and self._content_size == (W, H, dpr):
            pm = self._content_pix              # same size: repaint in place (whole area is refilled) - no realloc
        else:
            pm = QPixmap(max(1, round(W * dpr)), max(1, round(H * dpr)))
            pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hw = self.HW
        p.fillRect(0, 0, W, H, QColor("#1e1e1e"))
        p.fillRect(hw, self.V_Y, W - hw, self.V_H, QColor("#181818"))
        p.save()
        p.setClipRect(hw, 0, W - hw, H)
        self._paint_ruler(p, W)
        self._paint_clips(p)
        for st in self.strips():
            try:
                st.paint(p, self, self.strip_y(st), W)
            except Exception:
                pass
        y = self.lane_y()
        for ln in self.lanes:
            try:
                ln.paint(p, self, y, W)
            except Exception:
                pass
            y += int(ln.height(self))
        for key, _lb, ry, rh in (self.rows() if self.seq.locked else ()):      # [52.25] locked rows are dimmed
            if key in self.seq.locked:
                p.fillRect(QRectF(hw, ry, W - hw, rh), QColor(0, 0, 0, 105))
        if not self.seq.segs:
            p.setPen(QColor("#6c6c6c"))
            p.drawText(QRectF(hw, self.V_Y, W - hw, self.V_H),
                       Qt.AlignmentFlag.AlignCenter,
                       "Drag clips here from the Project panel")
        p.restore()
        if self.headers:
            self._paint_headers(p, H)                      # [52.26] LAST: nothing scrolled under the gutter can smear over it
        p.end()
        self._content_pix = pm
        self._content_size = (W, H, self.devicePixelRatioF())
        self._content_dirty = False

    # [MAP] Paint order: cached ruler+clips pixmap -> empty-state hint (part of the cache) -> razor hover line ->
    # file-drop marker -> playhead. Only the overlay (hover/drop/playhead) is drawn fresh every repaint; see
    # _render_content for the cached part.
    def paintEvent(self, ev):
        W, H, hw = self.width(), self.height(), self.HW
        size_key = (W, H, self.devicePixelRatioF())
        if self._content_dirty or self._content_pix is None or self._content_size != size_key:
            self._render_content(W, H)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self._content_pix)
        p.save()
        p.setClipRect(hw, 0, W - hw, H)
        if self.hover_x is not None and self.hover_x > hw:
            p.setPen(QPen(QColor("#ff5050"), 1))
            p.drawLine(QLineF(self.hover_x, self.RULER_H, self.hover_x, self.V_Y + self.V_H + 6))
        if self.drop_x is not None:
            p.setPen(QPen(QColor("#ffb020"), 2, Qt.PenStyle.DashLine))
            p.drawLine(QLineF(self.drop_x, self.RULER_H, self.drop_x, H))
        g = self.ghost
        if g:                                                         # [52.36] a clip being dragged between rows
            col = QColor("#ffb020" if g.get("ok", True) else "#e05050")
            tr = g.get("target")
            if tr is not None:
                p.setPen(QPen(col, 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 40))
                p.drawRoundedRect(tr, 3, 3)
                if g.get("new"):
                    p.setPen(col)
                    p.drawText(QPointF(max(hw, 0) + 8, tr.center().y() - 4), "+ new track")
            r = g["rect"]
            p.setOpacity(0.85)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.setBrush(QColor(g.get("color", "#7b62b0")))
            p.drawRoundedRect(r, 3, 3)
            if r.width() > 30:
                p.setPen(QColor("#101010"))
                p.drawText(QPointF(r.x() + 5, r.center().y() + 4), p.fontMetrics().elidedText(g.get("label", ""), Qt.TextElideMode.ElideRight, int(r.width() - 10)))
            p.setOpacity(1.0)
        x = self.tx(self.playhead)
        p.setPen(QPen(QColor("#2d8ceb"), 1.5))
        p.drawLine(QLineF(x, self.RULER_H, x, H))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2d8ceb"))
        p.drawPolygon(QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x + 6, 12),
                                 QPointF(x, 19), QPointF(x - 6, 12)]))
        if self._marq is not None:
            p.setPen(QPen(QColor("#2d8ceb"), 1))
            p.setBrush(QColor(45, 140, 235, 45))
            p.drawRect(self._marq)
        p.restore()

    def _paint_ruler(self, p, W):
        hw = self.HW
        p.fillRect(hw, 0, W - hw, self.RULER_H, QColor("#2a2a2a"))
        p.setPen(QColor("#111"))
        p.drawLine(hw, self.RULER_H, W, self.RULER_H)
        fps = self.seq.fps()
        c = next((x for x in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
                  if x * self.pps >= 100), 7200)
        t0 = max(0.0, self.xt(hw))
        k = int(math.floor(t0 / c))
        p.setPen(QColor("#8a8a8a"))
        while True:
            t = k * c
            x = self.tx(t)
            if x > W:
                break
            if x >= hw - 90:
                p.setPen(QColor("#8a8a8a"))
                p.drawLine(QLineF(x, self.RULER_H - 10, x, self.RULER_H))
                p.drawText(QPointF(x + 4, self.RULER_H - 4), fmt_tc(t, fps, frames=c < 1))
            p.setPen(QColor("#5a5a5a"))
            for j in range(1, 5):
                xm = self.tx(t + c * j / 5)
                if hw <= xm <= W:
                    p.drawLine(QLineF(xm, self.RULER_H - 4, xm, self.RULER_H))
            k += 1

    # [MAP] Each source FILE gets its own hue (golden-ratio spacing keeps neighbours distinct); clips from the same
    # file get a slightly different tone derived from in_s. The hue table is keyed by media.path [stale after a
    # rename - cosmetic]. Audio-only media are drawn nearly grey.
    def _clip_colors(self, s):
        """Each source file gets its own hue (golden-ratio spacing = always well separated);
        clips cut from the same file share it with a slightly different tone."""
        if self._hues is None:
            self._hues = {}
        h = self._hues.get(s.media.path)
        if h is None:
            h = self._hues[s.media.path] = (len(self._hues) * 0.61803398875 + 0.08) % 1.0
        k = (int(s.in_s * 2) * 7) % 5 - 2                    # stable per clip: -2..+2
        sat = 0.5 if s.media.has_video else 0.08
        band = QColor.fromHslF((h + k * 0.008) % 1.0, sat, 0.70 + k * 0.03)
        body = QColor.fromHslF(h, sat * 0.6, 0.13 + k * 0.006)
        return band, body

    # [COUPLING] Draws the small option pills (speed, mute, reverse, mirror, rotation) right-aligned in the clip's
    # label strip and returns the new right edge for the file-name text. Add a badge here when adding a Seg option.
    # Badges only draw when the clip is wider than 60 px. A crop/resize badge does not exist yet (listed as not
    # implemented in the notes).
    def _paint_badges(self, p, s, r, rx):
        """Mini icons for this clip's options, right-aligned in its label strip."""
        y, h = r.y() + 2, self.BAND_H - 4
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if abs(s.speed - 1.0) > 1e-6:
            f = p.font()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            lab = f"{s.speed:g}x"
            w = p.fontMetrics().horizontalAdvance(lab) + 18
            pill = QRectF(rx - w, y, w, h)
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            p.setBrush(QColor("#ffffff"))
            for dx in (0, 4):
                x = pill.x() + 4 + dx
                p.drawPolygon(QPolygonF([QPointF(x, y + 2.5), QPointF(x, y + h - 2.5), QPointF(x + 4, y + h / 2)]))
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(pill.x() + 13, y, w - 14, h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lab)
            p.setPen(Qt.PenStyle.NoPen)
            rx -= w + 3
        if s.mute:
            w = 19
            pill = QRectF(rx - w, y, w, h)
            x0 = pill.x()
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            p.setBrush(QColor("#ffffff"))
            p.drawPolygon(QPolygonF([QPointF(x0 + 4, y + 4), QPointF(x0 + 6.5, y + 4), QPointF(x0 + 10, y + 1.5),
                                     QPointF(x0 + 10, y + h - 1.5), QPointF(x0 + 6.5, y + h - 4),
                                     QPointF(x0 + 4, y + h - 4)]))
            p.setPen(QPen(QColor("#ff6b6b"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QLineF(x0 + 3, y + h - 2, x0 + w - 3, y + 2))
            rx -= w + 3
        if s.rev:
            w = 19
            pill = QRectF(rx - w, y, w, h)
            x0 = pill.x()
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            p.setBrush(QColor("#ffffff"))
            p.drawPolygon(QPolygonF([QPointF(x0 + 9, y + 2.5), QPointF(x0 + 9, y + h - 2.5), QPointF(x0 + 3, y + h / 2)]))
            p.drawPolygon(QPolygonF([QPointF(x0 + 16, y + 2.5), QPointF(x0 + 16, y + h - 2.5), QPointF(x0 + 10, y + h / 2)]))
            rx -= w + 3
        if s.mirror:
            w = 19
            pill = QRectF(rx - w, y, w, h)
            x0 = pill.x()
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            p.setBrush(QColor("#ffffff"))
            p.drawPolygon(QPolygonF([QPointF(x0 + 8, y + 2.5), QPointF(x0 + 8, y + h - 2.5), QPointF(x0 + 3, y + h - 2.5)]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), 1.1))
            p.drawPolygon(QPolygonF([QPointF(x0 + 11, y + 2.5), QPointF(x0 + 11, y + h - 2.5), QPointF(x0 + 16, y + h - 2.5)]))
            p.setPen(Qt.PenStyle.NoPen)
            rx -= w + 3
        if abs(s.rot % 360.0) > 1e-6:
            f = p.font()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            lab = f"{s.rot % 360.0:g}\u00b0"
            w = p.fontMetrics().horizontalAdvance(lab) + 18
            pill = QRectF(rx - w, y, w, h)
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            cx, cy = pill.x() + 8, y + h / 2
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), 1.1))
            p.drawEllipse(QPointF(cx, cy), 3.6, 3.6)
            p.drawLine(QLineF(cx, cy, cx + 2.2, cy - 2.2))
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(pill.x() + 14, y, w - 15, h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lab)
            p.setPen(Qt.PenStyle.NoPen)
            rx -= w + 3
        # [FEATURE] Volume (+/-dB) badge, same pill style as the others.
        if abs(s.vol_db) > 1e-6:
            f = p.font()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            lab = f"{s.vol_db:+.0f}dB"
            w = p.fontMetrics().horizontalAdvance(lab) + 14
            pill = QRectF(rx - w, y, w, h)
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            p.setPen(QColor("#ffffff"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, lab)
            p.setPen(Qt.PenStyle.NoPen)
            rx -= w + 3
        # [FEATURE] "M" badge when Track Type is Mono (a clip with audio at all - mono on a silent clip is moot).
        if s.track_type == "mono" and s.media.acodec.strip():
            w = 16
            pill = QRectF(rx - w, y, w, h)
            p.setBrush(QColor(16, 20, 28, 225))
            p.drawRoundedRect(pill, 3, 3)
            f = p.font()
            f.setPointSizeF(7.0)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, "M")
            p.setPen(Qt.PenStyle.NoPen)
            rx -= w + 3
        p.restore()
        return rx

    # [FEATURE] While a clip/bundle is held with the mouse, it is drawn lowered ~40% of the clip row's height so
    # the track underneath (its ghost slot) stays visible above it.
    DRAG_DROP_Y = 0.4

    def _paint_clips(self, p):
        starts = self.seq.starts()
        fm = p.fontMetrics()
        move_set = set(self._move_set) if self.mode == "move" and getattr(self, "_mx", None) is not None else set()
        group_label_idx = {}
        for i, s in enumerate(self.seq.segs):
            if s.grp is not None and s.grp not in group_label_idx:
                group_label_idx[s.grp] = i
        lifted = []
        # [FEATURE] Selection/highlight boxes merge into one border across a run of contiguously-selected clips
        # that share the same REAL group id (s.grp) - groups are always stored contiguously, so a selected group
        # draws as one merged box. [FIX] A plain multi-selection of clips that are NOT actually grouped (e.g.
        # Ctrl+click on two adjacent, unrelated clips) must never merge into one box just because they happen to
        # sit next to each other and are both selected - that looked exactly like a real group. run_grp tracks
        # the group id the currently-open run belongs to; a run only extends into the next clip when it shares
        # that id, so ungrouped selected clips each get their own separate border even when adjacent.
        sel_runs, run_start, run_grp, last_r = [], None, None, None

        def _close_run(end_r):
            nonlocal run_start
            if run_start is not None:
                sel_runs.append(QRectF(run_start.x(), run_start.y(), end_r.right() - run_start.x(), run_start.height()))
                run_start = None

        for i, s in enumerate(self.seq.segs):
            x0, x1 = self.tx(starts[i]), self.tx(starts[i] + s.dur)
            if x1 < self.HW or x0 > self.width():
                _close_run(last_r if last_r is not None else QRectF())
                run_grp = None
                continue
            r = QRectF(x0 + 0.5, self.V_Y + 0.5, max(1.0, x1 - x0 - 1.5), self.V_H - 1)
            if i in move_set:
                lifted.append((s, r, i))                    # its slot is drawn as a faint ghost
                p.setOpacity(0.3)
                self._draw_clip(p, fm, s, r, False, group_label_idx.get(s.grp) == i)
                p.setOpacity(1.0)
                _close_run(r)
                run_grp = None
                last_r = r
                continue
            self._draw_clip(p, fm, s, r, False, group_label_idx.get(s.grp) == i)
            if i in self.multi_sel:
                if run_start is not None and s.grp is not None and s.grp == run_grp:
                    pass                                     # same real group as the open run - extend it
                else:
                    _close_run(last_r if last_r is not None else QRectF())
                    run_start = r
                    run_grp = s.grp
            else:
                # Close at the LAST SELECTED clip, not the current unselected clip.
                # Using `r` here makes the selection border extend across the immediately
                # following clip, which looks like that clip belongs to the selected group.
                _close_run(last_r if last_r is not None else r)
                run_grp = None
            last_r = r
        if last_r is not None:
            _close_run(last_r)
        for run in sel_runs:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(run, 3, 3)
        if lifted and self.ghost is None:
            # Draw the dragged block anchored continuously to the mouse (via _grab/_mx) rather than to
            # its current model position, which can jump the instant a drag reorders the underlying
            # segs list. cum walks the block's own clips in their fixed relative order/spacing.
            bundle_t = self.xt(self._mx - self._grab)
            cum = 0.0
            drop_dy = self.V_H * self.DRAG_DROP_Y
            fr0 = fr1 = None
            for s, r, i in lifted:                           # the clips themselves hover under the mouse, together
                fx0 = self.tx(bundle_t + cum)
                fx1 = self.tx(bundle_t + cum + s.dur)
                fr = QRectF(fx0, r.y() + drop_dy, max(1.0, fx1 - fx0 - 1.5), r.height())
                fr0 = fr if fr0 is None else fr0
                fr1 = fr
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 90))
                p.drawRoundedRect(fr.translated(4, 9), 4, 4)
                p.setOpacity(0.93)
                self._draw_clip(p, fm, s, fr, False, group_label_idx.get(s.grp) == i)
                p.setOpacity(1.0)
                cum += s.dur
            # one merged selection border across the whole dragged bundle, not one per clip
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(fr0.x(), fr0.y(), fr1.right() - fr0.x(), fr0.height()), 3, 3)

    # [MAP] One clip: label band, filmstrip (a tile every TILE_W px, each sampled from THIS clip's own in..out range),
    # badges, file name, and a white outline when selected. Uses QPainter clip paths - restore() calls must stay
    # balanced with save().
    def _draw_clip(self, p, fm, s, r, sel, group_label=False):
        band, body = self._clip_colors(s)
        blank = getattr(s.media, "blank", False)         # [52.19] blank clip: ONE flat colour, no filmstrip/thumbnail area
        if blank:
            band = body = QColor(s.media.color)
        show_thumbs = self.V_H >= self.THUMB_MIN_H and not blank       # [51.10] too short -> plain clip, no filmstrip
        p.setPen(QPen(QColor("#101010"), 1))
        p.setBrush(body if show_thumbs else QColor(band))    # [51.12] solid colour when there are no thumbnails
        p.drawRoundedRect(r, 3, 3)
        path = QPainterPath()
        path.addRoundedRect(r, 3, 3)
        p.save()
        p.setClipPath(path, Qt.ClipOperation.IntersectClip)
        p.fillRect(QRectF(r.x(), r.y(), r.width(), self.BAND_H), QColor(band))
        # thumbnail filmstrip in the bottom half - a different frame every TILE_W px,
        # sampled across this clip's own in/out range so it reflects THIS clip's content
        thumb_top = r.y() + self.BAND_H
        thumb_h = max(1.0, self.V_H - self.BAND_H)
        n = max(1, min(20, round(r.width() / self.TILE_W)))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        tile_w = r.width() / n
        seg_dur = s.out_s - s.in_s
        for slot in range(n if show_thumbs else 0):      # no tiles when the row is too short
            t = s.in_s + (slot + 0.5) * seg_dur / n
            key = f"{s.media.path}|{round(t, 1)}"
            pm = self.thumb_pix.get(key)
            slot_x = r.x() + slot * tile_w
            if pm is not None and not pm.isNull():
                p.save()
                p.setClipRect(QRectF(slot_x, thumb_top, tile_w + 0.5, thumb_h), Qt.ClipOperation.IntersectClip)   # [52.26] was ReplaceClip: wiped the gutter/rounded clip
                pw, ph = pm.width(), pm.height()
                sc = thumb_h / ph                       # pixmap is cached at full row height; scale to the current one
                sw = pw * sc
                if sw <= tile_w:
                    p.drawPixmap(QRectF(slot_x + (tile_w - sw) / 2, thumb_top, sw, thumb_h), pm, QRectF(0, 0, pw, ph))
                else:
                    srcw = tile_w / sc
                    p.drawPixmap(QRectF(slot_x, thumb_top, tile_w, thumb_h), pm, QRectF((pw - srcw) / 2, 0, srcw, ph))
                p.restore()
            elif (self.mode is None and len(self.thumb_pending) < 48
                  and self._zoom_anim.state() != QVariantAnimation.State.Running):
                self.request_thumb(s.media, t, key)
        rx = r.right() - 4
        if r.width() > 60:
            rx = self._paint_badges(p, s, r, rx)
        if r.width() > 34:
            p.setPen(QColor("#10141c") if not blank or band.lightness() > 128 else QColor("#e8e8e8"))
            txt = fm.elidedText(s.media.name, Qt.TextElideMode.ElideRight, max(0, int(rx - r.x() - 10)))
            p.drawText(QPointF(r.x() + 6, r.y() + 12), txt)
        p.restore()
        if s.grp is not None:
            # [FEATURE] 20% tint of the group's own color over the whole clip, so grouped clips are
            # visually identifiable at a glance (in addition to the bottom color bar + label pill below).
            tint = QColor(self.seq.groups.get(s.grp, {}).get("color", "#888888"))
            tint.setAlpha(51)                     # 51/255 ~= 20% opacity
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tint)
            p.drawRoundedRect(r, 3, 3)
        if sel:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 3, 3)
        if s.grp is not None:
            gcolor = QColor(self.seq.groups.get(s.grp, {}).get("color", "#888888"))
            p.setPen(QPen(gcolor, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QLineF(r.x() + 2, r.bottom() - 1.5, r.right() - 2, r.bottom() - 1.5))
            name = self.seq.groups.get(s.grp, {}).get("name", "")
            if group_label and name:
                p.setPen(Qt.PenStyle.NoPen)
                f = p.font()
                f.setPointSizeF(7.5)
                f.setBold(True)
                p.setFont(f)
                fm2 = p.fontMetrics()
                w = min(r.width() - 4, fm2.horizontalAdvance(name) + 12)
                pill = QRectF(r.x() + 2, r.bottom() - self.BAND_H - 2, w, self.BAND_H)
                p.setBrush(gcolor)
                p.drawRoundedRect(pill, 2, 2)
                p.setPen(QColor("#101010"))
                p.drawText(pill.adjusted(5, 0, -2, 0),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm2.elidedText(name, Qt.TextElideMode.ElideRight, max(0, int(w - 7))))


