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
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QAudioBufferOutput, QAudioFormat
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
    audioLevel = Signal(float, float)   # [FEATURE] L/R RMS (~0..1) for the audio buffer just decoded - VUMeter

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
        self.audio.setVolume(max(0.0, min(1.0, 10 ** (float(sg.vol_db) / 20.0))))
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

        def _rms(vals):
            if not vals:
                return 0.0
            if unsigned:
                acc = sum((v - 128) * (v - 128) for v in vals)
            else:
                acc = sum(v * v for v in vals)
            return (acc / len(vals)) ** 0.5 / norm

        if ch == 1:
            r = _rms(samples)
            self.audioLevel.emit(r, r)
        else:
            self.audioLevel.emit(_rms(samples[0::ch]), _rms(samples[1::ch]))

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
        self._go(*self._target(n, 0.0), True)


