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
import array

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
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QAudioBufferOutput, QAudioFormat, QMediaDevices, QAudioSink
from PySide6.QtMultimediaWidgets import QVideoWidget, QGraphicsVideoItem

from utils import *
from media_model import *
from probing import *
from export_worker import *
from dialogs import *
from widgets import *
from preview_stack import *
from timeline import *

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
        # [PERF] Other background renderers (ScrubProxy) that must obey the SAME file-lock invariant: cancel_media()
        # and shutdown() below forward to them, so every existing `proxy.cancel_media(path)` call site in
        # MainWindow covers them with no extra wiring.
        self.companions = []
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
        self._q.put((k, seg.media.path, seg.in_s, seg.out_s, seg.media.fps, bool(seg.media.acodec.strip()),
                     seg.media.w, seg.media.h))

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
        for c in self.companions:
            c.cancel_media(path)

    def _run(self):
        while True:
            k, path, a, b, fps, has_a, mw, mh = self._q.get()
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
                # [PERF][RAM] ffmpeg's `reverse` filter holds EVERY decoded frame of the span in memory (raw yuv420p).
                # Cap that at ~384 MB by lowering the proxy height for long spans (30 s @ 30 fps 16:9 -> ~400 px
                # instead of 540; anything up to ~15 s stays 540). Floor 240 px.
                nfr = max(1.0, (b - a) * min(30.0, max(1.0, fps)))
                ar = (mw / mh) if (mw and mh) else 16 / 9
                hcap = int((384e6 / (nfr * 1.5 * ar)) ** 0.5) // 2 * 2
                hmax = max(240, min(540, hcap))
                vf = f"scale=-2:'min({hmax},ih)'" + (",fps=30" if fps > 30.5 else "") + ",reverse"
                cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2",
                       "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", path, "-map", "0:v:0"]
                if has_a:
                    cmd += ["-map", "0:a:0?", "-af", "areverse"]
                cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-g", "12",
                        "-threads", "2", "-pix_fmt", "yuv420p"]
                if has_a:
                    cmd += ["-c:a", "aac", "-b:a", "128k"]
                cmd.append(out)
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=NOWIN | LOW_PRIO)
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
        for c in self.companions:
            c.shutdown()


# [MAP][PERF] Adaptive-resolution scrubbing. QMediaPlayer can't be told to decode at a lower resolution, so when
# scrubbing can't keep up (Engine._scrub_note: 3 slow seeks in a row) the Engine asks this class for a SCRUB PROXY of
# the media under the playhead: the WHOLE file, <= 360 px tall, <= 30 fps, ALL-INTRA (-g 1: every frame is a keyframe,
# so a seek decodes exactly one small frame), rendered in the background at below-normal priority with 2 threads.
# Unlike the reverse proxy it is TIME-ALIGNED with the source (same timestamps), so the Engine only swaps the file
# path and keeps the same position. It is used ONLY for scrub seeks (Engine._scrub_seek) and only while degraded
# (Engine._lowres_until); at most MAX_FILES kept, files <= MAX_SEC; the Engine reloads the full-resolution source when the mouse rests for ~220 ms
# (Engine._scrub_settle) and when the session ends. Nothing is rendered unless scrubbing actually stutters.
# Identity: key = media.path. `files` (path -> proxy file) is the SAME dict as Engine.scrub_files (assigned in
# MainWindow.__init__ - never rebind). At most MAX_FILES kept (oldest dropped, never the one the player has open).
# [INVARIANT] Same as ReverseProxy: cancel_media(path) before rename / remove / clear / Save-Over - forwarded
# automatically through ReverseProxy.companions. shutdown() kills ffmpeg and deletes "quickcut_scrub_*".
class ScrubProxy(QObject):
    MAX_SEC = 1200.0        # don't transcode anything longer than 20 min just to scrub it (disk!)
    MAX_FILES = 3
    MIN_HEIGHT = 480        # below this, full-res decode is already cheap - a proxy would not help

    def __init__(self):
        super().__init__()
        self.files = {}         # media path -> finished proxy path (read by Engine)
        self.in_use = lambda: None      # returns the file the player has open (set by MainWindow)
        self._known = set()     # paths queued / running / done / failed (never retried until cancel_media)
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._cur = None
        self._dir = None
        self._n = 0
        threading.Thread(target=self._run, daemon=True).start()
        import atexit
        atexit.register(self.shutdown)

    def eligible(self, m):
        return bool(FFMPEG and m.has_video and m.h >= self.MIN_HEIGHT and 0 < m.dur <= self.MAX_SEC)

    def request(self, m):
        if not self.eligible(m):
            return
        with self._lock:
            if m.path in self._known:
                return
            self._known.add(m.path)
        self._q.put((m.path, m.fps, bool(m.acodec.strip())))

    def cancel_media(self, path):
        with self._lock:
            self._known.discard(path)
            p = self.files.pop(path, None)
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
            proc = self._proc if self._cur == path else None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _run(self):
        while True:
            path, fps, has_a = self._q.get()
            with self._lock:
                if path not in self._known or path in self.files:
                    continue
                self._cur = path
            out = None
            try:
                if self._dir is None:
                    self._dir = tempfile.mkdtemp(prefix="quickcut_scrub_")
                self._n += 1
                out = os.path.join(self._dir, f"scrub{self._n:04d}.mp4")
                vf = "scale=-2:'min(360,ih)'" + (",fps=30" if fps > 30.5 else "")
                cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2", "-i", path,
                       "-map", "0:v:0"]
                if has_a:
                    cmd += ["-map", "0:a:0?"]
                cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode",
                        "-crf", "30", "-g", "1", "-pix_fmt", "yuv420p"]
                if has_a:
                    cmd += ["-c:a", "aac", "-b:a", "96k"]
                cmd.append(out)
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=NOWIN | LOW_PRIO)
                rc = self._proc.wait()
                with self._lock:
                    ok = rc == 0 and path in self._known and os.path.isfile(out)
                    if ok:
                        self.files[path] = out
                        busy = self.in_use()
                        for k in [k for k in self.files if k != path and self.files[k] != busy]:
                            if len(self.files) <= self.MAX_FILES:
                                break
                            old = self.files.pop(k)
                            self._known.discard(k)
                            try:
                                os.remove(old)
                            except OSError:
                                pass
                if not ok:
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
    audioLevel = Signal(float, float)   # [FEATURE] L/R RMS (~0..1) for the audio buffer just decoded - VUMeter

    perfChanged = Signal(bool)          # True while the preview can't keep up

    SCRUB_MIN_GAP = 0.012               # s, floor between real seeks while dragging
    SCRUB_FIXED_MS = 35                 # fallback fixed throttle (only if frame signal is unusable)
    BLIP_MS = 110                       # audio blip length per scrub seek
    SCRUB_IDLE_MS = 600                 # scrub session ends this long after the last mouse move
    SLOW_SEEK = 0.22                    # s, seek->frame latency counted as "can't keep up"

    def __init__(self, seq, video_widget):
        super().__init__()
        self.seq = seq
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(getattr(video_widget, "item", video_widget))
        # [FEATURE] Tap the decoded audio for the VU meter. Best-effort: QAudioBufferOutput needs Qt >= 6.8, so
        # this is wrapped defensively - on an older Qt/PySide6 the meter just never receives levels (stays at
        # zero) instead of crashing the app. Purely a monitoring tap; never touches what's actually played.
        self._audio_buf_out = None
        try:
            self._audio_buf_out = QAudioBufferOutput(self)
            self.player.setAudioBufferOutput(self._audio_buf_out)
            self._audio_buf_out.audioBufferReceived.connect(self._on_audio_buffer)
        except Exception:
            self._audio_buf_out = None
        self.path = None
        self.idx = 0
        self.proxies = {}           # reverse-preview proxies (shared dict from ReverseProxy.files)
        self.scrub_files = {}       # media path -> low-res scrub proxy (shared dict from ScrubProxy.files)
        self.scrub_proxy = None     # ScrubProxy instance (set by MainWindow); None = adaptive resolution off
        self._scrub_seek = False    # True only while _scrub_send is calling seek() (see _target)
        self._lowres_until = 0.0    # monotonic deadline: prefer scrub proxies for scrub seeks until then
        self.playing = False        # what the player is really doing right now (drives the icon)
        self._want_playing = False  # what the user asked for (drives auto-advance across clips)
        self.loop = False           # right-click timeline -> Loop: restart from 0 instead of stopping at the end
        self._rate_boost = 1.0      # temporary playback-rate multiplier (MainWindow: hold Space to double it)
        self.playhead = 0.0
        self._pending = None
        self._last_pos = 0.0
        self._settle = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(15)
        self.timer.timeout.connect(self._tick)
        # --- scrub state (adaptive: next seek is sent as soon as the previous one showed a frame)
        self._sink = video_widget.videoSink()
        # [FIX] Hold the last good frame across source switches (see VideoView.freeze) instead of flashing black.
        self._view = video_widget if hasattr(video_widget, "freeze") else None
        self._thaw_hooked = False
        self._thaw_timer = QTimer(self)
        self._thaw_timer.setSingleShot(True)
        self._thaw_timer.setInterval(2500)       # failsafe: never leave a stale frame up forever
        self._thaw_timer.timeout.connect(self._thaw)
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
        # [FEATURE 51.1] Audible scrubbing: each scrub seek plays the player for a short blip so audio is decoded
        # (VU meter moves + you hear it). `_blip` = a blip is running; reports of it must not touch `playing`.
        self._boost_ok, self._boost_on, self._boost_gain = True, False, 1.0     # [51.6] amplified-preview path
        self._boost_sink, self._boost_io, self._boost_key = None, None, None
        self._blip = False
        self._blip_guard = 0.0
        self._blip_ms = 0
        self._blip_timer = QTimer(self)
        self._blip_timer.setSingleShot(True)
        self._blip_timer.setInterval(self.BLIP_MS)
        self._blip_timer.timeout.connect(self._blip_stop)
        self._scrub_watch = QTimer(self)
        self._scrub_watch.setSingleShot(True)
        self._scrub_watch.timeout.connect(self._scrub_timeout)
        self._scrub_idle = QTimer(self)
        self._scrub_idle.setSingleShot(True)
        self._scrub_idle.setInterval(self.SCRUB_IDLE_MS)
        self._scrub_idle.timeout.connect(self._scrub_end)
        # mouse rests (no scrub input for 220 ms) -> reload the full-resolution source if a proxy is showing
        self._scrub_rest = QTimer(self)
        self._scrub_rest.setSingleShot(True)
        self._scrub_rest.setInterval(220)
        self._scrub_rest.timeout.connect(self._scrub_settle)
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
        # [PERF] Standby channel = a second QMediaPlayer + QAudioOutput rendering into VideoView.item2. While playing, the
        # NEXT clip (when it needs a different file/position) is loaded and parked on its first frame here ~2.5 s
        # ahead; at the boundary _advance() swaps the two channels instead of stop()+setSource()+load, so there is no
        # black gap or decoder start-up stall. Any explicit seek/pause/edit drops it (releases the file - see
        # _drop_standby; every file-lock site calls engine.pause() first). If anything about it isn't ready or
        # doesn't match, _advance falls back to the old _go() path (+ the held-frame overlay), so it can only help.
        self._sp, self._sa, self._sb = None, None, None
        self._swap_until = 0.0
        self._pre_img, self._pre_idx = None, -1
        if hasattr(video_widget, "item2") and hasattr(video_widget, "swap_items"):
            try:
                self._sp = QMediaPlayer(self)
                self._sa = QAudioOutput(self)
                self._sp.setAudioOutput(self._sa)
                self._sp.setVideoOutput(video_widget.item2)
                self._audio_buf_out2 = None
                try:
                    self._audio_buf_out2 = QAudioBufferOutput(self)
                    self._sp.setAudioBufferOutput(self._audio_buf_out2)
                    self._audio_buf_out2.audioBufferReceived.connect(self._on_audio_buffer)
                except Exception:
                    self._audio_buf_out2 = None
            except Exception:
                self._sp, self._sa = None, None
        self._wire(self.player)
        if self._sp is not None:
            self._wire(self._sp)
        # [FIX 51.2] Always follow the system's MAIN audio device. Plugging/unplugging/switching a device used to leave
        # the QAudioOutputs bound to a dead endpoint (AUDCLNT_E_DEVICE_INVALIDATED). Device-list changes (debounced,
        # Windows fires several) and that error itself both rebind every output to the current default device.
        self._devs = QMediaDevices(self)
        self._dev_timer = QTimer(self)
        self._dev_timer.setSingleShot(True)
        self._dev_timer.setInterval(200)
        self._dev_timer.timeout.connect(self._rebind_audio)
        self._devs.audioOutputsChanged.connect(self._dev_timer.start)
        self._rebind_audio(resume=False)

    # Signals are connected for BOTH channels; each handler checks whether its sender is currently the ACTIVE player
    # (self.player) so roles can be swapped at runtime without reconnecting anything.
    def _wire(self, pl):
        pl.mediaStatusChanged.connect(
            lambda st, pl=pl: self._on_status(st) if pl is self.player else self._on_sb_status(st, pl))
        pl.errorOccurred.connect(
            lambda err, msg, pl=pl: self._on_error(err, msg) if pl is self.player else self._on_sb_error(pl))
        pl.playbackStateChanged.connect(lambda st, pl=pl: self._on_state_from(pl, st))

    def _on_state_from(self, pl, st):
        if pl is not self.player:
            return
        # A Paused/Stopped report queued from before a swap must not cancel playback that the swap just started.
        if (st != QMediaPlayer.PlaybackState.PlayingState and self._want_playing
                and time.monotonic() < self._swap_until):
            return
        self._on_playback_state(st)

    # [MAP] Applies the current clip's PREVIEW options: mute (audio.setMuted), volume (audio.setVolume) and
    # playback speed (playbackRate). Track selection / mono downmix are export-only (see ClipOptionsDialog's
    # [KNOWN ISSUE] note) - mirror/rotation are handled by VideoStage/VideoView, reverse by proxies.
    # [KNOWN ISSUE F6] Rate is clamped to 0.05..20 while the clip dialog allows 0.05..100, so a 50x clip previews at
    # 20x while _tick divides the source position by 50: the playhead crawls and _monitor falsely reports
    # "can't keep up" (its expected rate is speed-relative).
    def _apply_fx(self):
        """Per-clip mute + volume + playback speed for the clip now at self.idx (additive; no original line changed)."""
        segs = self.seq.segs
        if not segs:
            return
        sg = segs[min(self.idx, len(segs) - 1)]
        self.audio.setMuted(bool(sg.mute))
        # [FEATURE] Clip Options "Volume" slider (dB, +0 = unity). QAudioOutput's volume only goes up to 1.0
        # (0 dB), so a POSITIVE dB clamps silently in the live preview; export (ExportWorker._fx_args, its
        # `volume=` filter) is not capped and applies the full requested gain.
        # [FIX 51.6] QAudioOutput can't exceed 1.0, so a boost is NOT done there (and no other clip is made quieter).
        # For gain > 0 dB the player's own output is silenced and the decoded buffers (the meter's tap) are amplified
        # (hard-clipped, like export's volume filter) and played through our own QAudioSink - see _boost_write.
        g = 10 ** (float(sg.vol_db) / 20.0)
        self._boost_gain = g
        self._boost_on = bool(g > 1.0001 and self._boost_ok and not sg.mute)
        self.audio.setVolume(0.0 if self._boost_on else max(0.0, min(1.0, g)))
        r = max(0.05, min(float(sg.speed) * self._rate_boost, 20.0))
        if abs(self.player.playbackRate() - r) > 1e-6:
            self.player.setPlaybackRate(r)

    # [FEATURE] Turns one decoded QAudioBuffer into a per-channel RMS level and forwards it as audioLevel(l, r)
    # for VUMeter. Reads raw samples via `array` (stdlib - no numpy dependency) according to the buffer's own
    # sample format; unrecognised formats are simply skipped (meter stays silent for that buffer, no crash).
    # Cheap (a `sum` over one buffer's samples, typically ~1-4k), so this is safe to run on the GUI thread.
    _FMT_CODES = {
        QAudioFormat.SampleFormat.UInt8: ("B", 128.0, True),
        QAudioFormat.SampleFormat.Int16: ("h", 32768.0, False),
        QAudioFormat.SampleFormat.Int32: ("i", 2147483648.0, False),
        QAudioFormat.SampleFormat.Float: ("f", 1.0, False),
    }

    # [FEATURE 51.6] Audible boosts. Amplify the decoded buffer by the clip's gain (hard clip at full scale, as export's
    # `volume=` filter does) and push it to our own QAudioSink on the default device. The player's output is at volume 0
    # meanwhile (_apply_fx). Costs ~one sink buffer (~60 ms) of extra latency vs. the picture. Any failure -> boost path
    # disabled for the session and the clip plays at unity (never louder than the source, never quieter than before).
    def _boost_write(self, fmt, code, norm, unsigned, samples):
        try:
            if unsigned:
                raise ValueError("8-bit audio")
            g = self._boost_gain
            if code == "f":
                out = array.array("f", [max(-1.0, min(1.0, v * g)) for v in samples])
            else:
                hi, lo = int(norm) - 1, -int(norm)
                out = array.array(code, [max(lo, min(hi, int(v * g))) for v in samples])
            key = (fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat())
            if self._boost_sink is None or self._boost_key != key:
                self._boost_flush()
                if not QMediaDevices.defaultAudioOutput().isFormatSupported(fmt):
                    raise ValueError("format unsupported")
                sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt, self)
                sink.setBufferSize(fmt.bytesForDuration(60000))
                self._boost_io = sink.start()
                self._boost_sink, self._boost_key = sink, key
            self._boost_io.write(out.tobytes())
        except Exception:
            self._boost_flush()
            self._boost_ok = self._boost_on = False
            self.audio.setVolume(1.0)

    def _boost_flush(self):
        sink, self._boost_sink, self._boost_io, self._boost_key = self._boost_sink, None, None, None
        if sink is not None:
            try:
                sink.stop()
                sink.deleteLater()
            except Exception:
                pass

    def _on_audio_buffer(self, buf):
        if self.audio.isMuted():
            self.audioLevel.emit(0.0, 0.0)
            return
        try:
            fmt = buf.format()
            spec = self._FMT_CODES.get(fmt.sampleFormat())
            ch = fmt.channelCount()
            if not spec or ch < 1:
                return
            code, norm, unsigned = spec
            samples = array.array(code)
            samples.frombytes(bytes(buf.constData()))
        except Exception:
            return
        if not samples:
            self.audioLevel.emit(0.0, 0.0)
            return
        if self._boost_on and self.playing and not self._blip:
            self._boost_write(fmt, code, norm, unsigned, samples)

        def _rms(vals):
            if not vals:
                return 0.0
            if unsigned:
                acc = sum((v - 128) * (v - 128) for v in vals)
            else:
                acc = sum(v * v for v in vals)
            return (acc / len(vals)) ** 0.5 / norm

        # [PERF] Only ~256 frames per buffer are needed for a stable meter reading; the pure-Python sum over
        # every sample ran on the GUI thread for each audio buffer. Strided C-level slicing keeps it O(256).
        step = max(1, (len(samples) // ch) // 256)
        # [FIX 51.4] the tapped buffers are pre-volume: apply the current clip's gain so the meter follows Volume changes
        try:
            g = 10 ** (float(self.seq.segs[min(self.idx, len(self.seq.segs) - 1)].vol_db) / 20.0)
        except Exception:
            g = 1.0
        if ch == 1:
            r = _rms(samples[::step]) * g
            self.audioLevel.emit(r, r)
        else:
            self.audioLevel.emit(_rms(samples[0::ch * step]) * g, _rms(samples[1::ch * step]) * g)

    # [FEATURE] Temporary playback-rate multiplier, independent of the clip's own Seg.speed: MainWindow applies
    # 2.0 while Space is held down during playback (see MainWindow.keyPressEvent/keyReleaseEvent) and 1.0 when
    # it's released, so a quick tap still toggles play/pause but a hold fast-forwards instead. Reapplies
    # immediately via _apply_fx (normally only called on seek/clip-change) so the change is heard/seen at once.
    # Never touches Seg.speed, so nothing here needs to survive across clips, undo, or export.
    def set_speed_boost(self, mult):
        if mult != self._rate_boost:
            self._rate_boost = mult
            self._apply_fx()

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
        if px:
            return px, o
        # [PERF] Adaptive resolution: a scrub seek while degraded uses the time-aligned low-res proxy if it's ready.
        if self._scrub_seek and time.monotonic() < self._lowres_until:
            lp = self.scrub_files.get(seg.media.path)
            if lp and os.path.isfile(lp):
                return lp, seg.in_s + o
        return seg.media.path, seg.in_s + o

    # [INVARIANT][DO NOT BREAK #8] (start, end) of the clip in the units of the file that is loaded RIGHT NOW: (0,
    #   src_dur)
    # when the proxy is what the player has open, else (in_s, out_s). _tick and the EndOfMedia test use it.
    def _span(self, seg):
        """(start, end) of seg in the units of the file the player currently has loaded."""
        px = self.proxy_path(seg)
        return (0.0, seg.src_dur) if (px and self.path == px) else (seg.in_s, seg.out_s)

    @staticmethod
    def _is_audio_dev_error(msg):
        m = (msg or "").lower()
        return "audclnt" in m or "audio device" in m or "audio output" in m

    def _rebind_audio(self, resume=True):
        """Point every QAudioOutput at the current default output device (no-op-safe; never raises)."""
        try:
            self._boost_flush()             # sink is bound to the old device
            dev = QMediaDevices.defaultAudioOutput()
            for a in (self.audio, self._sa):
                if a is not None and a.device() != dev:
                    a.setDevice(dev)
        except Exception:
            return
        # a player whose device died may have stopped: restore position/intent on the new device
        if resume and self.seq.segs and self._pending is None:
            try:
                self.seek(self.playhead, play=self._want_playing)
            except Exception:
                pass

    def _on_error(self, err, msg):
        if self._is_audio_dev_error(msg):          # [FIX 51.2] device vanished: switch to default, no error popup
            self._rebind_audio()
            return
        self._pending = None
        self._thaw()
        self.error.emit(msg or "Playback error")

    # [MAP] Makes `playing` (and therefore the play/pause icon and the tick timer) truthful to the QMediaPlayer's
    # real state instead of assuming that our last play()/pause() took effect. This fixed the button staying on
    # "pause" after a clip switch.
    def _on_playback_state(self, state):
        """Keep our own 'playing' flag (and the UI's play/pause icon) truthful to what the
        player is actually doing, instead of just assuming our last play()/pause() call took
        effect - this is what was leaving the button stuck on 'pause' after a clip switch."""
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        # [51.1] scrub-audio blips play the player briefly without the user asking for playback: ignore their reports
        if not self._want_playing and (self._blip or (playing and time.monotonic() < self._blip_guard)):
            return
        if not playing:
            self._boost_flush()
        if playing != self.playing:
            self.playing = playing
            self.timer.start() if playing else self.timer.stop()
            self.playStateChanged.emit(playing)

    # ---- gapless clip switching (standby channel)
    PRELOAD_LEAD = 2.5          # start preloading the next clip this many (source) seconds before the current ends

    def _drop_standby(self):
        sb, self._sb = self._sb, None
        if sb is not None and self._sp is not None:
            try:
                self._sp.stop()
                self._sp.setSource(QUrl())
            except Exception:
                pass

    def _next_needs_reload(self, cur, n):
        return not (n.media.path == cur.media.path and abs(n.in_s - cur.out_s) < 0.05
                    and self.proxy_path(n) is None and self.path == cur.media.path)

    # Called from _tick every 15 ms while playing: cheap early-outs, then (once per clip) starts the standby load.
    # Also remembers a picture of the current frame shortly before a switch so the held-frame overlay always has
    # something to show even if the sink has already been cleared by the time we switch.
    def _maybe_preload(self, seg, pos, end):
        if self._scrub_active or self._sb is not None or self.idx + 1 >= len(self.seq.segs):
            return
        left = end - pos
        n = self.seq.segs[self.idx + 1]
        if left > self.PRELOAD_LEAD * max(1.0, seg.speed * self._rate_boost) or not self._next_needs_reload(seg, n):
            return
        if self._sp is not None:
            path, src_t = self._target(n, 0.0)
            self._sb = {"idx": self.idx + 1, "path": path, "ms": int(max(0.0, src_t) * 1000), "ready": False}
            try:
                self._sp.setSource(QUrl.fromLocalFile(path))
            except Exception:
                self._sb = None
        if self._pre_idx != self.idx and left < 0.5 * max(1.0, seg.speed * self._rate_boost):
            self._pre_idx = self.idx
            try:
                fr = self._sink.videoFrame()
                if fr.isValid():
                    im = fr.toImage()
                    self._pre_img = im.scaledToWidth(960) if im.width() > 960 else im
            except Exception:
                self._pre_img = None

    def _on_sb_status(self, st, pl):
        sb = self._sb
        if sb is None or pl is not self._sp or sb["ready"]:
            return
        S = QMediaPlayer.MediaStatus
        if st in (S.LoadedMedia, S.BufferedMedia):
            if pl.source() != QUrl.fromLocalFile(sb["path"]):
                return
            sb["ready"] = True
            pl.setPosition(sb["ms"])
            pl.pause()                  # parks on the first frame, rendered (hidden) into VideoView.item2

    def _on_sb_error(self, pl):
        if pl is self._sp:
            self._drop_standby()

    # True if the switch was done by swapping in the preloaded standby (caller must then NOT call _go).
    def _swap_standby(self, nxt, path, src_t):
        sb = self._sb
        if (sb is None or not sb["ready"] or sb["idx"] != nxt or sb["path"] != path
                or sb["ms"] != int(max(0.0, src_t) * 1000) or self._scrub_active or self._pending is not None):
            return False
        self._thaw()
        old = self.player
        self._sb = None
        self.player, self._sp = self._sp, old
        self.audio, self._sa = self._sa, self.audio
        self._view.swap_items()
        self._sink = self._view.videoSink()
        self.path = path
        self._pending = None
        self._settle = time.monotonic() + 0.15
        self._swap_until = time.monotonic() + 0.4
        self._last_pos = src_t
        self._apply_fx()                # volume / mute / rate of the clip that is now current
        self.player.play()
        try:
            old.stop()
            old.setSource(QUrl())       # retire the old channel (releases its file)
        except Exception:
            pass
        return True

    # [FIX] Black flash on source switch. Qt clears the sink when the source changes, so just BEFORE switching we copy
    # the last valid frame into VideoView's overlay and keep it until the new source delivers a real frame after its
    # pending seek has been applied (or 2.5 s pass). If already holding a frame (rapid successive switches while
    # scrubbing) the older held frame is kept - the sink has nothing valid to grab anyway.
    def _hold_frame(self):
        v = self._view
        if v is None:
            return
        try:
            if not v.is_frozen():
                fr = self._sink.videoFrame()
                if fr.isValid():
                    v.freeze(fr.toImage())
                elif self._pre_img is not None:
                    v.freeze(self._pre_img)         # sink already cleared (e.g. EndOfMedia): use the pre-captured one
                self._pre_img = None
            if v.is_frozen():
                if not self._thaw_hooked:
                    self._thaw_hooked = True
                    self._sink.videoFrameChanged.connect(self._on_thaw_frame)
                self._thaw_timer.start()
        except Exception:
            pass

    def _on_thaw_frame(self, frame):
        if self._pending is None and frame.isValid():
            self._thaw()

    def _thaw(self):
        self._thaw_timer.stop()
        if self._thaw_hooked:
            self._thaw_hooked = False
            try:
                self._sink.videoFrameChanged.disconnect(self._on_thaw_frame)
            except Exception:
                pass
        if self._view is not None:
            self._view.thaw()

    # [DO NOT BREAK #1] Switching files: set self.path, stash (ms, play) in _pending, player.stop(), THEN setSource().
    # The pending seek/play is applied later by _on_status when the new file reports Loaded/Buffered. Same file while a
    # load is still pending: just update _pending. Same file, nothing pending: direct setPosition + play/pause.
    # _settle (150 ms) makes _tick ignore position reports that still belong to the old position.
    def _go(self, path, src_t, play):
        ms = int(max(0.0, src_t) * 1000)
        self._settle = time.monotonic() + 0.15
        self._last_pos = src_t
        if path != self.path:
            self._hold_frame()
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
        self._drop_standby()            # an explicit seek supersedes any preloaded next clip
        self._boost_flush()             # drop queued amplified audio of the old position
        self._pre_img, self._pre_idx = None, -1     # a pre-captured end-of-clip picture is only valid for playback
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
            self._thaw()
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
        self._scrub_rest.start()
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
        self._scrub_seek = True
        try:
            self.seek(t, play=False)
        finally:
            self._scrub_seek = False
        self._scrub_valid = (self.path == before)     # a file switch is legitimately slow
        self._blip_kick()
        self._scrub_inflight = True
        self._scrub_sent = time.monotonic()
        if self._scrub_fixed:
            wd = self.SCRUB_FIXED_MS / 1000.0
        elif self._scrub_frames == 0:
            wd = 0.6                                  # grace until we've seen a frame signal work at all
        else:
            wd = min(0.6, max(0.12, self._scrub_ema * 3))
        self._scrub_watch.start(int(wd * 1000))

    # [FEATURE 51.1] Audible scrubbing. After a scrub seek (player paused on the target) play for BLIP_MS so the audio
    # buffer output feeds the VU meter (and the speakers). Skipped while a source load is pending (next seek blips),
    # while really playing, or when muted. `_blip_stop` pauses again and snaps the position back to the target so
    # the player never drifts ahead of the playhead.
    def _blip_kick(self):
        if self._want_playing or self.playing or self._pending is not None or self.audio.isMuted():
            return
        self._blip_ms = int(max(0.0, self._last_pos) * 1000)
        self._blip = True
        self._blip_guard = time.monotonic() + 0.5
        self.audio.setVolume(0.0)       # [51.3] silent: the player can't keep up so it only crackled; meter still gets buffers
        self.player.play()
        self._blip_timer.start()

    def _blip_stop(self):
        if not self._blip:
            return
        self._blip = False
        self._apply_fx()                # restores the clip's real volume/mute (blip set volume to 0)
        if self._want_playing or self.playing or self._pending is not None:
            return
        self.player.pause()
        self.player.setPosition(self._blip_ms)

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
        self._scrub_rest.stop()
        self._restore_fullres()

    # [PERF] Processing headroom is back (the mouse is resting): if the player is showing a low-res scrub proxy,
    # reload the full-resolution source at the same playhead. Never while playing or with a scrub target pending.
    def _scrub_settle(self):
        if self._scrub_inflight or self._scrub_t is not None:
            self._scrub_rest.start()
            return
        self._restore_fullres()

    def _restore_fullres(self):
        if (self.path and not self.playing and not self._want_playing and self.seq.segs
                and self.path in list(self.scrub_files.values())):
            self.seek(self.playhead, play=False)          # _scrub_seek is False -> _target picks the source

    def _scrub_note(self, slow):
        self._scrub_slow = self._scrub_slow + 1 if slow else max(0, self._scrub_slow - 1)
        if self._scrub_slow >= 3:
            self._scrub_slow = 0
            # [PERF] Try normal resolution first (we just did); now that it demonstrably can't keep up, ask for a
            # low-res proxy of the media under the playhead and prefer it for scrub seeks for the next 30 s.
            self._lowres_until = time.monotonic() + 30.0
            if self.scrub_proxy is not None and self.seq.segs:
                self.scrub_proxy.request(self.seq.segs[min(self.idx, len(self.seq.segs) - 1)].media)
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
            spd = (self.seq.segs[self.idx].speed if self.idx < len(self.seq.segs) else 1.0) * self._rate_boost
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
        self._drop_standby()            # releases the preloaded file too (file-lock rule: callers pause() first)
        if self.playing:
            self.playing = False
            self.timer.stop()
            self.player.pause()
            self.playStateChanged.emit(False)

    # [MAP] 15 ms timer while playing. Skips while a load is pending or inside the 150 ms _settle window. Reads the
    # player position (file seconds), runs the perf monitor, and either advances at the clip's end (pos >= end - 10 ms
    # in the LOADED file's units) or converts position -> timeline time: start_of_clip + (pos - base) / speed.
    # Emits playheadChanged (=> MainWindow.on_playhead => refresh_stage, timeline follow, timecode).
    # [52.21] A blank clip is a 1-fps stand-in video, so QMediaPlayer.position() only advances in ~1 s steps (the playhead
    # stuttered and jumped although the time was right). While a blank clip plays, the position is interpolated with the wall
    # clock from an anchor taken after every seek/clip change (key = idx + _settle); after a pause (>0.3 s between ticks) the
    # anchor restarts from the last shown playhead so the pause is not counted.
    def _blank_pos(self, raw, seg, base, now):
        key, bk = (self.idx, self._settle), getattr(self, "_bk", None)
        if bk is None or bk[0] != key:
            self._bk = bk = [key, raw, now, now]
        elif now - bk[3] > 0.3:
            bk[1] = base + max(0.0, self.playhead - self.seq.starts()[self.idx]) * (seg.speed or 1.0)
            bk[2] = now
        bk[3] = now
        return bk[1] + (now - bk[2]) * (self.player.playbackRate() or 1.0)

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
        if getattr(seg.media, "blank", False):
            pos = self._blank_pos(pos, seg, base, now)
        if pos >= end - 0.01:
            self._advance()
            return
        self._maybe_preload(seg, pos, end)
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
            if self.loop:
                self.seek(0.0, play=True)
                return
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
        path, src_t = self._target(n, 0.0)
        if self._swap_standby(nxt, path, src_t):
            return
        self._drop_standby()
        self._go(path, src_t, True)


