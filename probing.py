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
# [MAP] Every "Stream #0:N(lang): Audio: ..." line from `ffmpeg -i`'s stderr, in source order, with its
# "title" metadata tag if one follows it (before the next Stream line). Used to populate Media.audio_streams
# so Clip Options can list real track names ("Audio Track 1" etc. when no title tag is present).
def _probe_audio_streams(txt):
    lines = txt.splitlines()
    streams = []
    n = len(lines)
    for i, line in enumerate(lines):
        m = re.search(r"Stream #\d+:\d+(?:\((\w+)\))?:\s*Audio:", line)
        if not m:
            continue
        lang, name = m.group(1), None
        j = i + 1
        while j < n and not re.search(r"Stream #\d+:\d+", lines[j]):
            tm = re.search(r"^\s*title\s*:\s*(.+?)\s*$", lines[j])
            if tm:
                name = tm.group(1)
                break
            j += 1
        streams.append({"lang": lang, "name": name})
    return streams


def probe_media(path):
    if not FFMPEG:
        return None
    # [FIX CRITICAL] A freshly-written file (just baked from an image, just copied, still being flushed/scanned
    # by antivirus on Windows, etc.) can briefly fail to open ("error reading header" / "End of file") even
    # though the bytes are fine a moment later - this is exactly the reported "works on the 2nd try" import
    # failure. Retry once after a short pause before giving up, instead of reporting the file unreadable.
    for attempt in range(2):
        try:
            txt = run_tool([FFMPEG, "-hide_banner", "-i", path], timeout=25).stderr
        except Exception:
            txt = ""
        if re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt):
            break
        if attempt == 0:
            time.sleep(0.3)
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
    media = Media(path, dur, fps, w, h, vc, f"{ac} {hz}".strip(), bool(vline))
    media.audio_streams = _probe_audio_streams(txt) if aline else []
    return media


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


# [MAP] Turn a still image into a short silent H.264 video (cached by content+mtime+duration under a temp dir) so
# every existing video code path (probe, thumbnails, keyframes, preview, export) just works on it unchanged.
# This IS the "encoded as video on import" behaviour for feature (1): the image becomes a real IMAGE_CLIP_DUR-second
# clip the moment it is imported, not a special-cased still handled later by ExportWorker.
def image_to_clip(path, dur=IMAGE_CLIP_DUR):
    if not FFMPEG:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{path}|{st.st_mtime}|{dur}"
    d = os.path.join(tempfile.gettempdir(), "quickcut_imgclips")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, hashlib.md5(key.encode()).hexdigest() + ".mp4")
    if os.path.isfile(out):
        return out
    try:
        run_tool([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", path,
                  "-t", f"{dur:.3f}", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                  "-r", "30", "-pix_fmt", "yuv420p", "-an", "-c:v", "libx264",
                  "-movflags", "+faststart", out], timeout=30)
    except Exception:
        return None
    if not (os.path.isfile(out) and os.path.getsize(out) > 0):
        return None
    return out


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


