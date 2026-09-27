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
        """Crop/resize and/or per-clip mute/speed/mirror/rotation/audio: re-encode this span.
        Video order: crop/resize -> mirror -> rotate -> reverse -> speed.
        Audio order: track select/mix -> reverse -> speed -> volume -> (mono downmix, as an output option)."""
        mute, speed, mirror, rot, rev, atracks, vol_db, track_type = (
            opts if opts else (False, 1.0, False, 0.0, False, None, 0.0, "stereo"))
        fast = abs(speed - 1.0) > 1e-6
        has_a = bool(media.acodec.strip())
        # [FEATURE] Which of the source's audio streams (Clip Options "Audio Tracks") are actually included.
        n_tracks = max(1, len(media.audio_streams)) if has_a else 0
        if atracks is None:
            # opts was None (this clip was re-encoded only for crop/resize, not for any audio option) -
            # "every track" sized to THIS media, not a stale/undersized tuple from another clip's opts.
            atracks = tuple(True for _ in range(n_tracks))
        selected = [i for i in range(n_tracks) if i < len(atracks) and atracks[i]] if has_a else []
        # [FEATURE] Muting, or deselecting every individual track (nothing left to play), both mean silence.
        silence = has_a and (mute or not selected)
        # Every track still selected (the untouched default) is NOT a "multi-track selection" for mixing
        # purposes - it must keep behaving exactly like the original single `-map 0:a?` (every source audio
        # stream passed through as-is, `-c:a copy`-able) even when the source happens to have more than one
        # audio stream; only an actual PARTIAL multi-select (the user picked >1 but not all tracks) mixes.
        full_default = has_a and selected == list(range(n_tracks))
        multi_mix = has_a and not silence and len(selected) > 1 and not full_default
        mono = has_a and track_type == "mono"
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

        # [FEATURE] Audio post-filters applied AFTER track selection/mixing (order matters: reverse/speed need
        # to run on the already-mixed-down signal, same as the video side runs them after crop/rotate).
        post = (["areverse"] if rev else []) + ([cls._atempo(speed)] if fast else [])
        if abs(vol_db) > 1e-6:
            post.append(f"volume={vol_db:.2f}dB")

        args = ["-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", media.path]
        if silence:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]     # silent replacement track
        args += ["-map", "0:v?"]
        if has_a:
            if silence:
                args += ["-map", "1:a"]
            elif multi_mix:
                # [FEATURE] A genuine PARTIAL multi-track pick (>1 but not all tracks) is mixed into the ONE
                # output audio stream every downstream step (concat, _parts_match, ReverseProxy...) assumes a
                # clip has. Any post-filters are folded into the SAME filter_complex graph - ffmpeg refuses to
                # combine a simple -af with a stream that is already a -filter_complex output.
                mix_in = "".join(f"[0:a:{i}]" for i in selected)
                chain = f"{mix_in}amix=inputs={len(selected)}:duration=longest:dropout_transition=0"
                if post:
                    chain += "[am];[am]" + ",".join(post) + "[aout]"
                else:
                    chain += "[aout]"
                args += ["-filter_complex", chain, "-map", "[aout]"]
            else:
                # Full default (every track, however many there are) OR exactly one specific track chosen -
                # a single -map, identical in shape to the original code's unconditional "0:a?".
                sel_map = "0:a?" if full_default else f"0:a:{selected[0]}"
                args += ["-map", sel_map] + (["-af", ",".join(post)] if post else [])
        if vf:
            args += ["-vf", ",".join(vf), "-r", f"{media.fps:.6f}", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]
        if has_a:
            if silence:
                args += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
            elif post or multi_mix or mono:
                args += ["-c:a", "aac", "-b:a", "192k"]
                if mono:
                    args += ["-ac", "1"]      # downmix to mono - an OUTPUT option, works regardless of source layout
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
    # [FIXED, was KNOWN ISSUE F2] Parts with no audio stream (screen recordings, silent clips, image-baked
    # clips) no longer reference a nonexistent `[i:a:0]`: per-part audio presence is read from media.acodec,
    # and an anullsrc silent track (trimmed to that part's own duration) is synthesized when it's missing.
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
        next_input = 0
        for i, (f, part) in enumerate(zip(files, parts)):
            args += ["-i", f]
            vidx = next_input
            next_input += 1
            scale_chain.append(
                f"[{vidx}:v:0]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]")
            if part[0].acodec.strip():
                concat_inputs.append(f"[v{i}][{vidx}:a:0]")
            else:
                # This part has no audio stream at all (e.g. a clip baked from a still image) - the
                # concat filter needs every part to carry one, so synthesize silence exactly as long
                # as this part's own timeline duration (fixes F2: "Stream specifier ... matches no streams").
                speed = (part[4][1] if len(part) > 4 and part[4] else 1.0) or 1.0
                dur = max(0.02, (part[2] - part[1]) / speed)
                args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                aidx = next_input
                next_input += 1
                concat_inputs.append(f"[v{i}][{aidx}:a]")
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


