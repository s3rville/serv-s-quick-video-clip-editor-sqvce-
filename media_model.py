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

# ----------------------------------------------------------------------------- data model
# [MAP] One imported FILE (a Project-panel entry). Not a timeline clip - that is Seg.
# [INVARIANT] Media objects are dict keys (MainWindow.items/rows, MediaWorker._gen) and are compared with `is`
# (Sequence.export_parts). Never add __eq__/__hash__ and never copy a Media.
# [PITFALL] `path` is MUTABLE (rename_media, Save-Over refresh). Everything keyed by path must be kept in step:
# MainWindow.by_path, Timeline thumbnail caches (clear_thumbs_for), ReverseProxy keys (cancel_media),
# Timeline._hues (colour cache - goes stale after a rename; cosmetic only).
# Field notes:
#   keyframes  None = "not scanned yet / scan failed / audio-only" (NOT "has no keyframes"). Sorted list of
#              seconds once MediaWorker has finished. MainWindow._resume_load restarts the scan when it is None.
#   acodec     "<codec> <hz>" such as "aac 44100". The sample rate is included ON PURPOSE: _parts_match compares
#              it for equality, so different sample rates force a re-encoding join. "" = no audio stream.
#   w, h       the DISPLAYED size (already swapped for phone clips carrying a 90/270 rotation tag).
#   mark_in/out  default = whole file; only read when the media is inserted into the timeline.
class Media:
    """A file in the Project panel."""

    def __init__(self, path, dur, fps=30.0, w=0, h=0, vcodec="", acodec="", has_video=True, is_image=False):
        self.path = path
        self.name = os.path.basename(path)
        self.dur = dur
        self.fps = fps
        self.w, self.h = w, h
        self.vcodec, self.acodec = vcodec, acodec
        self.has_video = has_video
        self.is_image = is_image   # imported from a still picture (baked into a short silent video - see image_to_clip)
        self.blank = False         # [52.17] generated solid-colour clip (see probing.blank_clip); never in the Project panel
        self.color = "#000000"     # [52.17] its colour (only meaningful when blank)
        self.mark_in = 0.0
        self.mark_out = dur
        self.keyframes = None      # filled in by background worker
        self.thumb_path = None
        # [FEATURE] One dict per audio stream ffprobe/`ffmpeg -i` found: {"lang": "eng"/None, "name": title-tag
        # or None}. Filled in by probe_media; stays [] for video-only/audio-less media and is never touched by
        # anything downstream keyed on `acodec` (acodec's "codec + Hz" single-stream summary is unchanged).
        self.audio_streams = []

    # [MAP] Nearest keyframe to t that lies STRICTLY between lo and hi (exclusive), else None. Binary search over
    # the sorted list. Used by Sequence.split_at / insert_at and Timeline._trim (Snap mode).
    def nearest_kf(self, t, lo, hi):
        """Nearest keyframe to t that lies strictly between lo and hi (or None)."""
        kf = self.keyframes
        if not kf:
            return None
        i = bisect.bisect_left(kf, t)
        cands = [kf[j] for j in (i - 1, i) if 0 <= j < len(kf) and lo < kf[j] < hi]
        return min(cands, key=lambda k: abs(k - t)) if cands else None


# [MAP][COUPLING] One CLIP on the timeline = a slice [in_s, out_s] (SOURCE seconds) of a Media + per-clip options.
# Two durations exist - do not mix them up:
#     src_dur = out_s - in_s            length in the source file   (ffmpeg cut points, proxies, keys)
#     dur     = src_dur / speed         length on the TIMELINE      (all layout, playhead, undo geometry)
# Timeline offset -> source time is  in_s + offset * speed  (see Engine._target, Sequence.split_at).
#
# CHECKLIST when adding a per-clip field. Every place below enumerates the fields; miss one and undo, split,
# paste or export will silently drop your field.
#    1  Seg.__slots__ and Seg.__init__            append at the END: positional order is a contract
#    2  Seg.opts                                  only if it changes the exported bytes; append at the END of
#                                                 the tuple (_fx_args unpacks it; _concat_reencode reads p[4][3])
#    3  Sequence.snapshot()                       tuple order MUST equal __init__ order (_restore does Seg(*t))
#    4  Sequence.split_at(), Sequence.insert_at() four Seg(...) copies in total
#    5  MainWindow.copy_selected / paste_clip     9-tuple built and unpacked positionally
#    6  ClipOptionsDialog + edit_clip_options     UI, and ClipOptionsDialog.values()
#    7  ExportWorker._fx_args (+ est_secs)        the ffmpeg side
#    8  Engine._apply_fx                          live preview (if audible/visible)
#    9  Timeline._paint_badges                    visual indicator on the clip
#   10  Sequence.export_parts merge test          compares xf and opts, so a new opts field automatically
#                                                 stops clips that differ in it from being merged
# Seg objects use __slots__: assigning an attribute that is not in __slots__ raises AttributeError.
class Snap(list):
    """[52.11] A snapshot list that also carries `ext` = {name: state} from Sequence.ext providers (mods).
    Plain-list behaviour/equality is unchanged (ext is ignored by ==), so every existing snapshot user keeps working."""
    ext = None


class Seg:
    """A clip on the timeline: a slice [in_s, out_s] of a Media (source seconds).
    `dur` is the TIMELINE duration (source span / speed); `src_dur` is the source span."""
    __slots__ = ("media", "in_s", "out_s", "xf", "mute", "speed", "mirror", "rot", "rev", "grp",
                 "atracks", "vol_db", "track_type")

    # [STALE][KNOWN ISSUE F14] The trailing comments below say mirror and rot are "export only". Since v3 they are
    #   ALSO shown live
    # in the preview (MainWindow.refresh_stage -> VideoStage.set_state -> VideoView.set_layout). Left unchanged so
    # this file stays code-identical.
    def __init__(self, media, in_s, out_s, xf=None, mute=False, speed=1.0, mirror=False, rot=0.0, rev=False, grp=None,
                 atracks=None, vol_db=0.0, track_type="stereo"):
        self.media, self.in_s, self.out_s = media, in_s, out_s
        self.xf = xf        # crop/resize: output canvas (w,h) + where the scaled picture sits in it
        self.mute = mute    # per-clip: silence this clip's audio
        self.speed = speed  # per-clip playback speed (1.0 = normal)
        self.mirror = mirror  # per-clip horizontal flip (export only)
        self.rot = rot        # per-clip rotation, degrees clockwise (export only)
        self.rev = rev        # per-clip reverse playback (export re-encodes)
        self.grp = grp        # visual-only clip group id (see Sequence.groups); never affects export
        # [FEATURE] Clip Options "Audio" section - one bool per media.audio_streams entry (or a single implicit
        # entry when none were detected), which of the source's audio tracks are included. Defaults to every
        # track on, i.e. "use the source as-is". atracks=None resolves the default from the media it's attached to.
        n_tracks = max(1, len(media.audio_streams)) if media is not None else 1
        self.atracks = tuple(atracks) if atracks is not None else tuple(True for _ in range(n_tracks))
        self.vol_db = vol_db          # per-clip gain in dB, +0 = unity/center (Clip Options "Volume" slider)
        self.track_type = track_type  # "stereo" (source layout, unchanged) or "mono" (downmixed) - "Track Type"

    @property
    def src_dur(self):
        return self.out_s - self.in_s

    @property
    def dur(self):
        return (self.out_s - self.in_s) / (self.speed or 1.0)

    # [INVARIANT] THE lossless / re-encode switch. None => the clip can be stream-copied. Non-None => it goes through
    # ExportWorker._fx_args (re-encode). `mute` only counts when the media really has audio (muting a silent file
    # must not force a re-encode). `rot` is normalised to [0,360); values within 1e-6 of 0/360 mean "no rotation".
    # Tuple order (mute, speed, mirror, rot, rev, atracks, vol_db, track_type) is relied on by _fx_args (unpack)
    # and _concat_reencode (p[4][3] == rot - unaffected, still index 3). Append new entries at the END.
    @property
    def opts(self):
        """(mute, speed, mirror, rot, rev, atracks, vol_db, track_type) if this clip needs re-encoding for its
        options, else None."""
        m = bool(self.mute and self.media.acodec.strip())
        r = self.rot % 360.0
        has_a = bool(self.media.acodec.strip())
        # [FEATURE] Track selection / volume / mono only matter (and only force a re-encode) when the source
        # actually has audio AND at least one of them differs from "every track, unity gain, source layout".
        trk_nondefault = has_a and (any(not v for v in self.atracks) or abs(self.vol_db) > 1e-6
                                     or self.track_type == "mono")
        if (m or abs(self.speed - 1.0) > 1e-6 or self.mirror or self.rev
                or (r > 1e-6 and 360.0 - r > 1e-6) or trk_nondefault):
            return (m, self.speed, bool(self.mirror), r, bool(self.rev), self.atracks,
                    round(self.vol_db, 2), self.track_type)
        return None


# [MAP] The timeline model: an ordered GAP-FREE ("magnetic") list of Seg. Start times are DERIVED (starts()
# accumulates dur) and never stored, so any change of a clip's length/order/speed ripples automatically.
# Signals
#   edited  a COMMITTED change (an undo entry was pushed). MainWindow.on_seq_edited reacts: request reverse
#           proxies, refresh crop stage, re-seek the Engine, update labels/estimates.
#   live    a drag is in progress (Timeline mutates segs directly) -> repaint only. The Engine is NOT told
#           until the mouse is released, so the preview does not follow a trim/move drag.
# Flags
#   snap     cuts/trims land on keyframes (needs media.keyframes) -> preview == lossless export.
#   precise  cuts may land on any frame; export re-encodes (see ExportWorker, finding F1).
#   MainWindow keeps exactly one of them on through an exclusive QButtonGroup.
# [INVARIANT] Programmatic edits go through edit(fn). Exceptions: (a) Timeline drags mutate segs in place and
# call commit(before)+edited.emit() themselves on mouse release; (b) wholesale resets in remove_medias,
# clear_project and _reload_primary_after_saveover, which clear both undo stacks on purpose.
class Sequence(QObject):
    """Magnetic (gap-free) sequence with undo/redo."""
    edited = Signal()   # committed change -> preview must refresh
    live = Signal()     # in-progress drag -> repaint only
    blocked = Signal(str)   # [52.25] an edit() was refused because its layer is locked (arg = layer key)

    def __init__(self):
        super().__init__()
        self.segs = []
        self.undo_stack, self.redo_stack = [], []
        self.snap = True
        self.precise = False   # when True, cuts may land off-keyframe; export re-encodes just that sliver
        self.groups = {}       # grp_id -> {"color": "#rrggbb", "name": ""} - visual only, not undo-tracked
        self._next_grp = 1
        # [PERF] starts()/total() are O(n) over every clip and locate() was an O(n) linear scan; all three are
        # called on every scrub sample and every seek, so on a big project they add up fast. `_starts_cache` is
        # the memoized starts() list (or None when stale); `_total_cache` its total() companion. Cache
        # correctness relies on the same invariant the rest of the class already documents: every mutation of
        # `segs` or of a duration-affecting Seg field (in_s/out_s/speed) - whether via edit(fn) or a Timeline
        # drag mutating in place - is always followed, in the SAME call, by an `edited` or `live` emit (edit()
        # itself guarantees this; Timeline._trim/_move do it by hand). So invalidating on both signals covers
        # every path with nothing missed - see _invalidate_geometry.
        # [52.11] History providers (mods): name -> object with ext_snapshot() -> comparable plain data and
        # ext_restore(data). Their state rides inside every snapshot, so Ctrl+Z / Ctrl+Shift+Z cover it.
        # A mod records its own change with seq.commit(before_snapshot); seq.edited.emit().
        self.ext = {}
        self.locked = set()    # [52.25] layer keys locked in Advanced mode ("video", "audio", lane keys). Not undo-tracked, never saved.
        self._starts_cache = None
        self._total_cache = None
        self.edited.connect(self._invalidate_geometry)
        self.live.connect(self._invalidate_geometry)

    def _invalidate_geometry(self):
        self._starts_cache = None
        self._total_cache = None

    # [MAP] Visual-only clip grouping (feature: multi-select + right-click "Group"). Never affects export.
    def new_group(self):
        gid = self._next_grp
        self._next_grp += 1
        import random
        hue = random.randint(0, 359)
        # [FEATURE] Auto-name new groups "Group1", "Group2", ... (still renameable via F2/right-click).
        self.groups[gid] = {"color": QColor.fromHsv(hue, 140, 210).name(), "name": f"Group{gid}"}
        return gid

    # [COUPLING] Tuple order == Seg.__init__ order (see the Seg checklist). Snapshots hold Media by reference and
    # plain values for everything else, so they are cheap and immune to later in-place mutation of a Seg.
    def snapshot(self):
        snap = Snap((s.media, s.in_s, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev, s.grp,
                     s.atracks, s.vol_db, s.track_type) for s in self.segs)
        snap.ext = {}
        for k, p in self.ext.items():
            try:
                snap.ext[k] = p.ext_snapshot()
            except Exception:
                pass
        return snap

    # [PITFALL] Builds NEW Seg objects. Never keep a Seg reference across undo/redo (it would point at an orphan).
    # Timeline.sel is an index, which is why selection survives (clamped in Timeline._on_seq).
    def _restore(self, snap):
        self.segs = [Seg(*x) for x in snap]
        self._invalidate_geometry()
        ext = getattr(snap, "ext", None)
        if ext:
            for k, p in self.ext.items():
                if k in ext:
                    try:
                        p.ext_restore(ext[k])
                    except Exception:
                        pass

    # [MAP] Push `before` on the undo stack (depth capped at 100) and clear redo. Timeline drags call this directly.
    def commit(self, before):
        self.undo_stack.append(before)
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    # [INVARIANT] fn() returning exactly False means "nothing happened, do not commit". ANY other return value
    # (True, 0, 0.0, None, a float) COMMITS. The identity test `r is not False` is deliberate: split_at returns the
    # split time (a float) and edit() hands it back to MainWindow.split_at for the status bar. Do not "simplify" to
    # `if r:` - a legitimate 0.0 would then be treated as failure.
    # [52.25] `layer` = which row the edit touches. A locked layer refuses it (returns False = "nothing happened", the same
    # value callers already handle) and emits `blocked`. Pass layer=None for edits that only touch lane items (text/audio).
    def edit(self, fn, layer="video"):
        if layer is not None and layer in self.locked:
            self.blocked.emit(str(layer))
            return False
        before = self.snapshot()
        r = fn()
        if r is not False:
            self.commit(before)
            self.edited.emit()
        return r

    # [MAP] undo/redo emit `edited` (full preview refresh), never `live`. Keyboard: Ctrl+Z / Ctrl+Shift+Z
    #   (MainWindow.build_actions).
    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self._restore(self.undo_stack.pop())
            self.edited.emit()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self._restore(self.redo_stack.pop())
            self.edited.emit()

    def total(self):
        if self._total_cache is None:
            self.starts()
        return self._total_cache

    # [PERF] Memoized - do NOT mutate the returned list (callers use `starts() + [...]`, which copies).
    def starts(self):
        c = self._starts_cache
        if c is None:
            c, acc = [], 0.0
            for s in self.segs:
                c.append(acc)
                acc += s.dur
            self._starts_cache, self._total_cache = c, acc
        return c

    # [MAP] timeline time -> (segment index, offset inside that segment in TIMELINE seconds). Past the end it
    # returns (last index, its dur). Empty timeline returns (None, 0.0) - callers MUST handle idx None.
    # [PERF] O(log n) bisect over the cached starts() instead of a linear scan; same results as the old scan
    # (t<0 -> (0, 0.0); t>=total -> (last, last.dur)).
    def locate(self, t):
        st = self.starts()
        n = len(st)
        if n == 0:
            return None, 0.0
        i = max(0, bisect.bisect_right(st, t) - 1)
        d = self.segs[i].dur
        if not t < st[i] + d:                   # same comparison the old linear scan used (float-exact)
            if i < n - 1:                       # float-edge safety net: behave like the old scan and move on
                i += 1
                d = self.segs[i].dur
            if i == n - 1 and not t < st[i] + d:
                return i, d
        return i, max(0.0, t - st[i])

    # [PITFALL] The FIRST clip's fps drives timecode display, frame stepping (MainWindow.step) and the ruler for
    # the whole timeline, even when later clips have different frame rates.
    def fps(self):
        return self.segs[0].media.fps if self.segs else 30.0

    # --- edits (call through edit()) ---
    # [MAP] Split the clip under timeline time t. Returns the SOURCE time of the cut, or False when impossible
    # (within 0.02 s of a clip edge, or Snap is on and no keyframe lies inside the clip).
    # Snapping happens only when snap is on AND precise is off AND media.keyframes is already loaded.
    # [KNOWN ISSUE F11] While keyframes is still None (background scan running) the cut is made at the exact time
    # without snapping, so in Snap mode a cut made during the scan may not be reproducible by the lossless export.
    # Timeline._trim blocks in that situation; split_at does not.
    # [COUPLING] The two Seg(...) copies list every field - keep in step with Seg.
    def split_at(self, t):
        idx, off = self.locate(t)
        if idx is None:
            return False
        s = self.segs[idx]
        lo, hi = s.in_s + 0.02, s.out_s - 0.02
        src = s.in_s + off * s.speed
        if not (lo < src < hi):
            return False
        if self.snap and not self.precise and s.media.keyframes:
            k = s.media.nearest_kf(src, lo, hi)
            if k is None:
                return False
            src = k
        self.segs[idx:idx + 1] = [Seg(s.media, s.in_s, src, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev, s.grp,
                                       s.atracks, s.vol_db, s.track_type),
                                   Seg(s.media, src, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev, s.grp,
                                       s.atracks, s.vol_db, s.track_type)]
        self._invalidate_geometry()
        return src

    # [MAP] Ripple delete: neighbours close the gap automatically (there is no gap model).
    def delete(self, idx):
        if 0 <= idx < len(self.segs):
            del self.segs[idx]
            self._invalidate_geometry()
            return True
        return False

    # [MAP] Ripple delete several indices at once (e.g. every clip of a group) as one edit. Order doesn't matter -
    # sorted descending internally so earlier indices stay valid while later ones are removed.
    def delete_many(self, idxs):
        idxs = sorted({i for i in idxs if 0 <= i < len(self.segs)}, reverse=True)
        if not idxs:
            return False
        for i in idxs:
            del self.segs[i]
        self._invalidate_geometry()
        return True

    # [MAP][FIX B1] Index just past the last clip sharing segs[idx]'s group id. Groups are always stored as a
    # contiguous run (see Timeline._move), so a simple forward scan is enough.
    def _group_end(self, idx):
        gid = self.segs[idx].grp
        j = idx
        while j < len(self.segs) and self.segs[j].grp == gid:
            j += 1
        return j

    # [MAP] Insert `seg` at timeline time t (drag-drop, paste, double-click in Project). tol = how close to a clip
    # boundary still counts as "at the boundary" (0.1 s normally; 8 px worth of time for drops, see on_files_dropped).
    # At a boundary/end: plain list insert. Inside a clip: the clip is split (keyframe-snapped in Snap mode; if it
    # has no keyframe inside, the seg goes to the nearer edge instead). Returns the index of the inserted seg.
    # [COUPLING] The split branch copies every Seg field - keep in step with Seg.
    def insert_at(self, t, seg, tol=0.1):
        if not self.segs:
            self.segs.append(seg)
            self._invalidate_geometry()
            return 0
        if t >= self.total() - tol:
            self.segs.append(seg)
            self._invalidate_geometry()
            return len(self.segs) - 1
        idx, off = self.locate(max(0.0, t))
        s = self.segs[idx]
        # [FIX B1] Never let a drop land between two clips of the same group, or split a grouped clip with the
        # new media wedged into the middle of it - push it past the end of that whole group instead.
        if off <= tol:
            if idx > 0 and s.grp is not None and self.segs[idx - 1].grp == s.grp:
                pos = self._group_end(idx)
            else:
                pos = idx
            self.segs.insert(pos, seg)
            self._invalidate_geometry()
            return pos
        if s.dur - off <= tol:
            if idx + 1 < len(self.segs) and s.grp is not None and self.segs[idx + 1].grp == s.grp:
                pos = self._group_end(idx)
            else:
                pos = idx + 1
            self.segs.insert(pos, seg)
            self._invalidate_geometry()
            return pos
        # [FIX] Dropping/inserting inside a clip must never CUT that clip - it just pushes the whole
        # thing to whichever of its edges is nearer, same as the "no keyframe inside" fallback used to
        # do. (Previously this branch split the clip in two around the insert point.)
        pos = self._group_end(idx) if s.grp is not None else (idx if off < s.dur / 2 else idx + 1)
        self.segs.insert(pos, seg)
        self._invalidate_geometry()
        return pos

    # [INVARIANT] The bridge to ExportWorker. Returns mutable LISTS [media, in_s, out_s, xf, opts] and merges a clip
    # into the previous part only when ALL hold: same Media object, contiguous within 1 ms, equal xf, equal opts,
    # and the new clip is not reversed. Merging matters: "split then export unchanged" becomes ONE lossless cut
    # instead of a join. Reversed clips must never merge (playback order would flip inside the merged span).
    # Consumers index positionally: p[0] media, p[1] in, p[2] out, p[3] xf, p[4] opts
    # (ExportWorker._build_part_files / _parts_match / _concat_reencode, MainWindow._checked_parts). Append at END.
    def export_parts(self):
        """Merge neighbours that are contiguous slices of the same file."""
        parts = []
        for s in self.segs:
            if (parts and parts[-1][0] is s.media and abs(parts[-1][2] - s.in_s) < 0.001
                    and parts[-1][3] == s.xf and parts[-1][4] == s.opts and not s.rev):   # reversed clips must never merge (order would flip)
                parts[-1][2] = s.out_s
            else:
                parts.append([s.media, s.in_s, s.out_s, s.xf, s.opts])
        return parts


