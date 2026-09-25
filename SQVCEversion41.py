"""
QuickCut - a LOSSLESS video cutter / merger with a single timeline-driven preview.

Nothing is ever re-encoded: exports use ffmpeg stream copy (-c copy).

Setup (Windows):
    pip install PySide6
    put ffmpeg.exe AND ffprobe.exe next to this file (or in PATH)
Run:
    python QuickCut.py            (you can also pass video files as arguments)
"""
# =====================================================================================================
# MAINTAINER NOTES - added by a full code review.  COMMENTS ONLY: the executable code below is
# unchanged (proved by comparing ast.dump() of this file with the original - see DEV_GUIDE section 1).
#
# Companion documents
#   QuickCut_DEV_GUIDE.md       architecture, invariants, data flows, findings register, change recipes
#   QuickCut_dev_notes.md       the original hand-off written by the previous AI developers
#   test_quickcut_headless.py   regression harness (real ffmpeg, no display needed)
#
# TAG LEGEND - grep for these before planning a change
#   [MAP]            orientation: what a block is and who calls it
#   [INVARIANT]      a rule that OTHER code relies on; breaking it causes bugs somewhere else
#   [DO NOT BREAK]   each item fixes a bug that was reproduced by a user (from the original dev notes)
#   [COUPLING]       code that must be edited together with code elsewhere (checklist inside)
#   [PITFALL]        an easy mistake that looks harmless
#   [KNOWN ISSUE Fn] defect found in review. Details + suggested fix: DEV_GUIDE "Findings register".
#                    Deliberately NOT fixed here so this file stays behaviour-identical to the original.
#   [STALE]          a comment/docstring that no longer matches the code (left untouched on purpose)
#
# FILE MAP (top to bottom)
#    1. tools & helpers ....... find_tool, run_tool, quiet_ffmpeg_log, fmt_tc, norm_xf, xf_filter
#    2. data model ............ Media, Seg, Sequence   (magnetic timeline + undo/redo)
#    3. probing/background .... probe_media, gen_thumb_file, MediaWorker (thumbnail + keyframe scan)
#    4. export ................ ExportWorker           (cut / fx / concat / GIF / transcode)
#    5. icons + dialogs ....... icon(), ClipOptionsDialog, GifPresetDialog, HbPresetDialog, ExportDoneDialog
#    6. small widgets ......... TitleBar, Panel, ProjectRow, ProjectList
#    7. preview stack ......... CropOverlay, VideoView, VideoStage, WarningBar
#    8. timeline .............. Timeline               (painting + all mouse editing)
#    9. playback .............. ReverseProxy, Engine   (the ONLY code that touches QMediaPlayer)
#   10. app shell ............. QSS, MainWindow (wires everything together), main()
#
# THE 8 RULES THAT KEEP THIS PROGRAM FROM BREAKING
#   1. Do not edit Engine casually: each of its lines fixes a reproduced bug (see [DO NOT BREAK]).
#   2. Every timeline change goes through Sequence.edit(fn) - undo/redo and preview refresh depend on it.
#   3. Seg has a 9-field tuple contract that is copied in ~8 places - use the [COUPLING] checklist at Seg.
#   4. Before touching a media file on disk (rename / Save-Over / remove / clear) call BOTH
#      worker.cancel(...) and proxy.cancel_media(...) - Windows keeps files locked otherwise.
#   5. ffmpeg is always launched with an argv LIST and creationflags=NOWIN, via ExportWorker._ffmpeg
#      (export) or run_tool (probe/thumbnails). Never build shell strings.
#   6. No per-frame Python hooks on the video sink, no browser storage, no blocking work on the GUI thread.
#   7. Preview must equal export: a new visual/audio effect needs a live-preview path AND an ffmpeg path,
#      and must make Seg.opts non-None so the clip is re-encoded instead of stream-copied.
#   8. After every edit run:  python -m py_compile QuickCut.py   and   python test_quickcut_headless.py
#
# [STALE][KNOWN ISSUE F14] The module docstring at the very top ("Nothing is ever re-encoded ... -c copy") is
# outdated: since v2 crop/resize, speed, mute, mirror, rotate, reverse, GIF and Transcode all re-encode. Only
# plain cut/join clips are stream-copied. The "Setup" lines in it are still correct.
# =====================================================================================================
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

# [MAP] Global constants.
#   NOWIN         Windows CREATE_NO_WINDOW flag. Pass creationflags=NOWIN to EVERY subprocess that starts
#                 ffmpeg / ffprobe / gifsicle, otherwise a console window flashes on Windows (no-op elsewhere).
#   MIN_DUR       shortest clip (in SOURCE seconds) a trim drag may produce (used by Timeline._trim).
#   VIDEO_FILTER  file-dialog filter for Import. It also lists audio types (mp3/m4a/wav): audio-only files
#                 are legal Media with has_video=False (no thumbnail, no keyframes, no crop/rotate/mirror).
#                 Any new code that reads media.w / media.h / has_video must tolerate that case.
NOWIN = 0x08000000 if os.name == "nt" else 0   # hide console windows on Windows
MIN_DUR = 0.05
VIDEO_FILTER = "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.ts *.m4v *.flv *.mts *.m2ts *.mp3 *.m4a *.wav);;All files (*.*)"


# ----------------------------------------------------------------------------- tools
# [MAP] Locate an external tool: first next to this script (or next to the frozen .exe), then on PATH.
# Returns None when not found. FFMPEG / FFPROBE are resolved ONCE at import time (below); gifsicle is
# looked up on demand (_make_gif, est_secs, export_gif) so dropping gifsicle.exe next to the app works
# without a restart.
# [PITFALL] FFMPEG=None does not stop the app: MainWindow shows a warning and features silently degrade.
# FFPROBE=None disables Snap/Precise and the keyframe scan. Guard any new ffmpeg feature with `if FFMPEG`.
def find_tool(name):
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(base, name + ".exe"), os.path.join(base, name)):
        if os.path.isfile(cand):
            return cand
    return shutil.which(name)


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


# [MAP] Small blocking subprocess helper (text mode, utf-8, undecodable bytes replaced, no console window).
# [PITFALL] It blocks its caller. It is used by probe_media (on the GUI thread!) and gen_thumb_file (on
# worker threads). Do not use it for anything long-running - use ExportWorker or a thread instead.
def run_tool(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NOWIN, timeout=timeout)


# [MAP] (used_MB, total_MB) of physical RAM or None. Order: psutil (optional) -> Windows GlobalMemoryStatusEx
# via ctypes -> /proc/meminfo (Linux). macOS without psutil returns None (=> no RAM warning). Never raises.
# Consumer: MainWindow.check_system (polled every 3 s -> WarningBar "ram" pill).
def system_memory():
    """(used_MB, total_MB) of physical RAM, or None if it can't be read."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return int((vm.total - vm.available) / 2**20), int(vm.total / 2**20)
    except Exception:
        pass
    try:
        if os.name == "nt":
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MS()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int((st.ullTotalPhys - st.ullAvailPhys) / 2**20), int(st.ullTotalPhys / 2**20)
        elif os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k] = int(v.split()[0])          # kB
            tot, av = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return int((tot - av) / 1024), int(tot / 1024)
    except Exception:
        pass
    return None


# [DO NOT BREAK #7] Qt's FFmpeg backend prints harmless decoder warnings on every seek ("Could not update
# timestamps for skipped samples"...). This lowers the log level of the avutil library that ships inside
# PySide6 to AV_LOG_ERROR (16) - the same library instance Qt uses. Best effort, never raises, returns bool.
# Called twice from MainWindow.__init__ (now, and again after 2 s once Qt has surely loaded its backend).
# Set env QUICKCUT_VERBOSE_FFMPEG=1 to see the full log when debugging playback problems.
def quiet_ffmpeg_log():
    """Qt's FFmpeg backend echoes harmless decoder warnings to the console on every seek (e.g.
    "[aac @ ...] Could not update timestamps for skipped samples"). Raise FFmpeg's log threshold to
    ERROR so only real errors print. Best effort: loads the avutil that ships with PySide6 (same
    library instance Qt uses). Set QUICKCUT_VERBOSE_FFMPEG=1 to leave the log alone."""
    if os.environ.get("QUICKCUT_VERBOSE_FFMPEG"):
        return False
    try:
        import PySide6
        d = os.path.dirname(PySide6.__file__)
        for pat in ("avutil*.dll", "libavutil*.so*", "libavutil*.dylib"):
            for f in glob.glob(os.path.join(d, pat)) + glob.glob(os.path.join(d, "Qt", "lib", pat)):
                ctypes.CDLL(f).av_log_set_level(16)       # 16 == AV_LOG_ERROR
                return True
    except Exception:
        pass
    return False


# [MAP] os.replace() retried up to 10 x 0.3 s. On Windows a file can stay locked for a moment after we close
# it (async handle release, antivirus, search indexer, cloud sync). Returns (ok, error_text).
# Users: rename_media and the Save-Over finish step.
# [PITFALL] In the failure case it blocks the GUI thread for ~3 s.
def _replace_with_retry(src, dst, tries=10, delay=0.3):
    """os.replace() that retries briefly - on Windows a file can stay locked for a moment
    after we close it (async handle release, antivirus scan, search indexer, etc.)."""
    last = None
    for _ in range(tries):
        try:
            os.replace(src, dst)
            return True, None
        except OSError as e:
            last = e
            time.sleep(delay)
    return False, str(last)


# [MAP] Timecode string HH:MM:SS:FF (non-drop-frame). The frame number is fraction*fps, capped at
# round(fps)-1 so 29.97 fps can never display ":30". frames=False gives HH:MM:SS (used by the ruler when tick
# spacing is >= 1 s). Also used to build screenshot file names.
def fmt_tc(t, fps=30.0, frames=True):
    t = max(0.0, float(t))
    fr = max(1, int(round(fps)))
    s = int(t)
    ff = min(int((t - s) * fps + 1e-6), fr - 1)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if frames:
        return f"{h:02d}:{m:02d}:{sec:02d}:{ff:02d}"
    return f"{h:02d}:{m:02d}:{sec:02d}"


DEFAULT_BG = "#000000"     # blank/background color used when an xf carries none (backward-compat default)


# [MAP] xf may be stored as a 6-tuple (legacy, no color) or a 7-tuple (cw,ch,px,py,pw,ph,color). Every
# reader of xf goes through this so a missing 7th element never has to be special-cased at each call site.
def xf_color(xf):
    """The blank/background color of an xf tuple, or DEFAULT_BG if it carries none."""
    return xf[6] if len(xf) > 6 else DEFAULT_BG


# [INVARIANT] Every crop/resize transform that gets STORED in Seg.xf must pass through here (only
# MainWindow.on_stage_commit creates them). It makes all sizes and offsets EVEN (yuv420 chroma needs it) and
# clamps sizes to 16..16384 so nothing collapses to zero or explodes. libx264/yuv420p export fails on odd
# sizes - if you add another producer of xf, call norm_xf on it.
def norm_xf(xf):
    """Snap a crop/resize transform to values ffmpeg/libx264 accept: every size and offset even
    (yuv420 chroma), sizes within 16..16384 so nothing collapses to zero or explodes. Passes the
    blank/background color (7th element) through unchanged, defaulting to DEFAULT_BG if absent."""
    cw, ch, px, py, pw, ph = xf[:6]
    ev = lambda v: max(16, min(16384, int(round(v / 2.0)) * 2))
    return (ev(cw), ev(ch), int(round(px / 2.0)) * 2, int(round(py / 2.0)) * 2, ev(pw), ev(ph), xf_color(xf))


# [MAP][INVARIANT] Single source of truth for crop/resize -> ffmpeg -vf.
# xf = (canvas_w, canvas_h, pic_x, pic_y, pic_w, pic_h, bg_color), all in SOURCE pixels (post-rotation-tag
# size): the OUTPUT frame is canvas_w x canvas_h; the source picture, scaled to pic_w x pic_h, has its
# top-left corner at (pic_x, pic_y) inside that frame. bg_color (e.g. "#1a2b3c") fills any area not
# covered by the picture; it defaults to DEFAULT_BG (black) when the tuple is a legacy 6-tuple.
#     pure crop      canvas shrinks, picture offset goes negative, picture size unchanged
#     resize         picture size changes, canvas unchanged; uncovered area is bg_color
#     un-crop (v4)   canvas grows back out over the (still intact) picture; overflow beyond it is bg_color
# Filter chain = scale -> pad (to the union of picture and canvas, bg_color) -> crop (to the canvas) -> setsar=1.
# Only the pieces that are needed are emitted. The live preview implements the SAME geometry in
# VideoView._relayout / VideoStage.relayout - if you change the math here, change it there too.
def xf_filter(xf):
    """ffmpeg -vf chain: scale the picture, pad with the chosen bg color to the union of picture+canvas,
    then crop to the canvas. Handles crop, shrink (bg borders) and grow (overflow cropped)."""
    cw, ch, px, py, pw, ph = xf[:6]
    color = xf_color(xf)
    ux0, uy0, ux1, uy1 = min(0, px), min(0, py), max(cw, px + pw), max(ch, py + ph)
    f = f"scale={pw}:{ph},"
    if (ux1 - ux0, uy1 - uy0) != (pw, ph):
        f += f"pad={ux1 - ux0}:{uy1 - uy0}:{px - ux0}:{py - uy0}:color={color},"
    if (ux1 - ux0, uy1 - uy0) != (cw, ch) or ux0 or uy0:
        f += f"crop={cw}:{ch}:{-ux0}:{-uy0},"
    return f + "setsar=1"


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

    def __init__(self, path, dur, fps=30.0, w=0, h=0, vcodec="", acodec="", has_video=True):
        self.path = path
        self.name = os.path.basename(path)
        self.dur = dur
        self.fps = fps
        self.w, self.h = w, h
        self.vcodec, self.acodec = vcodec, acodec
        self.has_video = has_video
        self.mark_in = 0.0
        self.mark_out = dur
        self.keyframes = None      # filled in by background worker
        self.thumb_path = None

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
class Seg:
    """A clip on the timeline: a slice [in_s, out_s] of a Media (source seconds).
    `dur` is the TIMELINE duration (source span / speed); `src_dur` is the source span."""
    __slots__ = ("media", "in_s", "out_s", "xf", "mute", "speed", "mirror", "rot", "rev")

    # [STALE][KNOWN ISSUE F14] The trailing comments below say mirror and rot are "export only". Since v3 they are
    #   ALSO shown live
    # in the preview (MainWindow.refresh_stage -> VideoStage.set_state -> VideoView.set_layout). Left unchanged so
    # this file stays code-identical.
    def __init__(self, media, in_s, out_s, xf=None, mute=False, speed=1.0, mirror=False, rot=0.0, rev=False):
        self.media, self.in_s, self.out_s = media, in_s, out_s
        self.xf = xf        # crop/resize: output canvas (w,h) + where the scaled picture sits in it
        self.mute = mute    # per-clip: silence this clip's audio
        self.speed = speed  # per-clip playback speed (1.0 = normal)
        self.mirror = mirror  # per-clip horizontal flip (export only)
        self.rot = rot        # per-clip rotation, degrees clockwise (export only)
        self.rev = rev        # per-clip reverse playback (export re-encodes)

    @property
    def src_dur(self):
        return self.out_s - self.in_s

    @property
    def dur(self):
        return (self.out_s - self.in_s) / (self.speed or 1.0)

    # [INVARIANT] THE lossless / re-encode switch. None => the clip can be stream-copied. Non-None => it goes through
    # ExportWorker._fx_args (re-encode). `mute` only counts when the media really has audio (muting a silent file
    # must not force a re-encode). `rot` is normalised to [0,360); values within 1e-6 of 0/360 mean "no rotation".
    # Tuple order (mute, speed, mirror, rot, rev) is relied on by _fx_args (unpack) and _concat_reencode
    # (p[4][3] == rot). Append new entries at the END.
    @property
    def opts(self):
        """(mute, speed, mirror, rot, rev) if this clip needs re-encoding for its options, else None."""
        m = bool(self.mute and self.media.acodec.strip())
        r = self.rot % 360.0
        if m or abs(self.speed - 1.0) > 1e-6 or self.mirror or self.rev or (r > 1e-6 and 360.0 - r > 1e-6):
            return (m, self.speed, bool(self.mirror), r, bool(self.rev))
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

    def __init__(self):
        super().__init__()
        self.segs = []
        self.undo_stack, self.redo_stack = [], []
        self.snap = True
        self.precise = False   # when True, cuts may land off-keyframe; export re-encodes just that sliver

    # [COUPLING] Tuple order == Seg.__init__ order (see the Seg checklist). Snapshots hold Media by reference and
    # plain values for everything else, so they are cheap and immune to later in-place mutation of a Seg.
    def snapshot(self):
        return [(s.media, s.in_s, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev) for s in self.segs]

    # [PITFALL] Builds NEW Seg objects. Never keep a Seg reference across undo/redo (it would point at an orphan).
    # Timeline.sel is an index, which is why selection survives (clamped in Timeline._on_seq).
    def _restore(self, snap):
        self.segs = [Seg(*x) for x in snap]

    # [MAP] Push `before` on the undo stack (depth capped at 100) and clear redo. Timeline drags call this directly.
    def commit(self, before):
        self.undo_stack.append(before)
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    # [INVARIANT] fn() returning exactly False means "nothing happened, do not commit". ANY other return value
    # (True, 0, 0.0, None, a float) COMMITS. The identity test `r is not False` is deliberate: split_at returns the
    # split time (a float) and edit() hands it back to MainWindow.split_at for the status bar. Do not "simplify" to
    # `if r:` - a legitimate 0.0 would then be treated as failure.
    def edit(self, fn):
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
        return sum(s.dur for s in self.segs)

    def starts(self):
        out, acc = [], 0.0
        for s in self.segs:
            out.append(acc)
            acc += s.dur
        return out

    # [MAP] timeline time -> (segment index, offset inside that segment in TIMELINE seconds). Past the end it
    # returns (last index, its dur). Empty timeline returns (None, 0.0) - callers MUST handle idx None.
    def locate(self, t):
        acc = 0.0
        for i, s in enumerate(self.segs):
            if t < acc + s.dur:
                return i, max(0.0, t - acc)
            acc += s.dur
        if self.segs:
            return len(self.segs) - 1, self.segs[-1].dur
        return None, 0.0

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
        self.segs[idx:idx + 1] = [Seg(s.media, s.in_s, src, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev), Seg(s.media, src, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev)]
        return src

    # [MAP] Ripple delete: neighbours close the gap automatically (there is no gap model).
    def delete(self, idx):
        if 0 <= idx < len(self.segs):
            del self.segs[idx]
            return True
        return False

    # [MAP] Insert `seg` at timeline time t (drag-drop, paste, double-click in Project). tol = how close to a clip
    # boundary still counts as "at the boundary" (0.1 s normally; 8 px worth of time for drops, see on_files_dropped).
    # At a boundary/end: plain list insert. Inside a clip: the clip is split (keyframe-snapped in Snap mode; if it
    # has no keyframe inside, the seg goes to the nearer edge instead). Returns the index of the inserted seg.
    # [COUPLING] The split branch copies every Seg field - keep in step with Seg.
    def insert_at(self, t, seg, tol=0.1):
        if not self.segs:
            self.segs.append(seg)
            return 0
        if t >= self.total() - tol:
            self.segs.append(seg)
            return len(self.segs) - 1
        idx, off = self.locate(max(0.0, t))
        s = self.segs[idx]
        if off <= tol:
            self.segs.insert(idx, seg)
            return idx
        if s.dur - off <= tol:
            self.segs.insert(idx + 1, seg)
            return idx + 1
        lo, hi = s.in_s + 0.02, s.out_s - 0.02
        src = s.in_s + off * s.speed
        if self.snap and not self.precise and s.media.keyframes:
            k = s.media.nearest_kf(src, lo, hi)
            if k is None:                      # no keyframe inside: use nearest edge
                pos = idx if off < s.dur / 2 else idx + 1
                self.segs.insert(pos, seg)
                return pos
            src = k
        self.segs[idx:idx + 1] = [Seg(s.media, s.in_s, src, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev), seg, Seg(s.media, src, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev)]
        return idx + 1

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


# ----------------------------------------------------------------------------- probing
# [MAP] Reads duration / fps / size / codecs by parsing the STDERR TEXT of `ffmpeg -i file` (works without
# ffprobe). Returns a Media, or None when unreadable or duration <= 0.
# [PITFALL] Regex based. If a future ffmpeg changes its banner format, fps silently falls back to 30.0 and size
# to 0x0 (which disables crop/resize because the code tests `not media.w`). Re-test after upgrading ffmpeg.
#   - Phone clips: a "rotation of +-90 degrees" display matrix swaps w/h, so Media.w/h are the DISPLAYED size
#     (consistent with Qt playback and with ffmpeg auto-rotate when re-encoding).
#   - has_video ignores "attached pic" streams (cover art inside mp3/m4a).
#   - variable-frame-rate files report a single nominal fps here.
# [KNOWN ISSUE F12] Runs SYNCHRONOUSLY on the GUI thread from MainWindow.import_paths (25 s timeout per file):
# importing from a slow/network drive freezes the window. Consider moving it into MediaWorker.
def probe_media(path):
    if not FFMPEG:
        return None
    try:
        txt = run_tool([FFMPEG, "-hide_banner", "-i", path], timeout=25).stderr
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if not m:
        return None
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if dur <= 0:
        return None
    lines = txt.splitlines()
    vline = next((l for l in lines if "Video:" in l and "attached pic" not in l), "")
    aline = next((l for l in lines if "Audio:" in l), "")
    fps = 30.0
    mm = re.search(r"([\d.]+)\s*fps", vline)
    if mm:
        try:
            fps = float(mm[1]) or 30.0
        except ValueError:
            pass
    w = h = 0
    mm = re.search(r",\s*(\d{2,5})x(\d{2,5})", vline)
    if mm:
        w, h = int(mm[1]), int(mm[2])
    rot = re.search(r"rotation of (-?[\d.]+) degrees", txt)
    if rot and w and int(round(abs(float(rot[1])))) % 180 == 90:
        w, h = h, w                       # phone video: displayed (and exported) size is swapped
    vc = (re.search(r"Video:\s*(\w+)", vline) or [None, ""])[1]
    ac = (re.search(r"Audio:\s*(\w+)", aline) or [None, ""])[1]
    hz = (re.search(r"(\d+)\s*Hz", aline) or [None, ""])[1]
    return Media(path, dur, fps, w, h, vc, f"{ac} {hz}".strip(), bool(vline))


# [MAP] Extract one frame to a small jpg under %TEMP%/<cache_dir>, cached on disk by md5(key). Returns the path
# or None. Used for the Project thumbnail (MediaWorker) and timeline filmstrips (Timeline._gen_thumb).
# [INVARIANT] `key` must contain everything that would invalidate the picture. The Project thumbnail key includes
# the file's mtime (correct). The timeline filmstrip key does NOT [KNOWN ISSUE F5]: after Save-Over the same
# "path|time" key is served from the old on-disk jpg. Cache folders are never purged [F15].
def gen_thumb_file(path, t, key, cache_dir, width=120):
    """Extract one frame as a small jpg, cached on disk by key. Returns file path or None."""
    d = os.path.join(tempfile.gettempdir(), cache_dir)
    os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, hashlib.md5(key.encode()).hexdigest() + ".jpg")
    if os.path.isfile(fn):
        return fn
    try:
        run_tool([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                  "-ss", f"{max(0.0, t):.2f}", "-i", path,
                  "-frames:v", "1", "-vf", f"scale={width}:-2", fn], timeout=20)
    except Exception:
        return None
    return fn if os.path.isfile(fn) else None


# [MAP] One daemon thread + queue that loads imported media in the background:
#    (1) project thumbnail -> emits `ready`;   (2) pre-read of the first 48 MB so the OS cache is warm and
#    playback starts without a stall;          (3) streamed `ffprobe -show_entries packet=pts_time,flags` to
#    collect KEYFRAME times, with progress derived from packet timestamps -> emits `ready` again at the end.
# `ready` / `progress` are emitted from the worker thread; Qt queues them to the GUI thread (safe).
# [INVARIANT] Job versioning: _gen[media] holds the id of the media's current job. start() bumps it, cancel()
# deletes it, and the worker re-checks _alive(media, gen) between EVERY step and never publishes results of a
# cancelled/superseded job (verified: a cancelled job leaves media.keyframes untouched).
# cancel(m, wait=True) kills the running ffprobe and blocks (<= 3 s) until the loader has released the file -
# REQUIRED before rename / Save-Over on Windows.
# [COUPLING] Every place that calls worker.cancel(...) must also call ReverseProxy.cancel_media(path), and must
# call MainWindow._resume_load(m) if the operation then fails, so keyframes still get (re)loaded.
class MediaWorker(QObject):
    """Background loader for imported media. Jobs run one at a time (queued) so several imports
    don't fight over the disk. Per job it: grabs the project-panel thumbnail, pre-reads the head of
    the file so the OS cache is warm for playback, then streams an ffprobe keyframe scan (needed
    for lossless-accurate cuts) and reports real progress from how far through the file that scan is."""
    ready = Signal(object)
    progress = Signal(object, float)      # (media, 0.0 .. 1.0); 1.0 == fully loaded

    WARM_BYTES = 48 * 1024 * 1024         # how much of the file head to pre-read
    CHUNK = 4 * 1024 * 1024
    SCAN_TIMEOUT = 600.0

    def __init__(self):
        super().__init__()
        self._q = queue.Queue()
        self._gen = {}                    # media -> id of its current job (missing = cancelled)
        self._lock = threading.Lock()
        self._current = None              # media the loader thread is working on right now
        self._proc = None                 # its running ffprobe process, if any
        self._last_emit = 0.0
        self._last_p = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- public API (call from the GUI thread)
    def start(self, media):
        with self._lock:
            gen = self._gen.get(media, 0) + 1
            self._gen[media] = gen
        self.progress.emit(media, 0.0)
        self._q.put((media, gen))

    def cancel(self, media, wait=False):
        """Abort loading `media`. With wait=True, block briefly until the loader has let go of the
        file (needed before renaming / overwriting it on Windows)."""
        with self._lock:
            self._gen.pop(media, None)
            if self._current is media and self._proc is not None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if wait:
            deadline = time.monotonic() + 3.0
            while self._current is media and time.monotonic() < deadline:
                time.sleep(0.01)

    # ---- worker thread
    def _alive(self, media, gen):
        return self._gen.get(media) == gen

    # [MAP] Progress emitter: throttled to ~12 Hz and forced monotonic within a job (_last_p), so the blue load
    # line in ProjectRow never jumps backwards.
    def _emit(self, media, gen, p, force=False):
        now = time.monotonic()
        p = min(1.0, max(self._last_p, p))
        if force or now - self._last_emit >= 0.08:
            self._last_emit, self._last_p = now, p
            if self._alive(media, gen):
                self.progress.emit(media, p)

    # [MAP] Worker-thread main loop. Skips jobs cancelled while queued. `finally` ALWAYS emits progress 1.0 for a
    # still-current job (even if it failed) so the load bar can never get stuck; exceptions are swallowed on purpose.
    # A failed scan therefore leaves media.keyframes None: Snap-mode start-trims stay blocked, Precise still works.
    def _loop(self):
        while True:
            media, gen = self._q.get()
            if not self._alive(media, gen):
                continue                              # removed / superseded while queued
            self._current = media
            self._last_p, self._last_emit = 0.0, 0.0
            try:
                self._load(media, gen)
            except Exception:
                pass
            finally:
                self._proc = None
                self._current = None
                if self._alive(media, gen):
                    self.progress.emit(media, 1.0)    # always finish, even if something failed

    # [MAP] Progress budget: thumbnail + cache warm-up use the first 12 % of the bar (3 % after the thumbnail);
    # the keyframe scan uses the remaining 88 %, measured as packet_time/duration. Audio-only media skip the scan
    # and use the whole bar for warm-up.
    # [KNOWN ISSUE F10] If the scan exceeds SCAN_TIMEOUT (600 s) ffprobe is killed but the PARTIAL keyframe list is
    # still published as if complete, so later cut points silently do not exist (Snap finds nothing past that time).
    def _load(self, media, gen):
        scan = bool(media.has_video and FFPROBE)
        head_w = 0.12 if scan else 1.0                # share of the bar used before the scan

        if media.has_video and FFMPEG:
            key = f"{media.path}|{os.path.getmtime(media.path)}|proj"
            thumb = gen_thumb_file(media.path, min(1.0, media.dur / 2), key, "quickcut_thumbs", width=160)
            if thumb and self._alive(media, gen):
                media.thumb_path = thumb
                self.ready.emit(media)
        if not self._alive(media, gen):
            return
        p0 = 0.03 if scan else 0.0
        self._emit(media, gen, p0, force=True)

        # warm the OS file cache with the head of the file so playback starts without a stall
        try:
            total = min(os.path.getsize(media.path), self.WARM_BYTES)
            done = 0
            with open(media.path, "rb") as f:
                while done < total and self._alive(media, gen):
                    got = len(f.read(min(self.CHUNK, total - done)))
                    if not got:
                        break
                    done += got
                    self._emit(media, gen, p0 + (head_w - p0) * (done / max(1, total)))
        except OSError:
            pass
        if not self._alive(media, gen):
            return
        self._emit(media, gen, head_w, force=True)

        if not scan:
            return
        # keyframe scan - streamed so we can report progress from the packet timestamps
        with self._lock:
            if not self._alive(media, gen):
                return
            self._proc = subprocess.Popen(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", media.path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", creationflags=NOWIN)
            proc = self._proc
        kfs = []
        deadline = time.monotonic() + self.SCAN_TIMEOUT
        dur = max(media.dur, 0.001)
        for line in proc.stdout:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    t = float(parts[0])
                except ValueError:
                    continue
                if "K" in parts[1]:
                    kfs.append(t)
                self._emit(media, gen, head_w + (1.0 - head_w) * min(1.0, t / dur))
            if time.monotonic() > deadline:
                proc.kill()
                break
        proc.wait()
        if not self._alive(media, gen):
            return                                    # cancelled: don't publish partial keyframes
        kfs.sort()
        media.keyframes = kfs or None
        self.ready.emit(media)


# ----------------------------------------------------------------------------- export
# [MAP] Runs on its own QThread. Built by MainWindow._run_export from Sequence.export_parts().
# PIPELINE (see run):
#   A. tmpdir = mkdtemp NEXT TO THE OUTPUT file (same drive; removed in `finally`).
#   B. _build_part_files - every part becomes exactly one physical file:
#        part has xf or opts .......... _fx_args           -> partNNN.mkv  (re-encode, libx264 crf18 when a
#                                                             video filter is needed)
#        untouched whole file ......... the source path itself (no copy)
#        on a keyframe, or not precise  _cut_args          (stream copy)
#        precise + start off-keyframe . _reencode_args     (video re-encoded; audio copied)  <-- see F1
#   C. combine:  1 file  -> remux copy
#                all parts "match" (_parts_match) -> concat DEMUXER with -c copy
#                otherwise -> _concat_reencode (concat FILTER, everything re-encoded, scaled into the largest
#                frame and centred on black)                                                  <-- see F2
#   D. optional 2nd pass over the staged file:  GIF -> _make_gif   |   Transcode -> _transcode
#      (for GIF/Transcode step C writes tmpdir/stage.mkv instead of the final path).
# [DO NOT BREAK] Steps B-C are the delicate lossless logic. GIF and Transcode were deliberately added as a
# SECOND PASS so that they never touch it. Add new output formats as a step D, not inside B/C.
# Errors: any exception -> done(False, message). MainWindow compares the message to the literal "Cancelled"
# to tell a user cancel from a failure - keep that string.
# Threading: only this thread's run() spawns export ffmpeg processes; `_proc` is the running one so cancel()
# can terminate it from the GUI thread.
class ExportWorker(QThread):
    progress = Signal(int, str)
    done = Signal(bool, str)

    # [MAP] gif and transcode are alternatives (export_gif passes gif=, export passes transcode=); both None = normal
    # lossless pipeline. `precise` is forced True for GIF by MainWindow._run_export (frame-accurate GIF starts).
    def __init__(self, parts, out, precise=False, gif=None, transcode=None):
        super().__init__()
        self.gif = gif                  # GIF preset dict {w, fps, lossy, colors} or None (normal video export)
        self.transcode = transcode      # HandBrake-lite preset dict, or None (normal lossless pipeline)
        self.parts, self.out = parts, out
        self.precise = precise
        self._cancel = False
        self._proc = None

    # [KNOWN ISSUE F7] _cancel is only checked AFTER an ffmpeg step returns (in _ffmpeg). terminate() kills the
    # running step immediately, but a Cancel that lands BETWEEN steps lets the next step start and finish before
    # "Cancelled" is raised. Fix: check self._cancel at the top of _ffmpeg and in the _build_part_files loop.
    def cancel(self):
        self._cancel = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # [INVARIANT] The ONE place export spawns ffmpeg: `-y -hide_banner -loglevel error`, stdout/stderr piped, no
    # console window. Non-zero exit -> RuntimeError with the last 1500 chars of stderr, which MainWindow shows in
    # the "Export failed" box. Any new export step must call this (so cancel + error reporting keep working).
    def _ffmpeg(self, args):
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + args
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding="utf-8", errors="replace",
                                      creationflags=NOWIN)
        _, err = self._proc.communicate()
        rc = self._proc.returncode
        self._proc = None
        if self._cancel:
            raise RuntimeError("Cancelled")
        if rc != 0:
            raise RuntimeError((err or "")[-1500:] or f"ffmpeg exited with code {rc}")

    # [MAP] Lossless copy of [a, b] from the SOURCE. -ss/-to are INPUT options (before -i): fast keyframe seek;
    # with -c copy the output begins at the keyframe at-or-before `a`, so it is frame-exact only when `a` sits on a
    # keyframe (which is what Snap mode guarantees). -avoid_negative_ts make_zero rebases timestamps.
    # `-map 0:v? -map 0:a?` keeps only video+audio (subtitles/data streams are dropped by design).
    # `-to` as an INPUT option was verified on ffmpeg 6.1.1; re-test if you lower the supported ffmpeg version.
    @staticmethod
    def _cut_args(media, a, b, out):
        """Fast lossless stream-copy cut. Frame-accurate only if 'a' lands on a keyframe."""
        return ["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", media.path,
                "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-avoid_negative_ts", "make_zero", out]

    # [KNOWN ISSUE F1][PITFALL] Used in Precise mode when the start is NOT on a keyframe. The whole span [a, b] of
    # VIDEO is re-encoded (x264 veryfast, crf 18, yuv420p); audio is stream-copied. It is NOT only "the sliver up to
    # the next keyframe" as the Help text, the Precise tooltip and the dev notes claim - measured: 0 of 150 frames
    # after the first keyframe were bit-identical to the source. `-r` locks the output frame rate to the source fps
    # (without it short spans can drift). Either fix the wording or implement a true head-only re-encode + tail copy.
    @staticmethod
    def _reencode_args(media, a, b, out):
        """Re-encodes just the video for this span so it can start on any frame, not only a
        keyframe. Audio is still stream-copied (audio doesn't have this limitation). The frame
        rate is locked explicitly - without it, re-encoding a short span can let ffmpeg's
        default frame timing drift away from the source's actual fps."""
        return ["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", media.path,
                "-map", "0:v?", "-map", "0:a?",
                "-r", f"{media.fps:.6f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy", out]

    # [STALE][KNOWN ISSUE F14] DEAD CODE: nothing calls _xf_args any more - crop/resize is handled by _fx_args (which
    #   calls
    # xf_filter). Safe to delete after a search; kept so the file stays code-identical.
    @staticmethod
    def _xf_args(media, a, b, xf, out):
        """Crop/resize needs a real re-encode of this span (audio still copied)."""
        return ["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", media.path,
                "-map", "0:v?", "-map", "0:a?", "-vf", xf_filter(xf),
                "-r", f"{media.fps:.6f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy", out]

    # [MAP] ffmpeg's atempo only accepts 0.5..2.0 per instance, so other speeds are expressed as a chain, e.g. 4x ->
    # "atempo=2.0,atempo=2.000000". Mirrors the video setpts=PTS/speed so audio and video stay in sync.
    @staticmethod
    def _atempo(sp):
        f = []
        while sp > 2.0:
            f.append("atempo=2.0")
            sp /= 2.0
        while sp < 0.5:
            f.append("atempo=0.5")
            sp /= 0.5
        f.append(f"atempo={sp:.6f}")
        return ",".join(f)

    # [MAP] Re-encode ONE part that has crop/resize and/or clip options. Filter ORDER is fixed (the preview and the
    # math in _concat_reencode assume it):  crop/resize (xf_filter) -> mirror -> rotate -> reverse -> speed.
    #   - rotate: 90/180/270 use transpose/flip; other angles use the rotate filter (UI only produces 90 steps).
    #   - reverse buffers the WHOLE clip in RAM (fine for short clips; the 30 s cap only limits the PREVIEW proxy).
    #   - speed: video setpts=PTS/speed; audio atempo chain. Reverse audio = areverse.
    #   - mute: replaces audio with an `anullsrc` silent stereo 48 kHz track, AAC 192k, ended by -shortest.
    #   - audio otherwise stream-copied, except when speed/reverse force AAC 192k.
    #   - output is ALWAYS written to .mkv (see _build_part_files) because mkv can hold any copied audio codec.
    # [KNOWN ISSUE F3] If no video filter is needed (for media that has video the ONLY such case is "mute only"),
    #   video is stream-copied
    # (`-c:v copy`) so the clip starts at the previous keyframe even in Precise mode: measured 5.16 s instead of the
    # expected 4.7 s for a 3.3 -> 8.0 cut. This classmethod has no access to keyframes/precise flags, so a fix must
    # pass them in.
    @classmethod
    def _fx_args(cls, media, a, b, xf, opts, out):
        """Crop/resize and/or per-clip mute/speed/mirror/rotation: re-encode this span.
        Order: crop/resize -> mirror -> rotate -> speed."""
        mute, speed, mirror, rot, rev = opts if opts else (False, 1.0, False, 0.0, False)
        fast = abs(speed - 1.0) > 1e-6
        has_a = bool(media.acodec.strip())
        vf = []
        if media.has_video:
            if xf:
                vf.append(xf_filter(xf))
            if mirror:
                vf.append("hflip")
            r = rot % 360.0
            if abs(r - 90) < 1e-6:
                vf.append("transpose=1")
            elif abs(r - 180) < 1e-6:
                vf.append("hflip,vflip")
            elif abs(r - 270) < 1e-6:
                vf.append("transpose=2")
            elif r > 1e-6 and 360.0 - r > 1e-6:
                vf.append(f"rotate={r:.4f}*PI/180:ow=iw:oh=ih:c=black")
            if rev:
                vf.append("reverse")      # buffers the whole clip in RAM - fine for short clips
            if fast:
                vf.append(f"setpts=PTS/{speed:.6f}")
        args = ["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", media.path]
        if mute and has_a:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]     # silent replacement track
        args += ["-map", "0:v?"]
        if has_a:
            args += ["-map", "1:a"] if mute else ["-map", "0:a?"]
        if vf:
            args += ["-vf", ",".join(vf), "-r", f"{media.fps:.6f}", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]
        if has_a:
            if mute:
                args += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
            elif fast or rev:
                af = (["areverse"] if rev else []) + ([cls._atempo(speed)] if fast else [])
                args += ["-af", ",".join(af), "-c:a", "aac", "-b:a", "192k"]
            else:
                args += ["-c:a", "copy"]
        return args + [out]

    # [MAP] Keyframe at-or-before t (eps = 20 ms tolerance). None if keyframes are not loaded. Used only to decide
    # "is the start already on a keyframe?" in _build_part_files.
    @staticmethod
    def _kf_before(media, t, eps=0.02):
        kf = media.keyframes
        if not kf:
            return None
        i = bisect.bisect_right(kf, t + eps) - 1
        return kf[i] if i >= 0 else None

    # [MAP] Step B of the pipeline (see class notes). Emits progress (i, "Preparing clip i of n...") for i = 0..n-1.
    # Decision order matters: fx parts first; then the "whole untouched file" shortcut (start <= 0.02 s and end
    # >= dur - 0.05 s -> use the original path, no processing); then cut vs precise re-encode. Temp part names keep
    # the source extension for copy cuts (container must be able to hold the copied codecs) and .mkv for fx parts.
    # [KNOWN ISSUE F1] The precise branch re-encodes the entire part.
    def _build_part_files(self, tmpdir, parts):
        """Returns the list of physical files (in order) that make up the export. Each
        timeline part becomes exactly one file: a fast lossless copy when possible, or - only
        in precise mode, only when the requested start isn't on a keyframe - a re-encode of
        that part's video (audio always stays a plain copy). Parts that are already whole,
        untouched clips, or already start on a keyframe, never get re-encoded."""
        files = []
        for i, part in enumerate(parts):
            m, a, b, xf = part[:4]
            opts = part[4] if len(part) > 4 else None
            self.progress.emit(i, f"Preparing clip {i + 1} of {len(parts)}...")
            if xf or opts:
                tmp = os.path.join(tmpdir, f"part{i:03d}.mkv")   # .mkv holds any audio codec we copy
                self._ffmpeg(self._fx_args(m, a, b, xf, opts, tmp))
                files.append(tmp)
                continue
            if a <= 0.02 and b >= m.dur - 0.05:                # whole file: no cut needed at all
                files.append(m.path)
                continue
            kf_before = self._kf_before(m, a) if m.keyframes else None
            on_keyframe = kf_before is not None and abs(kf_before - a) < 0.02
            tmp = os.path.join(tmpdir, f"part{i:03d}{os.path.splitext(m.path)[1]}")
            if on_keyframe or not self.precise or not m.has_video:
                self._ffmpeg(self._cut_args(m, a, b, tmp))
            else:
                self._ffmpeg(self._reencode_args(m, a, b, tmp))
            files.append(tmp)
        return files

    # [INVARIANT][DO NOT BREAK] Gatekeeper for the concat DEMUXER with -c copy. That splice does no re-encoding: if
    # parts differ in codec / resolution / fps / audio format it silently produces a corrupt file (frozen video
    # while audio continues, wildly wrong duration). True only when EVERY media shares vcodec, w, h, fps (+-0.01) and
    # acodec ("codec hz" string), and NO part has xf/opts. Anything else -> _concat_reencode.
    @staticmethod
    def _parts_match(parts):
        """True only if every part shares the same video codec/resolution/fps and audio
        codec - meaning a fast stream-copy concat is safe. The concat demuxer with '-c copy'
        doesn't re-encode anything: if the parts don't actually match, it silently produces a
        corrupt file - players freeze on the last frame they can decode while audio keeps
        going from the raw packet stream, and the container's summed duration can end up
        wildly wrong. Any mismatch here means we must re-encode instead."""
        medias = [p[0] for p in parts]
        if len(medias) < 2:
            return True
        if any(p[3] or (len(p) > 4 and p[4]) for p in parts):
            return False          # a crop/resize/speed/mute part is freshly encoded - never splice it by copy
        first = medias[0]
        return all(m.vcodec == first.vcodec and m.w == first.w and m.h == first.h
                   and abs(m.fps - first.fps) < 0.01 and m.acodec == first.acodec
                   for m in medias[1:])

    # [MAP] Join dissimilar clips with ffmpeg's concat FILTER (decode + re-encode everything: libx264 veryfast crf 18,
    # AAC). All inputs must share one frame size, so every clip is scaled to fit inside the LARGEST clip's frame
    # (never stretched, aspect preserved, centred on black). Target size uses xf canvas or media size and swaps
    # w/h for 90/270 rotation (that is the p[4][3] read).
    # [KNOWN ISSUE F2] The filter graph hard-codes an audio input `[i:a:0]` for every clip. If ANY clip has no audio
    # stream (screen recordings, silent clips) ffmpeg fails with "matches no streams" - verified with a clip that has
    # audio joined to one that does not, and with two mismatched video-only clips. Fix: probe per-clip audio and
    # feed anullsrc (or drop audio when none has it).
    def _concat_reencode(self, files, parts, out):
        """Join clips whose encoding doesn't match via ffmpeg's concat FILTER (decodes and
        re-encodes everything into one consistent stream), instead of the concat demuxer
        (which just splices packets and requires identical parameters). The filter also
        requires every input to share one resolution, so each clip is scaled to fit inside
        the largest clip's frame (never upscaled beyond it) and centered on a black
        background rather than stretched, preserving its own aspect ratio."""
        sizes = [(p[3][0], p[3][1]) if p[3] else (p[0].w, p[0].h) for p in parts]
        sizes = [(h, w) if (p[4] and round(p[4][3]) % 180 == 90) else (w, h)      # 90/270 deg swaps dims
                 for (w, h), p in zip(sizes, parts)]
        tw = max((w for w, h in sizes if w), default=1920)
        th = max((h for w, h in sizes if h), default=1080)
        tw, th = tw - (tw % 2), th - (th % 2)   # even dims required by libx264/yuv420p
        args = []
        scale_chain = []
        concat_inputs = []
        for i, f in enumerate(files):
            args += ["-i", f]
            scale_chain.append(
                f"[{i}:v:0]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]")
            concat_inputs.append(f"[v{i}][{i}:a:0]")
        filt = ";".join(scale_chain) + ";" + "".join(concat_inputs) + \
            f"concat=n={len(files)}:v=1:a=1[outv][outa]"
        self._ffmpeg(args + ["-filter_complex", filt, "-map", "[outv]", "-map", "[outa]",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", out])

    # [MAP] Step D (GIF): ffmpeg  fps -> scale(min(width, source), lanczos) -> palettegen(max_colors, stats_mode=diff)
    # -> paletteuse(dither, diff_mode=rectangle), audio dropped, infinite loop. If gifsicle is found and lossy > 0
    # the raw GIF goes to tmpdir/raw.gif and `gifsicle -O3 --lossy=N` writes the final file (if gifsicle fails the
    # un-lossy GIF is copied instead). Without gifsicle "lossy" is approximated by a coarser bayer dither.
    # [DO NOT BREAK] Only dither=floyd_steinberg or bayer are used - sierra2_4 failed on the user's ffmpeg build.
    def _make_gif(self, src):
        """ffmpeg palettegen/paletteuse; true lossy compression via gifsicle when it is available
        (next to QuickCut or on PATH), otherwise lossy is approximated with coarser dithering."""
        g = self.gif
        lossy = int(g["lossy"])
        gs = find_tool("gifsicle") if lossy > 0 else None
        if lossy <= 0 or gs:
            dither = "floyd_steinberg"
        else:
            dither = "bayer:bayer_scale=%d" % (5 if lossy <= 20 else 4 if lossy <= 40 else 3 if lossy <= 80 else 2)
        dst = os.path.join(os.path.dirname(src), "raw.gif") if gs else self.out
        vf = (f"fps={int(g['fps'])},scale=w='min({int(g['w'])},iw)':h=-2:flags=lanczos,split[a][b];"
              f"[a]palettegen=max_colors={int(g['colors'])}:stats_mode=diff[p];"
              f"[b][p]paletteuse=dither={dither}:diff_mode=rectangle")
        self._ffmpeg(["-i", src, "-an", "-filter_complex", vf, "-loop", "0", dst])
        if gs:
            self._proc = subprocess.Popen([gs, "-O3", f"--lossy={lossy}", dst, "-o", self.out],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)
            rc = self._proc.wait()
            self._proc = None
            if self._cancel:
                raise RuntimeError("Cancelled")
            if rc != 0:
                shutil.copyfile(dst, self.out)      # gifsicle failed: keep the un-lossy GIF

    # [MAP] Step D ("HandBrake-lite"): re-encode the staged file per preset dict {format, encoder, mode, value, h,
    # fps, web_opt}. h/fps == 0 means "Source". Scale filter never upscales (min(h, ih)). Encoders: x264 (crf or
    # bitrate+maxrate+bufsize), nvenc (h264_nvenc vbr; needs an NVIDIA GPU + driver), vp9 (libvpx-vp9; crf needs
    # -b:v 0). webm -> libopus 128k; mp4 -> AAC 192k (+faststart if web_opt).
    # No encoder/container sanity check: an unusable combination fails inside ffmpeg and its stderr surfaces in the
    # normal "Export failed" dialog. Only Export uses this - Save-Over never transcodes (it must stay lossless and
    # keep the exact file name/extension).
    def _transcode(self, src):
        """HandBrake-lite: re-encode the assembled file per the chosen preset (encoder,
        constant-quality or avg-bitrate, resolution, fps, web-optimized, container)."""
        p = self.transcode
        vf, args = [], ["-i", src]
        h = p.get("h") or 0
        if h > 0:
            vf.append(f"scale=-2:'min({int(h)},ih)'")
        fps = p.get("fps") or 0
        if vf:
            args += ["-vf", ",".join(vf)]
        if fps > 0:
            args += ["-r", f"{fps}"]
        enc, mode, val = p["encoder"], p["mode"], float(p["value"])
        if enc == "nvenc":
            args += ["-c:v", "h264_nvenc", "-preset", "p5", "-pix_fmt", "yuv420p"]
            args += ["-rc", "vbr", "-cq", f"{val:g}", "-b:v", "0"] if mode == "cq" else \
                    ["-rc", "vbr", "-b:v", f"{val:g}k", "-maxrate", f"{val * 1.5:.0f}k"]
        elif enc == "vp9":
            args += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-row-mt", "1"]
            args += ["-crf", f"{val:g}", "-b:v", "0"] if mode == "cq" else ["-b:v", f"{val:g}k"]
        else:
            args += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
            args += ["-crf", f"{val:g}"] if mode == "cq" else \
                    ["-b:v", f"{val:g}k", "-maxrate", f"{val * 1.5:.0f}k", "-bufsize", f"{val * 2:.0f}k"]
        args += ["-map", "0:v?", "-map", "0:a?"]
        fmt = p.get("format", "mp4")
        if fmt == "webm":
            args += ["-c:a", "libopus", "-b:a", "128k"]
        else:
            args += ["-c:a", "aac", "-b:a", "192k"]
            if p.get("web_opt"):
                args += ["-movflags", "+faststart"]
        self._ffmpeg(args + [self.out])

    # [MAP] Thread entry. Order: mkdtemp -> pick `out` (stage.mkv for GIF/Transcode else the final path) ->
    # _build_part_files -> combine (remux / concat demuxer / concat filter) -> optional GIF or transcode -> done(True,
    # final_path). Any exception -> done(False, text). `finally` always deletes tmpdir.
    # Progress numbering: n parts emit 0..n-1, then n ("Joining clips..." / "Writing file..."); GIF/Transcode emit
    # one more. [KNOWN ISSUE F17] With a single part the extra step re-emits 1 (cosmetic: bar does not advance).
    # [PITFALL] tmpdir lives next to the output, so a crash/kill leaves a "quickcut_*" folder behind [F15].
    def run(self):
        tmpdir = None
        try:
            parts = self.parts
            tmpdir = tempfile.mkdtemp(prefix="quickcut_", dir=os.path.dirname(os.path.abspath(self.out)))
            # GIF and Transcode both render the normal lossless pipeline to a staging file first,
            # then convert that staged file in a second pass.
            out = os.path.join(tmpdir, "stage.mkv") if (self.gif or self.transcode) else self.out
            files = self._build_part_files(tmpdir, parts)
            self.progress.emit(len(parts), "Writing file..." if len(files) == 1 else "Joining clips...")
            if len(files) == 1:
                self._ffmpeg(["-i", files[0], "-map", "0:v?", "-map", "0:a?", "-c", "copy", out])
            elif self._parts_match(parts):
                lst = os.path.join(tmpdir, "list.txt")
                with open(lst, "w", encoding="utf-8") as fh:
                    for p in files:
                        safe = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
                        fh.write(f"file '{safe}'\n")
                self._ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v?", "-map", "0:a?",
                              "-c", "copy", out])
            else:
                self._concat_reencode(files, parts, out)
            if self.gif:
                self.progress.emit(1 if len(parts) == 1 else len(parts) + 1, "Creating GIF...")
                self._make_gif(out)
            elif self.transcode:
                self.progress.emit(1 if len(parts) == 1 else len(parts) + 1, "Transcoding...")
                self._transcode(out)
            self.done.emit(True, self.out)
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, str(e))
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)


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
class DblSlider(QSlider):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()
        e.accept()


# [MAP] Modal dialog opened by double-clicking a clip (Timeline.clipOptionsRequested -> MainWindow.
# edit_clip_options). Edits mute / speed / mirror / reverse. Rotation is NOT here (the 90-degree button in the
# Resize tool bar does it). The speed slider is logarithmic 0.25x..10x (_s2v/_v2s) with a snap-to-1x zone; the
# spin box accepts 0.05x..100x and always wins.
# [KNOWN ISSUE F6] The spin box allows up to 100x but Engine._apply_fx clamps preview playback to 20x, so above
# 20x the preview desynchronises (playhead crawls, "can't keep up" warning fires). Export is unaffected.
# [COUPLING] values() returns (mute, speed, mirror, rev) in that order - edit_clip_options unpacks it
# positionally. Checkbox is labelled "Reverse playback (Max 30 seconds)": the cap only limits the live PREVIEW
# proxy; export is not capped (details: ReverseProxy).
class ClipOptionsDialog(QDialog):
    """Double-click a clip: mute, speed (slider 0.25x-10x + always-live manual box), mirror."""

    @staticmethod
    def _s2v(x):
        return round(0.25 * 40 ** (x / 1000.0), 2)

    @staticmethod
    def _v2s(v):
        return int(round(1000 * math.log(max(min(v, 10.0), 0.25) / 0.25) / math.log(40)))

    def __init__(self, parent, name, mute, speed, has_audio, mirror, has_video, rev=False):
        super().__init__(parent)
        self.setWindowTitle("Clip options")
        self.setModal(True)
        self.setMinimumWidth(340)
        v = QVBoxLayout(self)
        v.setSpacing(8)
        t = QLabel(self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 310))
        t.setStyleSheet("color:#eaeaea;font-weight:600;")
        v.addWidget(t)
        self.mute = QCheckBox("Mute audio" + ("" if has_audio else "  (no audio track)"))
        self.mute.setChecked(bool(mute) and has_audio)
        self.mute.setEnabled(has_audio)
        v.addWidget(self.mute)
        v.addWidget(QLabel("Speed"))
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
        v.addLayout(row)
        scale = QHBoxLayout()
        scale.setContentsMargins(0, 0, 90, 0)
        for txt in ("0.25x", "1x", "10x"):
            scale.addWidget(QLabel(txt))
            if txt != "10x":
                scale.addStretch(1)
        v.addLayout(scale)
        self.mirror = QCheckBox("Mirror video (flip horizontally)")
        self.mirror.setChecked(bool(mirror) and has_video)
        self.mirror.setEnabled(has_video)
        v.addWidget(self.mirror)
        self.rev = QCheckBox("Reverse playback (Max 30 seconds)")
        self.rev.setChecked(bool(rev) and has_video)
        self.rev.setEnabled(has_video)
        self.rev.setToolTip("Clip plays backwards. Live preview works for clips up to 30 s (a preview copy is\n"
                            "rendered in the background); longer clips still reverse on export.")
        v.addWidget(self.rev)
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

    def _reset(self):
        self.spin.setValue(1.0)
        self.mute.setChecked(False)
        self.mirror.setChecked(False)
        self.rev.setChecked(False)

    def values(self):
        return self.mute.isChecked(), round(self.spin.value(), 2), self.mirror.isChecked(), self.rev.isChecked()


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
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for name in ("Video", "GIF"):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFixedHeight(24)
            b.setStyleSheet("QPushButton{background:transparent;border:1px solid transparent;border-radius:3px;"
                            "padding:0 16px;font-weight:700;color:#9a9a9a;}"
                            "QPushButton:hover:!checked{background:#2e2e2e;color:#e0e0e0;}"
                            "QPushButton:checked{background:#2d8ceb;color:#ffffff;}")
            b.clicked.connect(lambda _=False, n=name: window.request_mode(n))
            grp.addButton(b)
            lay.addWidget(b)
            lay.addSpacing(4)
            self.mode_btns[name] = b
        self.mode_btns["Video"].setChecked(True)
        lay.addStretch(1)
        self.min_btn = self._btn("win_min", "Minimize", window.showMinimized)
        self.max_btn = self._btn("win_max", "Maximize", window.toggle_maximize)
        self.close_btn = self._btn("win_close", "Close", window.close)
        self.close_btn.setObjectName("closeBtn")
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)
        self._drag_pos = None

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

    def __init__(self, media):
        super().__init__()
        self.media = media
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
# Interaction: 8 handles + move (hit test with SLOP 9 px). SHIFT snaps to standard ratios (_snap). Edge/corner
# snapping to the current canvas edge within CANVAS_SNAP (10 SCREEN px, deliberately screen-space so it feels
# looser when zoomed out and tighter when zoomed in). Bounds `b`: crop = picture united with canvas (allows
# "un-crop" past the current frame); resize = whole overlay. Resize corner drags scale proportionally.
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
        self.sel = p if p is not None else QRectF(st.canvas_rect() if st.tool == "crop" else st.picture_rect())
        self.update()

    def _hit(self, pos):
        r, s = self.sel, self.SLOP
        if not (r.left() - s <= pos.x() <= r.right() + s and r.top() - s <= pos.y() <= r.bottom() + s):
            return None
        h = ("l" if abs(pos.x() - r.left()) <= s else "r" if abs(pos.x() - r.right()) <= s else "") + \
            ("t" if abs(pos.y() - r.top()) <= s else "b" if abs(pos.y() - r.bottom()) <= s else "")
        return h or ("move" if r.contains(pos) else None)

    def mousePressEvent(self, e):
        h = self._hit(e.position()) if e.button() == Qt.MouseButton.LeftButton else None
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(self)
            a = m.addAction("Reset crop && resize for this clip")
            if m.exec(e.globalPosition().toPoint()) is a:
                self.stage.resetRequested.emit()
            return
        if h is None:
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
        d = e.position() - p0
        st = self.stage
        b = st.picture_rect().united(st.canvas_rect()) if st.tool == "crop" else QRectF(self.rect())
        r = QRectF(r0)
        if h == "move":
            r.translate(d)
            r.moveLeft(max(b.left(), min(r.left(), max(b.left(), b.right() - r.width()))))
            r.moveTop(max(b.top(), min(r.top(), max(b.top(), b.bottom() - r.height()))))
            c = st.canvas_rect()
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
                c = st.canvas_rect()
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
            full, canvas = st.picture_rect(), st.canvas_rect()
            if full.width() > canvas.width() + 0.5 or full.height() > canvas.height() + 0.5:
                p.setPen(QPen(QColor("#9a9a9a"), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(full)          # ghost: drag the selection out to here to un-crop
        else:
            p.setPen(QPen(QColor("#9a9a9a"), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(st.canvas_rect())                    # the fixed output frame
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
        # dw,dh = the canvas's own on-screen size (pre/post 90 deg swap) - normally equal to this
        # widget's own w,h, except while the crop tool widens the widget to reveal a cropped-out
        # ghost area, in which case (ox, oy) is the canvas's top-left offset within the widget.
        dw, dh = (ch, cw) if rot in (90, 270) else (cw, ch)
        t = QTransform()
        t.translate(ox + dw / 2.0, oy + dh / 2.0)           # (applied bottom-up) picture -> canvas -> mirror -> rotate
        t.rotate(rot)
        t.scale(-1.0 if mir else 1.0, 1.0)
        t.translate(-cw / 2.0, -ch / 2.0)
        t.translate(px, py)
        it.setTransform(t)


# [MAP] Container for the video + crop/resize editing UI (OK/Cancel bar, W x H boxes, swap, rotate).
# Children: `canvas` (black output frame), `vclip` (native-parent host that HARD-CLIPS the video so an oversized
# picture can never spill over the rest of the program), `video` (VideoView), `overlay` (CropOverlay), `bar`.
# THREE COORDINATE SPACES (the main source of confusion in this class):
#   1 pre-rotation canvas units  = the Seg.xf space (source pixels, before rotation/mirror)
#   2 DISPLAYED canvas units     = after rotation/mirror (`_t()` maps 1 -> 2; `_disp_dims()` gives the size).
#                                  `pend` (the pending selection) and the W/H boxes live in THIS space.
#   3 screen pixels of the stage = `_rc` (canvas rect) and `_k` (screen px per canvas unit) map 2 -> 3.
# `_ok()` converts displayed -> pre-rotation and emits committed(tool, rect-in-stage-pixels, bg-color-hex);
# MainWindow.
# on_stage_commit turns that into a new Seg.xf (through norm_xf) via Sequence.edit.
# [INVARIANT] `_rc` / `_k` are computed in relayout() and used by ALL crop maths; the v4 "ghost" widening only
# changes the size of the native video widget (vclip/video) and `_canvas_off`, never `_rc`/`_k`.
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
        # fixed bottom-right action bar: [color swatch] ... [W] x [H] [Cancel] [OK]
        self.bar = QWidget(self)
        lay = QHBoxLayout(self.bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
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
        for b in (self.b_cancel, self.b_ok, self.b_swap, self.b_rot, self.b_color):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoDefault(False)
        self.b_cancel.clicked.connect(self.cancelClicked)
        self.b_ok.clicked.connect(self._ok)
        for w in (self.ed_w, x, self.ed_h, self.b_swap, self.b_rot, self.b_cancel, self.b_ok):
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
        self._zoom, self._pan = 1.0, QPointF(0.0, 0.0)
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
        if self.tool == "crop":
            _, _, px, py, pw, ph = xf[:6]
            full = self._t().mapRect(QRectF(px, py, pw, ph))    # full recoverable extent, display units
            fx0, fy0 = min(0.0, full.x()), min(0.0, full.y())
            fx1 = max(cw, full.x() + full.width())
            fy1 = max(ch, full.y() + full.height())
            w, h = max(16, min(w, fx1 - fx0)), max(16, min(h, fy1 - fy0))
            x, y = max(fx0, min(r.x(), fx1 - w)), max(fy0, min(r.y(), fy1 - h))
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
    # Crop tool additionally widens vclip/video to canvas U picture (the un-crop "ghost").
    # Ends by showing/raising overlay + bar only while a tool is active.
    def relayout(self):
        xf, S = self._eff(), self.rect()
        fx_on = self._rot() != 0 or bool(self.fx[1])
        editing = self.tool is not None and xf is not None
        # while editing, preview the swatch's PENDING color (even pre-OK); otherwise show the committed one
        self.video.set_bg_color(self.bg_color if editing else (xf_color(self.xf) if self.xf else DEFAULT_BG))
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
            if editing and zoom > 1.0:
                # clamp pan so the canvas can never be dragged entirely out of view
                mx = max(0.0, (dw * k - av.width()) / 2 + av.width() * 0.4)
                my = max(0.0, (dh * k - av.height()) / 2 + av.height() * 0.4)
                self._pan = QPointF(max(-mx, min(self._pan.x(), mx)), max(-my, min(self._pan.y(), my)))
            else:
                self._pan = QPointF(0.0, 0.0)
            cx, cy = av.center().x() + self._pan.x(), av.center().y() + self._pan.y()
            rc = QRectF(cx - dw * k / 2, cy - dh * k / 2, dw * k, dh * k).toRect()
            self.canvas.setGeometry(rc)
            self._k = rc.width() / dw
            self._rc = QRectF(rc)
            if editing and self.tool == "crop":
                # Let a ghost of anything already cropped away show through: widen the native
                # video widget to the full recoverable picture extent instead of clipping it to
                # the (already-cropped) canvas. self._rc/self._k - the canvas<->screen mapping
                # every crop selection maths runs in - stay exactly as computed above.
                vrc = QRectF(rc).united(self.picture_rect()).toRect()
            else:
                vrc = rc
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
    _hues = None

    HW, RULER_H = 6, 22
    V_Y, V_H = RULER_H + 3, 92
    BAND_H = 16                            # top label strip inside each clip; rest is thumbnail
    TILE_W = 64                            # target width of one filmstrip thumbnail

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

    # --- state
    def _on_seq(self):
        if self.sel >= len(self.seq.segs):
            self.sel = -1
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
        pps = max(1.0, min(800.0, pps))
        if anchor_x is None:
            anchor_x = self.HW + (self.width() - self.HW) / 2
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
        anchor_x = self._zoom_anchor_x if self._zoom_anchor_x is not None else self.HW + (self.width() - self.HW) / 2
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
            self.set_zoom(self.pps * (1.15 if dy > 0 else 1 / 1.15), anchor_x=e.position().x())
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
                self.sel = idx
                self.update()
            menu = QMenu(self)
            a_split = menu.addAction("Add Edit at Playhead\tShift+C")
            a_copy = menu.addAction("Copy Clip\tCtrl+C")
            a_copy.setEnabled(idx is not None)
            a_paste = menu.addAction("Paste Clip\tCtrl+V")
            a_del = menu.addAction("Ripple Delete\tDel")
            a_del.setEnabled(idx is not None)
            act = menu.exec(e.globalPosition().toPoint())
            if act == a_split:
                self.splitRequested.emit(self.playhead)
            elif act == a_copy:
                self.copyRequested.emit()
            elif act == a_paste:
                self.pasteRequested.emit()
            elif act == a_del:
                self.deleteRequested.emit()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if y < self.RULER_H and x >= self.HW:
            self.mode = "scrub"
            self.seekRequested.emit(self._clamp_t(self.xt(x)))
            return
        idx, edge = self.hit(pos)
        if self.tool == "razor":
            if idx is not None:
                self.splitRequested.emit(self.xt(x))
            return
        if idx is None:
            self.sel = -1
            self.update()
            return
        self.sel = idx
        seg = self.seq.segs[idx]
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
            self._grab = x - self.tx(self.seq.starts()[idx])
            self._mx = x                                     # clip "lifts" and follows the mouse while held
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        x = pos.x()
        self.hover_x = x if self.tool == "razor" else None
        if self.mode == "scrub":
            self.seekRequested.emit(self._clamp_t(self.xt(x)))
        elif self.mode in ("trim_in", "trim_out"):
            self._trim(x)
        elif self.mode == "move":
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
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.tool == "razor":
            return
        idx, _ = self.hit(e.position())
        if idx is not None:
            self.sel, self.mode = idx, None
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
    def _move(self, x):
        segs, i = self.seq.segs, self.sel
        s = segs[i]
        c = self.xt(x - self._grab) + s.dur / 2
        acc, new_i = 0.0, 0
        for o in segs[:i] + segs[i + 1:]:
            if acc + o.dur / 2 < c:
                new_i += 1
            acc += o.dur
        if new_i != i:
            segs.insert(new_i, segs.pop(i))
            self.sel = new_i
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
        p.restore()
        return rx

    def _paint_clips(self, p):
        starts = self.seq.starts()
        fm = p.fontMetrics()
        lifted = None
        for i, s in enumerate(self.seq.segs):
            x0, x1 = self.tx(starts[i]), self.tx(starts[i] + s.dur)
            if x1 < self.HW or x0 > self.width():
                continue
            r = QRectF(x0 + 0.5, self.V_Y + 0.5, max(1.0, x1 - x0 - 1.5), self.V_H - 1)
            if i == self.sel and self.mode == "move" and getattr(self, "_mx", None) is not None:
                lifted = (s, r)                              # its slot is drawn as a faint ghost
                p.setOpacity(0.3)
                self._draw_clip(p, fm, s, r, False)
                p.setOpacity(1.0)
                continue
            self._draw_clip(p, fm, s, r, i == self.sel)
        if lifted:                                           # the clip itself hovers under the mouse
            s, r = lifted
            fr = QRectF(self._mx - self._grab + 0.5, r.y() - 7, r.width(), r.height())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRoundedRect(fr.translated(4, 9), 4, 4)
            p.setOpacity(0.93)
            self._draw_clip(p, fm, s, fr, True)
            p.setOpacity(1.0)

    # [MAP] One clip: label band, filmstrip (a tile every TILE_W px, each sampled from THIS clip's own in..out range),
    # badges, file name, and a white outline when selected. Uses QPainter clip paths - restore() calls must stay
    # balanced with save().
    def _draw_clip(self, p, fm, s, r, sel):
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
        if sel:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 3, 3)


# ----------------------------------------------------------------------------- playback engine
# [MAP] QMediaPlayer cannot play backwards, so clips with Reverse ON are previewed from a small reversed COPY
# ("proxy") rendered in the background by ffmpeg: span [in, out] only, <= 540 px tall, <= 30 fps, -g 12 (frequent
# keyframes so seeking is cheap), libx264 ultrafast crf 23, audio reversed AAC. Only clips with src_dur <= 30 s
# get one (MAX_SEC); longer clips preview FORWARD but still export reversed. One render at a time (thread + queue).
# The proxy is ORIENTATION-only: speed, mute, mirror, rotation and crop are NOT baked in - the preview applies
# them live as usual. Until a proxy is ready the clip previews forward; MainWindow.on_proxy_ready then re-seeks
# if the playhead is inside that clip.
# Identity: key = (media.path, round(in_s,3), round(out_s,3)). `files` (key -> path) is the SAME dict object as
# Engine.proxies (assigned in MainWindow.__init__) - never rebind either attribute.
# _known = keys queued/running/done/failed: a failed key is never retried until forgotten (prune/cancel_media).
# [INVARIANT] cancel_media(path) must be called next to every worker.cancel (remove, rename, clear, Save-Over):
# it kills a running render for that file (releasing the source handle) and deletes its proxies.
# Lifecycle: created in MainWindow.__init__, atexit + closeEvent call shutdown() (kills ffmpeg, deletes the temp
# folder "quickcut_rev_*").
# [PITFALL] `ready` is emitted from the worker thread; do not touch widgets from _run.
class ReverseProxy(QObject):
    """Background renderer of small reversed copies ("proxies") of clips that have Reverse on, so the
    preview can play them backwards (QMediaPlayer can't). One job at a time, per-clip cap MAX_SEC.
    Proxy = span [in,out] of the source, <=540 px tall, <=30 fps, video+audio reversed. Speed, mute,
    mirror, rotation and crop are NOT baked in - the preview applies those live as usual."""
    ready = Signal()
    MAX_SEC = 30.0

    def __init__(self):
        super().__init__()
        self.files = {}         # key -> finished proxy path (read by Engine)
        self._known = set()     # keys queued / running / done / failed (never retried)
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._cur = None        # (key, media path) being rendered
        self._dir = None
        self._n = 0
        threading.Thread(target=self._run, daemon=True).start()
        import atexit
        atexit.register(self.shutdown)

    @staticmethod
    def key(seg):
        return (seg.media.path, round(seg.in_s, 3), round(seg.out_s, 3))

    def eligible(self, seg):
        return bool(FFMPEG and seg.rev and seg.media.has_video and 0 < seg.src_dur <= self.MAX_SEC + 1e-6)

    def pending(self, seg):
        return self.eligible(seg) and self.key(seg) not in self.files

    def request(self, seg):
        if not self.eligible(seg):
            return
        k = self.key(seg)
        with self._lock:
            if k in self._known:
                return
            self._known.add(k)
        self._q.put((k, seg.media.path, seg.in_s, seg.out_s, seg.media.fps, bool(seg.media.acodec.strip())))

    # [MAP] Deletes proxies no clip references any more, but NEVER the one the player currently has open (in_use =
    # Engine.path) - deleting an open file would break playback on Windows. Called from MainWindow.ensure_proxies.
    def prune(self, keep, in_use=None):
        """Delete proxies no clip needs any more (never the one the player has open)."""
        with self._lock:
            for k in [k for k in self.files if k not in keep and self.files[k] != in_use]:
                p = self.files.pop(k)
                self._known.discard(k)
                try:
                    os.remove(p)
                except OSError:
                    pass

    def cancel_media(self, path):
        """Stop/forget everything for a media file (call before rename / save-over / remove)."""
        with self._lock:
            for k in [k for k in self._known if k[0] == path]:
                self._known.discard(k)
                p = self.files.pop(k, None)
                if p:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            proc = self._proc if (self._cur and self._cur[1] == path) else None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _run(self):
        while True:
            k, path, a, b, fps, has_a = self._q.get()
            with self._lock:
                if k not in self._known or k in self.files:
                    continue
                self._cur = (k, path)
            out = None
            try:
                if self._dir is None:
                    self._dir = tempfile.mkdtemp(prefix="quickcut_rev_")
                self._n += 1
                out = os.path.join(self._dir, f"rev{self._n:04d}.mp4")
                vf = "scale=-2:'min(540,ih)'" + (",fps=30" if fps > 30.5 else "") + ",reverse"
                cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                       "-i", path, "-map", "0:v:0"]
                if has_a:
                    cmd += ["-map", "0:a:0?", "-af", "areverse"]
                cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-g", "12",
                        "-pix_fmt", "yuv420p"]
                if has_a:
                    cmd += ["-c:a", "aac", "-b:a", "128k"]
                cmd.append(out)
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=NOWIN)
                rc = self._proc.wait()
                with self._lock:
                    ok = rc == 0 and k in self._known and os.path.isfile(out)
                    if ok:
                        self.files[k] = out
                if ok:
                    self.ready.emit()
                else:
                    try:
                        os.remove(out)
                    except OSError:
                        pass
            except Exception:
                pass
            finally:
                self._proc = None
                self._cur = None

    def shutdown(self):
        try:
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)


# [MAP][DO NOT BREAK] The ONLY video playback path: one QMediaPlayer that hops between the files of the clips on
# the timeline. The UI never touches the QMediaPlayer directly (MainWindow.rename_media / clear_project /
# save_over / closeEvent are the few deliberate exceptions, all to RELEASE a file lock).
#
# Each numbered item below fixes a bug that was reproduced by a user. Keep the equivalences when editing.
#   1  _go: player.stop() BEFORE setSource() when switching files (else the video sink freezes/keeps a stale frame)
#   2  `playing` (what the player really does; drives the play/pause icon) and `_want_playing` (what the user
#      asked for; drives auto-advance into the next clip) are separate on purpose. EndOfMedia checks _want_playing.
#   3  _on_status has a stray-status guard: ignore Loaded/Buffered events whose player.source() no longer equals
#      the file we are waiting for (else the pending seek+play is consumed by the previous file's event and
#      playback sticks on one frame). QUrl objects are compared, not strings; "cannot tell" counts as mismatch.
#   4  Screenshot is on demand (MainWindow.take_screenshot -> videoSink().videoFrame()); there is NO permanent
#      per-frame sink subscription. The only frame hook is the drag-only scrub-timing connection (see 6).
#   5  Output is the VideoView's QGraphicsVideoItem (self._sink = video_widget.videoSink()).
#   6  Scrub is ADAPTIVE (see scrub()). seek() must keep `self._scrub_t = None`. Never go back to one seek per
#      mouse event: flooding QMediaPlayer keeps cancelling seeks before their frame is decoded.
#   7  Performance detection (perfChanged, _monitor, _flag_perf_bad), MainWindow.check_system RAM/disk warnings and
#      quiet_ffmpeg_log(). GPU decoding is left to Qt - do not force it.
#   8  Reverse-preview helpers proxy_path(seg) / _target(seg, off) -> (file, pos) / _span(seg) -> (start, end) of the
#      file CURRENTLY LOADED. For non-reversed clips they are exactly the original expressions
#      (in_s + min(off, dur-0.001)*speed, and in_s..out_s). seek, _tick, _advance and the EndOfMedia check all go
#      through them. _advance only skips a reload when contiguous AND no proxy AND self.path == cur.media.path.
#      _scrub_send's dedupe key intentionally still uses the SOURCE path/frame.
#
# STATE GLOSSARY
#   path            file currently loaded in the player (a media file OR a reverse proxy)
#   idx             index of the clip the player is in
#   playhead        timeline seconds; the value the UI shows
#   _pending        (ms, play) to apply when the newly set source finishes loading; None when nothing is pending
#   _settle         monotonic deadline (150 ms) during which _tick ignores the player position after a jump
#   _last_pos       last known player position in FILE seconds (for the EndOfMedia end-of-clip test)
#   _scrub_*        scrub state machine (see scrub()); _perf_* / _pm_*  performance monitor
# FLOW  seek(t) -> locate clip -> _target() -> _go(file, pos, play) -> [setSource -> _on_status(Loaded) applies
#       _pending] or [setPosition/play] -> _apply_fx() (mute + playback speed) -> emits playheadChanged
#       playing: a 15 ms QTimer calls _tick -> reads player.position -> computes playhead -> _advance at clip end.
class Engine(QObject):
    """Plays the sequence: one QMediaPlayer hopping between clips. This is the ONLY
    video playback in the app - it always shows what's on the timeline."""
    playheadChanged = Signal(float)
    playStateChanged = Signal(bool)
    error = Signal(str)

    perfChanged = Signal(bool)          # True while the preview can't keep up

    SCRUB_MIN_GAP = 0.012               # s, floor between real seeks while dragging
    SCRUB_FIXED_MS = 35                 # fallback fixed throttle (only if frame signal is unusable)
    SCRUB_IDLE_MS = 600                 # scrub session ends this long after the last mouse move
    SLOW_SEEK = 0.22                    # s, seek->frame latency counted as "can't keep up"

    def __init__(self, seq, video_widget):
        super().__init__()
        self.seq = seq
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(getattr(video_widget, "item", video_widget))
        self.path = None
        self.idx = 0
        self.proxies = {}           # reverse-preview proxies (shared dict from ReverseProxy.files)
        self.playing = False        # what the player is really doing right now (drives the icon)
        self._want_playing = False  # what the user asked for (drives auto-advance across clips)
        self.playhead = 0.0
        self._pending = None
        self._last_pos = 0.0
        self._settle = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(15)
        self.timer.timeout.connect(self._tick)
        # --- scrub state (adaptive: next seek is sent as soon as the previous one showed a frame)
        self._sink = video_widget.videoSink()
        self._scrub_t = None            # newest scrub target not yet sent to the player
        self._scrub_active = False      # inside a drag (frame-signal connected only while True)
        self._scrub_inflight = False    # a seek was sent and its frame hasn't shown up yet
        self._scrub_sent = 0.0
        self._scrub_valid = True        # False when that seek switched files (legitimately slow)
        self._scrub_ema = 0.06          # smoothed seek->frame latency, seconds
        self._scrub_key = None          # (file, source frame) last sent - skip re-sending it
        self._scrub_frames = 0
        self._scrub_misses = 0
        self._scrub_fixed = False       # fallback: fixed-rate throttle if frame signal never fires
        self._scrub_slow = 0
        self._pump_pending = False
        self._scrub_watch = QTimer(self)
        self._scrub_watch.setSingleShot(True)
        self._scrub_watch.timeout.connect(self._scrub_timeout)
        self._scrub_idle = QTimer(self)
        self._scrub_idle.setSingleShot(True)
        self._scrub_idle.setInterval(self.SCRUB_IDLE_MS)
        self._scrub_idle.timeout.connect(self._scrub_end)
        # --- "can't keep up" detection
        self._perf_bad = False
        self._perf_clear = QTimer(self)
        self._perf_clear.setSingleShot(True)
        self._perf_clear.setInterval(6000)
        self._perf_clear.timeout.connect(self._perf_clear_cb)
        self._pm = None                 # playback monitor window: (t0, pos0, clip idx)
        self._pm_bad = 0
        self._pm_lag = 0
        self._last_tick = None
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.playbackStateChanged.connect(self._on_playback_state)

    # [MAP] Applies the current clip's PREVIEW options: mute (audio.setMuted) and playback speed (playbackRate).
    # Only mute and speed are handled here; mirror/rotation are handled by VideoStage/VideoView, reverse by proxies.
    # [KNOWN ISSUE F6] Rate is clamped to 0.05..20 while the clip dialog allows 0.05..100, so a 50x clip previews at
    # 20x while _tick divides the source position by 50: the playhead crawls and _monitor falsely reports
    # "can't keep up" (its expected rate is speed-relative).
    def _apply_fx(self):
        """Per-clip mute + playback speed for the clip now at self.idx (additive; no original line changed)."""
        segs = self.seq.segs
        if not segs:
            return
        sg = segs[min(self.idx, len(segs) - 1)]
        self.audio.setMuted(bool(sg.mute))
        r = max(0.05, min(float(sg.speed), 20.0))
        if abs(self.player.playbackRate() - r) > 1e-6:
            self.player.setPlaybackRate(r)

    # [MAP] Finished reverse proxy for this clip, else None. None means "play the source forward" (Reverse ON but not
    # rendered yet, or clip longer than 30 s). Always check the file still exists.
    def proxy_path(self, seg):
        """Finished reversed proxy for this clip, else None (clip then previews forward)."""
        if not seg.rev:
            return None
        p = self.proxies.get(ReverseProxy.key(seg))
        return p if p and os.path.isfile(p) else None

    # [INVARIANT][DO NOT BREAK #8] (file, position-in-that-file) for a timeline offset inside a clip. Position is
    # clamped to dur-0.001 (never seek exactly to the end: EndOfMedia would fire immediately) and multiplied by speed.
    # Non-reversed / no proxy: (source file, in_s + o). Reversed with proxy: (proxy file, o) - the proxy plays FORWARD
    # from 0 and already contains the reversed frames.
    def _target(self, seg, off):
        """(file, position in that file) for timeline offset `off` inside seg. Non-reversed clips: exactly
        the source file at in_s + off*speed. Reversed clip with a proxy: proxy plays forward from 0."""
        o = min(off, max(0.0, seg.dur - 0.001)) * seg.speed
        px = self.proxy_path(seg)
        return (px, o) if px else (seg.media.path, seg.in_s + o)

    # [INVARIANT][DO NOT BREAK #8] (start, end) of the clip in the units of the file that is loaded RIGHT NOW: (0,
    #   src_dur)
    # when the proxy is what the player has open, else (in_s, out_s). _tick and the EndOfMedia test use it.
    def _span(self, seg):
        """(start, end) of seg in the units of the file the player currently has loaded."""
        px = self.proxy_path(seg)
        return (0.0, seg.src_dur) if (px and self.path == px) else (seg.in_s, seg.out_s)

    def _on_error(self, err, msg):
        self._pending = None
        self.error.emit(msg or "Playback error")

    # [MAP] Makes `playing` (and therefore the play/pause icon and the tick timer) truthful to the QMediaPlayer's
    # real state instead of assuming that our last play()/pause() took effect. This fixed the button staying on
    # "pause" after a clip switch.
    def _on_playback_state(self, state):
        """Keep our own 'playing' flag (and the UI's play/pause icon) truthful to what the
        player is actually doing, instead of just assuming our last play()/pause() call took
        effect - this is what was leaving the button stuck on 'pause' after a clip switch."""
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        if playing != self.playing:
            self.playing = playing
            self.timer.start() if playing else self.timer.stop()
            self.playStateChanged.emit(playing)

    # [DO NOT BREAK #1] Switching files: set self.path, stash (ms, play) in _pending, player.stop(), THEN setSource().
    # The pending seek/play is applied later by _on_status when the new file reports Loaded/Buffered. Same file while a
    # load is still pending: just update _pending. Same file, nothing pending: direct setPosition + play/pause.
    # _settle (150 ms) makes _tick ignore position reports that still belong to the old position.
    def _go(self, path, src_t, play):
        ms = int(max(0.0, src_t) * 1000)
        self._settle = time.monotonic() + 0.15
        self._last_pos = src_t
        if path != self.path:
            self.path = path
            self._pending = (ms, play)
            # Stopping before switching sources avoids a Qt Multimedia glitch where the video
            # sink can be left showing a black/stale frame after setSource() while mid-playback.
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(path))
        elif self._pending is not None:
            self._pending = (ms, play)
        else:
            self.player.setPosition(ms)
            self.player.play() if play else self.player.pause()

    # [DO NOT BREAK #2/#3] Two jobs. (a) Loaded/Buffered + _pending set -> after the stray-status guard, apply fx,
    # setPosition, play/pause. (b) EndOfMedia -> if the USER wanted playback (_want_playing) and nothing is pending
    # and we are within 0.3 s of the clip's end in the loaded file's units (_span) -> _advance().
    # Changing the guard's comparison or the _want_playing test brings back the "stuck on one frame" bug.
    def _on_status(self, st):
        S = QMediaPlayer.MediaStatus
        if st in (S.LoadedMedia, S.BufferedMedia) and self._pending is not None:
            # Guard against a stray status event that's still about the PREVIOUS source
            # arriving after we've already moved on to a new one - consuming it here would
            # eat the pending seek+play meant for the new file, leaving playback stuck.
            # Compare QUrl objects (not strings) so path-normalization differences can't
            # cause a false mismatch, and treat "can't tell" (blank/transitional source) as
            # a mismatch too - letting it through was consuming the pending seek+play before
            # the real new file had loaded, leaving playback permanently stuck on one frame.
            if self.path is not None and self.player.source() != QUrl.fromLocalFile(self.path):
                return
            ms, play = self._pending
            self._pending = None
            self._settle = time.monotonic() + 0.15
            self._apply_fx()
            self.player.setPosition(ms)
            self.player.play() if play else self.player.pause()
        elif st == S.EndOfMedia:
            # Use _want_playing (user intent), not self.playing: a clip briefly pausing
            # itself (or our own explicit stop() before switching sources) must not cancel
            # the intent to keep playing into the next clip.
            if self._want_playing and self._pending is None and self.seq.segs:
                seg = self.seq.segs[min(self.idx, len(self.seq.segs) - 1)]
                if self._last_pos >= self._span(seg)[1] - 0.3:
                    self._advance()

    # [MAP] The universal jump: timeline time -> clip -> _target -> _go, then fx, flags, timer, signals. Default play=None
    # means "keep the user's intent". Empty timeline: stop, clear the source, emit 0.
    # [DO NOT BREAK #6] Must keep `self._scrub_t = None` (a real seek supersedes an unsent scrub target) and resets the
    # performance-monitor window. Called from: playback controls, on_seq_edited (after EVERY edit), scrub (via
    # _scrub_send), rename/Save-Over recovery.
    def seek(self, t, play=None):
        if play is None:
            play = self._want_playing
        t = max(0.0, min(t, self.seq.total()))
        self._scrub_t = None            # a real seek supersedes any not-yet-sent scrub target
        self._pm, self._pm_bad, self._last_tick = None, 0, None
        self.playhead = t
        if not self.seq.segs:
            self.playing = False
            self._want_playing = False
            self.timer.stop()
            self.player.stop()
            self.player.setSource(QUrl())
            self.path, self._pending = None, None
            self.playheadChanged.emit(0.0)
            self.playStateChanged.emit(False)
            return
        idx, off = self.seq.locate(t)
        seg = self.seq.segs[idx]
        self.idx = idx
        self._go(*self._target(seg, off), play)
        self._apply_fx()
        self.playing = play
        self._want_playing = play
        self.timer.start() if play else self.timer.stop()
        self.playheadChanged.emit(t)
        self.playStateChanged.emit(play)

    # [DO NOT BREAK #6] Playhead dragged with the mouse. The playhead LINE follows at full mouse rate, but real seeks
    # are ADAPTIVE: a new seek is sent only after the previous one has put a frame on screen (or a watchdog fired),
    # newest target wins, so the preview refreshes as fast as the decoder allows and skips frames only when it cannot.
    # State machine:  scrub() -> _scrub_pump() -> _scrub_send() [seek(play=False), inflight=True, start watchdog]
    #    -> _on_scrub_frame() [inflight=False, measure latency -> EMA, pump again]
    #    or _scrub_timeout() [watchdog: stop waiting; after 3 misses with zero frames seen -> fixed 35 ms throttle]
    #    -> _scrub_end() [600 ms after the last mouse move: disconnect the frame signal].
    # The sink's videoFrameChanged is connected ONLY while scrubbing and used ONLY for timing (no frame conversion).
    def scrub(self, t):
        """Playhead dragged with the mouse. The playhead line/timecode follow at full mouse rate.
        Real seeks are ADAPTIVE: a new one is sent as soon as the previous one has put a frame on
        screen, so the preview refreshes as fast as the decoder (GPU or CPU) can deliver and frames
        are only skipped when it can't keep up - the newest target always wins. Flooding
        QMediaPlayer with a seek per mouse event instead keeps cancelling the seek before its
        frame is decoded. (The sink's frame signal is connected ONLY during a drag and only used
        for timing - no frame conversion - so it stays clear of the per-frame issue in the notes.)"""
        t = max(0.0, min(t, self.seq.total()))
        self._scrub_t = t
        self.playhead = t
        self.playheadChanged.emit(t)
        if not self._scrub_active:
            self._scrub_active = True
            self._scrub_key = None
            if not self._scrub_fixed:
                self._sink.videoFrameChanged.connect(self._on_scrub_frame)
        self._scrub_idle.start()
        self._scrub_pump()

    def _scrub_pump(self):
        if self._scrub_inflight or self._scrub_t is None:
            return
        wait = self.SCRUB_MIN_GAP - (time.monotonic() - self._scrub_sent)
        if wait > 0:
            if not self._pump_pending:
                self._pump_pending = True
                QTimer.singleShot(int(wait * 1000) + 1, self._scrub_pump_later)
            return
        self._scrub_send()

    def _scrub_pump_later(self):
        self._pump_pending = False
        self._scrub_pump()

    # [MAP] Sends the newest target. The dedupe key is (SOURCE path, source frame number) - intentionally not the proxy
    # path - so dragging within one source frame doesn't re-seek. `_scrub_valid` is False when the seek switched files
    # (legitimately slow), so those latencies are not fed into the EMA or the performance detector.
    def _scrub_send(self):
        t, self._scrub_t = self._scrub_t, None
        if t is None or not self.seq.segs:
            return
        idx, off = self.seq.locate(t)
        seg = self.seq.segs[idx]
        key = (seg.media.path, round((seg.in_s + off * seg.speed) * max(1.0, seg.media.fps)))
        if key == self._scrub_key:
            return                                    # that source frame is already on screen
        self._scrub_key = key
        before = self.path
        self.seek(t, play=False)
        self._scrub_valid = (self.path == before)     # a file switch is legitimately slow
        self._scrub_inflight = True
        self._scrub_sent = time.monotonic()
        if self._scrub_fixed:
            wd = self.SCRUB_FIXED_MS / 1000.0
        elif self._scrub_frames == 0:
            wd = 0.6                                  # grace until we've seen a frame signal work at all
        else:
            wd = min(0.6, max(0.12, self._scrub_ema * 3))
        self._scrub_watch.start(int(wd * 1000))

    def _on_scrub_frame(self, *_):
        self._scrub_frames += 1
        if not self._scrub_inflight:
            return
        lat = time.monotonic() - self._scrub_sent
        self._scrub_inflight = False
        self._scrub_watch.stop()
        self._scrub_misses = 0
        if self._scrub_valid:
            self._scrub_ema = 0.7 * self._scrub_ema + 0.3 * lat
            self._scrub_note(lat > self.SLOW_SEEK)
        QTimer.singleShot(0, self._scrub_pump)

    def _scrub_timeout(self):
        """No frame arrived in time: stop waiting so scrubbing can't stall."""
        self._scrub_inflight = False
        if not self._scrub_fixed:
            if self._scrub_frames == 0:
                self._scrub_misses += 1
                if self._scrub_misses >= 3:           # frame signal isn't usable on this backend
                    self._scrub_fixed = True
                    try:
                        self._sink.videoFrameChanged.disconnect(self._on_scrub_frame)
                    except Exception:
                        pass
            else:
                self._scrub_ema = min(0.5, self._scrub_ema * 1.5)
                if self._scrub_valid:
                    self._scrub_note(True)
        self._scrub_pump()

    def _scrub_end(self):
        if self._scrub_inflight or self._scrub_t is not None:
            self._scrub_idle.start()                  # still delivering the last target
            return
        self._scrub_active = False
        self._scrub_slow = 0
        try:
            self._sink.videoFrameChanged.disconnect(self._on_scrub_frame)
        except Exception:
            pass

    def _scrub_note(self, slow):
        self._scrub_slow = self._scrub_slow + 1 if slow else max(0, self._scrub_slow - 1)
        if self._scrub_slow >= 3:
            self._scrub_slow = 0
            self._flag_perf_bad()

    def _flag_perf_bad(self):
        if not self._perf_bad:
            self._perf_bad = True
            self.perfChanged.emit(True)
        self._perf_clear.start()

    def _perf_clear_cb(self):
        self._perf_bad = False
        self.perfChanged.emit(False)

    # [MAP] Playback health check used by _tick: player position should advance ~1 s of source time per real second
    # x speed. Two consecutive bad 1-second windows (lagging position, or the UI thread starved > 120 ms three times)
    # raise the "GPU/CPU can't keep up" warning via perfChanged (auto-clears after 6 s).
    def _monitor(self, now, pos):
        """Playback health: video position should advance ~1s per second. Two bad 1-second
        windows in a row (position lagging, or the UI thread stalling) = can't keep up."""
        if self._pm is None or self._pm[2] != self.idx or pos < self._pm[1] - 0.05:
            self._pm, self._pm_lag = (now, pos, self.idx), 0
            return
        t0, p0, _ = self._pm
        if now - t0 >= 1.0:
            spd = self.seq.segs[self.idx].speed if self.idx < len(self.seq.segs) else 1.0
            bad = (pos - p0) / (now - t0) / (spd or 1.0) < 0.8 or self._pm_lag >= 3
            self._pm_bad = self._pm_bad + 1 if bad else 0
            if self._pm_bad >= 2:
                self._pm_bad = 0
                self._flag_perf_bad()
            self._pm, self._pm_lag = (now, pos, self.idx), 0

    # [MAP] Restarts from 0 if the playhead is at (or within 30 ms of) the end, otherwise resumes from the playhead.
    def play(self):
        if not self.seq.segs:
            return
        t = self.playhead
        if t >= self.seq.total() - 0.03:
            t = 0.0
        self.seek(t, play=True)

    # [DO NOT BREAK #2] Clears `_want_playing` first (user intent), then pauses the player only if it is really playing.
    # Called before every operation that must not race with playback (export, rename, remove, Save-Over, tools...).
    def pause(self):
        self._want_playing = False
        if self.playing:
            self.playing = False
            self.timer.stop()
            self.player.pause()
            self.playStateChanged.emit(False)

    # [MAP] 15 ms timer while playing. Skips while a load is pending or inside the 150 ms _settle window. Reads the
    # player position (file seconds), runs the perf monitor, and either advances at the clip's end (pos >= end - 10 ms
    # in the LOADED file's units) or converts position -> timeline time: start_of_clip + (pos - base) / speed.
    # Emits playheadChanged (=> MainWindow.on_playhead => refresh_stage, timeline follow, timecode).
    def _tick(self):
        now = time.monotonic()
        if self._last_tick is not None and now - self._last_tick > 0.12:
            self._pm_lag += 1                       # the UI thread was starved for >120 ms
        self._last_tick = now
        if (not self.playing or self._pending is not None or not self.seq.segs
                or time.monotonic() < self._settle):
            return
        segs = self.seq.segs
        self.idx = min(self.idx, len(segs) - 1)
        seg = segs[self.idx]
        pos = self.player.position() / 1000.0
        self._last_pos = pos
        self._monitor(now, pos)
        base, end = self._span(seg)
        if pos >= end - 0.01:
            self._advance()
            return
        self.playhead = self.seq.starts()[self.idx] + max(0.0, min(seg.dur, (pos - base) / (seg.speed or 1.0)))
        self.playheadChanged.emit(self.playhead)

    # [DO NOT BREAK #8] Move to the next clip. End of timeline -> stop and park at the end. Otherwise apply the next
    # clip's fx and either (a) keep playing WITHOUT reloading - only when the next clip is a contiguous slice of the
    # same source file (< 50 ms gap) AND has no reverse proxy AND the player has that very file loaded - or (b) _go()
    # to the target file/position. Condition (a) is what gives gapless playback across a split point; the
    # `self.path == cur.media.path` term prevents "continuing" when the current file is actually a proxy.
    def _advance(self):
        segs = self.seq.segs
        cur = segs[self.idx]
        nxt = self.idx + 1
        if nxt >= len(segs):
            self.playing = False
            self._want_playing = False
            self.timer.stop()
            self.player.pause()
            self.playhead = self.seq.total()
            self.playheadChanged.emit(self.playhead)
            self.playStateChanged.emit(False)
            return
        n = segs[nxt]
        self.idx = nxt
        self._apply_fx()
        if (n.media.path == cur.media.path and abs(n.in_s - cur.out_s) < 0.05
                and self.proxy_path(n) is None and self.path == cur.media.path):
            return                                    # contiguous: just keep playing
        self._go(*self._target(n, 0.0), True)


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
        self.play_btn = self.tool_btn("play", "Play / Pause (Space)", self.toggle_play)
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
        p.body_lay.addWidget(self.tl, 1)
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
        sc("Space", self.toggle_play)
        sc("V", lambda: (self.b_sel.setChecked(True), self.set_tool("select")))
        sc("C", lambda: (self.b_razor.setChecked(True), self.set_tool("razor")))
        sc("X", lambda: (self.b_crop.setChecked(True), self.set_tool("crop")))
        sc("R", lambda: (self.b_resize.setChecked(True), self.set_tool("resize")))
        sc("Alt+Left", lambda: self.move_clip(-1))
        sc("Alt+Right", lambda: self.move_clip(1))
        sc("Left", lambda: self.step(-1))
        sc("Right", lambda: self.step(1))
        sc("Shift+Left", lambda: self.step(-5))
        sc("Shift+Right", lambda: self.step(5))
        sc("Up", lambda: self.goto_edit(-1))
        sc("Down", lambda: self.goto_edit(1))
        sc("Home", lambda: self.engine.seek(0.0, play=False))
        sc("End", lambda: self.engine.seek(self.seq.total(), play=False))

    def show_help(self):
        QMessageBox.information(self, "Keyboard shortcuts", (
            "Space  Play / pause\n"
            "Left / Right  Step one frame  (Shift = 5 frames)\n"
            "Up / Down  Previous / next edit\n"
            "Home / End  Go to start / end\n"
            "V  Selection tool     C  Razor / Cut tool\n"
            "X  Crop tool     R  Resize tool  (drag corners/sides; right-click the overlay to reset)\n"
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
        for p in paths:
            p = os.path.abspath(p)
            if p in self.by_path:
                out.append(self.by_path[p])
                continue
            if not os.path.isfile(p):
                continue
            m = probe_media(p)
            if not m:
                self.statusBar().showMessage(f"Could not read: {os.path.basename(p)}", 6000)
                continue
            self.medias.append(m)
            self.by_path[p] = m
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, p)
            row = ProjectRow(m)
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
        menu = QMenu(self)
        a_ins = menu.addAction("Insert into Timeline at Playhead")
        a_star = menu.addAction("Set as Primary (Save-Over target)")
        a_ren = menu.addAction("Rename File...\tF2")
        menu.addSeparator()
        a_open = menu.addAction("Open File")
        a_loc = menu.addAction("Open File Location")
        menu.addSeparator()
        a_rm = menu.addAction("Remove from Project")
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
            self.request_remove_media(m, confirm=False)   # explicit menu action - no extra confirm

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
            del self.by_path[m.path]
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
        del self.by_path[old_path]
        m.path = new_path
        m.name = os.path.basename(new_path)
        self.by_path[new_path] = m
        self.items[m].setData(Qt.ItemDataRole.UserRole, new_path)
        self.tl.clear_thumbs_for(old_path)
        row = self.rows.get(m)
        if row:
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
    def toggle_play(self):
        self.engine.pause() if self.engine.playing else self.engine.play()

    def step(self, n):
        self.engine.seek(self.engine.playhead + n / self.seq.fps(), play=False)

    def goto_edit(self, d):
        bounds = self.seq.starts() + [self.seq.total()]
        t = self.engine.playhead
        if d < 0:
            c = [b for b in bounds if b < t - 0.02]
            self.engine.seek(max(c) if c else 0.0, play=False)
        else:
            c = [b for b in bounds if b > t + 0.02]
            self.engine.seek(min(c) if c else self.seq.total(), play=False)

    def on_timeline_seek(self, t):
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
        if tool == "crop" and self.stage.picture_rect().united(self.stage.canvas_rect()) != QRectF(self.stage.canvas_rect()):
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
                                seg.mirror, seg.media.has_video, seg.rev)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        mute, speed, mirror, rev = dlg.values()
        if mute == seg.mute and abs(speed - seg.speed) < 1e-9 and mirror == seg.mirror and rev == seg.rev:
            return

        def do():
            seg.mute, seg.speed, seg.mirror, seg.rev = mute, speed, mirror, rev
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
    #   crop   -> (sel_w, sel_h, px - sel_x, py - sel_y, pw, ph, color)   canvas = selection, picture shifts
    #   resize -> (cw, ch, sel_x, sel_y, sel_w, sel_h, color)              canvas unchanged, picture = selection
    # Both pass norm_xf (even sizes). An unchanged result only re-lays-out; a result equal to the identity transform is
    # stored as None (so the clip returns to the lossless fast path). The maths is plain arithmetic, so growing the
    # canvas back out ("un-crop") works without special cases (v4). `color` is the blank/background swatch chosen
    # in VideoStage's bar; it rides along in xf's 7th slot (see xf_color/norm_xf/xf_filter).
    def on_stage_commit(self, tool, sel, color):
        seg = self.current_seg()
        if seg is None or not seg.media.w or self.stage.scale() <= 0:
            return
        W, H = seg.media.w, seg.media.h
        old = norm_xf(seg.xf or (W, H, 0, 0, W, H, DEFAULT_BG))
        cw, ch, px, py, pw, ph, _old_color = old
        rc, k = self.stage.canvas_rect(), self.stage.scale()
        rx, ry = (sel.x() - rc.x()) / k, (sel.y() - rc.y()) / k
        rw, rh = sel.width() / k, sel.height() / k
        new = norm_xf((rw, rh, px - rx, py - ry, pw, ph, color) if tool == "crop"
                       else (cw, ch, rx, ry, rw, rh, color))
        if new == old:
            self.stage.relayout()                     # snap the overlay/preview back
            return
        ident = (W, H, 0, 0, W, H, DEFAULT_BG)
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

    def delete_selected(self):
        i = self.tl.sel
        if i >= 0 and self.seq.edit(lambda: self.seq.delete(i)) is not False:
            self.tl.sel = -1
            self.tl.update()

    # [COUPLING] clipboard_seg is a positional 9-tuple copy of the Seg (media, in_s, out_s, xf, mute, speed, mirror,
    # rot, rev) - same order as Seg.__init__ / snapshot. paste_clip unpacks it positionally.
    def copy_selected(self):
        i = self.tl.sel
        if not (0 <= i < len(self.seq.segs)):
            self.statusBar().showMessage("Select a clip on the timeline first (click it), then Ctrl+C.", 4000)
            return
        s = self.seq.segs[i]
        self.clipboard_seg = (s.media, s.in_s, s.out_s, s.xf, s.mute, s.speed, s.mirror, s.rot, s.rev)
        self.statusBar().showMessage(f"Copied {s.media.name} ({fmt_tc(s.dur, self.seq.fps())})", 3000)

    def paste_clip(self):
        if not self.clipboard_seg:
            self.statusBar().showMessage("Nothing to paste - select a clip and press Ctrl+C first.", 4000)
            return
        media, a, b, xf, mute, speed, mirror, rot, rev = self.clipboard_seg
        t = self.engine.playhead
        was_empty = not self.seq.segs
        result = {}

        def do():
            seg = Seg(media, a, b, xf, mute, speed, mirror, rot, rev)
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
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(QSS)
    win = MainWindow([a for a in sys.argv[1:] if os.path.isfile(a)])
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
