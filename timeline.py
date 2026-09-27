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
    deleteRequested = Signal()
    copyRequested = Signal()
    pasteRequested = Signal()
    filesDropped = Signal(list, float)
    zoomChanged = Signal(float)
    thumbReady = Signal(str, str)
    trimBlocked = Signal(str)
    clipOptionsRequested = Signal(int)
    loopToggled = Signal(bool)
    _hues = None

    HW, RULER_H = 6, 22
    V_Y, V_H = RULER_H + 3, 92
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
        self.bar = None
        self.loop = False
        self.multi_sel = set()   # extra selected clip indices (Ctrl+click), alongside self.sel
        self._move_set = []      # indices being dragged together (single clip, multi-sel, or a named group)
        self.thumb_pix = {}         # key -> pre-scaled QPixmap
        self.thumb_pending = set()
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setMinimumHeight(self.V_Y + self.V_H + 8)
        self.setFont(QFont("Segoe UI", 8))
        seq.edited.connect(self._on_seq)
        seq.live.connect(self._on_seq)
        self._zoom_anchor_x, self._zoom_anchor_t = None, 0.0
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(180)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(self._apply_zoom)

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
        edges = self.seq.starts() + [self.seq.total()]
        if not edges:
            return t
        SNAP_S = 8.0 / self.pps
        best = min(edges, key=lambda e: abs(e - t))
        return best if abs(best - t) <= SNAP_S else t

    # --- state
    def _on_seq(self):
        if self.sel >= len(self.seq.segs):
            self.sel = -1
        self.multi_sel = {i for i in self.multi_sel if i < len(self.seq.segs)}
        self._update_bar()
        self.update()

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
                self.scroll_x = max(0.0, t * self.pps - 60)
                self._update_bar()
        self.update()

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
        self.update()

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
        self.update()

    def zoom_fit(self, animate=True):
        total = max(self.seq.total(), 1.0)
        view = max(50, self.width() - self.HW - 80)
        self.scroll_x = 0.0
        self.set_zoom(view / total, anchor_x=self.HW, animate=animate)

    def resizeEvent(self, e):
        self._update_bar()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.pps * (1.15 if dy > 0 else 1 / 1.15), anchor_x=self.tx(self.playhead))
        else:
            self.scroll_x = max(0.0, self.scroll_x - dy)
            self._update_bar()
            self.update()

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

    def group_selected(self):
        idxs = sorted(self.multi_sel)
        if len(idxs) < 2:
            return
        # [FIX] Clips must be CONTIGUOUS (no un-selected clip in between) to form a group - a group is a single
        # unbroken block on the timeline (see [FIX B1]/_group_end, which assume this), so selecting e.g. clips
        # 0 and 2 while skipping 1 must not be allowed to group.
        if idxs != list(range(idxs[0], idxs[-1] + 1)):
            return
        if not self._group_selectable(idxs):
            return
        existing = {self.seq.segs[i].grp for i in idxs if self.seq.segs[i].grp is not None}
        gid = existing.pop() if len(existing) == 1 else self.seq.new_group()
        for i in idxs:
            self.seq.segs[i].grp = gid
        self.update()

    def ungroup_selected(self):
        if not (0 <= self.sel < len(self.seq.segs)):
            return
        gid = self.seq.segs[self.sel].grp
        if gid is None:
            return
        for s in self.seq.segs:
            if s.grp == gid:
                s.grp = None
        self.seq.groups.pop(gid, None)
        self.update()

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
            self.update()

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
            self.update()

    # --- mouse
    # [MAP] Left button: ruler -> scrub; Razor tool -> emit splitRequested(time under mouse) if over a clip; empty
    # area -> deselect; otherwise select the clip and enter trim_in / trim_out / move. Right button -> context menu
    # (add edit, copy, paste, ripple delete; same actions as the shortcuts). Focus is taken on click
    # (ClickFocus) so the Delete key is routed here - see MainWindow.handle_delete_key.
    # Starting a trim_in in Snap mode before keyframes are loaded emits trimBlocked (status-bar hint only; _trim
    # then ignores the drag).
    def mousePressEvent(self, e):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = e.position()
        x, y = pos.x(), pos.y()
        if e.button() == Qt.MouseButton.RightButton:
            idx, _ = self.hit(pos)
            if idx is not None:
                if idx not in self.multi_sel:
                    self.multi_sel = {idx}
                self.sel = idx
                self.update()
            menu = QMenu(self)
            a_split = menu.addAction("Add Edit at Playhead\tShift+C")
            a_copy = menu.addAction("Copy Clip\tCtrl+C")
            a_copy.setEnabled(idx is not None)
            a_paste = menu.addAction("Paste Clip\tCtrl+V")
            a_del = menu.addAction("Ripple Delete\tDel")
            a_del.setEnabled(idx is not None)
            menu.addSeparator()
            cur_grp = self.seq.segs[idx].grp if idx is not None else None
            a_group = a_ungroup = a_rename = a_color = None
            sel_idxs = sorted(self.multi_sel)
            # [FIX] Only offer grouping when the selection is a single unbroken run of clips, AND does not
            # partially overlap an existing group - grouping a partial overlap would split that group and
            # leave its remaining clips interleaved with the new group (see _group_selectable).
            contiguous = bool(sel_idxs) and sel_idxs == list(range(sel_idxs[0], sel_idxs[-1] + 1))
            if idx is not None and len(sel_idxs) >= 2 and contiguous and self._group_selectable(sel_idxs):
                grp_ids = {self.seq.segs[i].grp for i in sel_idxs if self.seq.segs[i].grp is not None}
                label = "Add to Group" if len(grp_ids) == 1 else "Group Selected Clips"
                a_group = menu.addAction(label)
            if cur_grp is not None:
                a_rename = menu.addAction("Rename Group...\tF2")
                a_color = menu.addAction("Change Group Color...")
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
            self.sel = -1
            self.multi_sel.clear()
            self.update()
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
            self.update()
            return
        seg = self.seq.segs[idx]
        # Plain click always replaces the previous selection. Grouped clips expand to their group;
        # ungrouped clips select only themselves.
        if seg.grp is not None:
            self.multi_sel = {i for i, s in enumerate(self.seq.segs) if s.grp == seg.grp}
        else:
            self.multi_sel = {idx}
        self.sel = idx
        if self.tool in ("crop", "resize"):
            # [FIX] Crop/Resize edits a single frame in place - starting a trim or move here would
            # change the very clip being edited out from under the tool. Selection still works
            # (so the right-click menu / group actions keep functioning), dragging doesn't.
            self.update()
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
            bundle_dur = sum(self.seq.segs[k].dur for k in self._move_set)
            self._grab = bundle_dur * self.pps / 2
            # [FIX] Don't lift/follow the mouse yet - see mouseMoveEvent's MOVE_ARM_PX check. Lifting
            # (and switching the cursor) right on press made a plain or double click visibly "jerk" the
            # clip for one frame even though nothing was actually dragged.
            self._mx = None
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        x = pos.x()
        self.hover_x = x if self.tool == "razor" else None
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
        else:
            if self.tool == "razor":
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                idx, edge = self.hit(pos)
                self.setCursor(Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.ArrowCursor)
        self.update()

    # [INVARIANT] The single place where a drag becomes an undo step: compares the live snapshot with _snap0 and
    # commits only if something really changed (a click without movement creates no undo entry).
    def mouseReleaseEvent(self, e):
        if self.mode in ("trim_in", "trim_out", "move"):
            if self.seq.snapshot() != self._snap0:
                self.seq.commit(self._snap0)
                self.seq.edited.emit()
        if self.mode == "move":
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.mode = None
        self._mx = None
        self._move_set = []
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.tool == "razor":
            return
        idx, _ = self.hit(e.position())
        if idx is not None:
            seg = self.seq.segs[idx]
            self.sel = idx
            self.multi_sel = ({i for i, s in enumerate(self.seq.segs) if s.grp == seg.grp}
                              if seg.grp is not None else {idx})
            self.mode = None
            self.update()
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
        self.seq.live.emit()

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
    def request_thumb(self, media, t, key):
        if key in self.thumb_pending or key in self.thumb_pix or not FFMPEG or not media.has_video:
            return
        self.thumb_pending.add(key)
        threading.Thread(target=self._gen_thumb, args=(media.path, t, key), daemon=True).start()

    def _gen_thumb(self, path, t, key):
        try:
            fn = gen_thumb_file(path, t, key, "quickcut_clipthumbs", width=140)
            if fn:
                self.thumbReady.emit(key, fn)
        finally:
            self.thumb_pending.discard(key)

    def _on_thumb_ready(self, key, filepath):
        h = max(1, self.V_H - self.BAND_H)
        pm = QPixmap(filepath)
        if not pm.isNull():
            self.thumb_pix[key] = pm.scaledToHeight(h, Qt.TransformationMode.SmoothTransformation)
        self.update()

    # [MAP] Drops in-memory thumbnails (and pending markers) for one media path. Call whenever a file's contents or
    # path change (remove, rename, Save-Over, clear). It does NOT clear the on-disk cache [F5].
    def clear_thumbs_for(self, path):
        for k in [k for k in self.thumb_pix if k.startswith(path + "|")]:
            del self.thumb_pix[k]
        for k in [k for k in self.thumb_pending if k.startswith(path + "|")]:
            self.thumb_pending.discard(k)

    # --- painting
    # [MAP] Paint order: background -> ruler -> clips (clipped to the area right of the gutter) -> empty-state hint
    # -> razor hover line -> file-drop marker -> playhead. Everything is drawn by hand each repaint; keep the
    # per-clip work cheap (called on every playhead tick during playback).
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H, hw = self.width(), self.height(), self.HW
        p.fillRect(0, 0, W, H, QColor("#1e1e1e"))
        p.fillRect(hw, self.V_Y, W - hw, self.V_H, QColor("#181818"))
        p.save()
        p.setClipRect(hw, 0, W - hw, H)
        self._paint_ruler(p, W)
        self._paint_clips(p)
        if not self.seq.segs:
            p.setPen(QColor("#6c6c6c"))
            p.drawText(QRectF(hw, self.V_Y, W - hw, self.V_H),
                       Qt.AlignmentFlag.AlignCenter,
                       "Drag clips here from the Project panel")
        if self.hover_x is not None and self.hover_x > hw:
            p.setPen(QPen(QColor("#ff5050"), 1))
            p.drawLine(QLineF(self.hover_x, self.RULER_H, self.hover_x, self.V_Y + self.V_H + 6))
        if self.drop_x is not None:
            p.setPen(QPen(QColor("#ffb020"), 2, Qt.PenStyle.DashLine))
            p.drawLine(QLineF(self.drop_x, self.RULER_H, self.drop_x, H))
        x = self.tx(self.playhead)
        p.setPen(QPen(QColor("#2d8ceb"), 1.5))
        p.drawLine(QLineF(x, self.RULER_H, x, H))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2d8ceb"))
        p.drawPolygon(QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x + 6, 12),
                                 QPointF(x, 19), QPointF(x - 6, 12)]))
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
        if lifted:
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
        p.setPen(QPen(QColor("#101010"), 1))
        p.setBrush(body)
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
        tile_w = r.width() / n
        seg_dur = s.out_s - s.in_s
        for slot in range(n):
            t = s.in_s + (slot + 0.5) * seg_dur / n
            key = f"{s.media.path}|{round(t, 1)}"
            pm = self.thumb_pix.get(key)
            slot_x = r.x() + slot * tile_w
            if pm is not None and not pm.isNull():
                p.save()
                p.setClipRect(QRectF(slot_x, thumb_top, tile_w + 0.5, thumb_h))
                pw = pm.width()
                if pw <= tile_w:
                    p.drawPixmap(QPointF(slot_x + (tile_w - pw) / 2, thumb_top), pm)
                else:
                    src = QRectF((pw - tile_w) / 2, 0, tile_w, thumb_h)
                    p.drawPixmap(QRectF(slot_x, thumb_top, tile_w, thumb_h), pm, src)
                p.restore()
            elif self.mode is None and len(self.thumb_pending) < 48:
                self.request_thumb(s.media, t, key)
        rx = r.right() - 4
        if r.width() > 60:
            rx = self._paint_badges(p, s, r, rx)
        if r.width() > 34:
            p.setPen(QColor("#10141c"))
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


