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


# [MAP] Global constants.
#   NOWIN         Windows CREATE_NO_WINDOW flag. Pass creationflags=NOWIN to EVERY subprocess that starts
#                 ffmpeg / ffprobe / gifsicle, otherwise a console window flashes on Windows (no-op elsewhere).
#   MIN_DUR       shortest clip (in SOURCE seconds) a trim drag may produce (used by Timeline._trim).
#   VIDEO_FILTER  file-dialog filter for Import. It also lists audio types (mp3/m4a/wav): audio-only files
#                 are legal Media with has_video=False (no thumbnail, no keyframes, no crop/rotate/mirror).
#                 Any new code that reads media.w / media.h / has_video must tolerate that case.
NOWIN = 0x08000000 if os.name == "nt" else 0   # hide console windows on Windows
MIN_DUR = 0.05
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")
IMAGE_CLIP_DUR = 4.0    # default timeline length of an imported still image (it is baked into a real video on import)
IMAGE_MAX_DUR = 600.0   # baked length of that video (bug fix: lets the user freely drag a still's clip edges
                        # to lengthen it on the timeline, up to 10 minutes, instead of hard-capping at IMAGE_CLIP_DUR)
VIDEO_FILTER = ("Media files (*.mp4 *.mkv *.mov *.avi *.webm *.ts *.m4v *.flv *.mts *.m2ts *.mp3 *.m4a *.wav "
                 "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff);;All files (*.*)")


# ----------------------------------------------------------------------------- tools
# [MAP] Locate an external tool: first next to this script (or next to the frozen .exe), then on PATH.
# Returns None when not found. FFMPEG / FFPROBE are resolved ONCE at import time (below); gifsicle is
# looked up on demand (_make_gif, est_secs, export_gif) so dropping gifsicle.exe next to the app works
# without a restart.
# [PITFALL] FFMPEG=None does not stop the app: MainWindow shows a warning and features silently degrade.
# FFPROBE=None disables Snap/Precise and the keyframe scan. Guard any new ffmpeg feature with `if FFMPEG`.
def find_tool(name):
    # [FIX post-split] This file (utils.py) now lives one folder deeper than the old single-file
    # script did (inside quickcut_split/), so "next to this file" alone can miss ffmpeg.exe/ffprobe.exe
    # if they were left next to the project folder instead of moved inside it. Check the script's own
    # folder first (unchanged behaviour), then walk up two more parent folders before falling back to PATH.
    if getattr(sys, "frozen", False):
        bases = [os.path.dirname(sys.executable)]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        parent = os.path.dirname(here)
        grandparent = os.path.dirname(parent)
        bases = [here, parent, grandparent]
    for base in bases:
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


# [MAP] xf may be stored as a 6-tuple (legacy, no color/clip), a 7-tuple (cw,ch,px,py,pw,ph,color), or an
# 11-tuple (...,color,clip_x,clip_y,clip_w,clip_h) once clip_rect (below) has ever been written. Every
# reader of xf goes through xf_color/xf_clip so a missing trailing element never has to be special-cased
# at each call site.
def xf_color(xf):
    """The blank/background color of an xf tuple, or DEFAULT_BG if it carries none."""
    return xf[6] if len(xf) > 6 else DEFAULT_BG


# [INVARIANT] clip_rect is the portion of the (pre-rotation, canvas-space) picture that is actually
# visible - the crop window a Video-mode edit moves. It is DELIBERATELY independent of canvas_rect
# (canvas_w/canvas_h, xf[:2]): canvas_rect is only the output/export frame's size, and Canvas-mode edits
# (VideoStage.crop_mode == "canvas") change ONLY canvas_rect, never this. Defaults to the full canvas
# (clip_rect == canvas_rect) for any xf that predates this field, which reproduces the old (coupled)
# behaviour exactly - see xf_filter below.
def xf_clip(xf):
    """The clip_rect (x, y, w, h) of an xf tuple, in the same source-pixel canvas-space as pic_x/pic_y/
    pic_w/pic_h. Defaults to (0, 0, canvas_w, canvas_h) - i.e. clip_rect == canvas_rect - for xf tuples
    stored before clip_rect existed."""
    if len(xf) > 10:
        return xf[7:11]
    cw, ch = xf[0], xf[1]
    return (0, 0, cw, ch)


# [INVARIANT] Every crop/resize transform that gets STORED in Seg.xf must pass through here (only
# MainWindow.on_stage_commit creates them). It makes all sizes and offsets EVEN (yuv420 chroma needs it) and
# clamps sizes to 16..16384 so nothing collapses to zero or explodes. libx264/yuv420p export fails on odd
# sizes - if you add another producer of xf, call norm_xf on it.
# [NOTE] Always emits the full 11-tuple (clip_rect included) now, even when passed a legacy 6/7-tuple -
# xf_clip's default (clip_rect == canvas_rect) makes that a no-op for anything that was never cropped.
def norm_xf(xf):
    """Snap a crop/resize transform to values ffmpeg/libx264 accept: every size and offset even
    (yuv420 chroma), sizes within 16..16384 so nothing collapses to zero or explodes. Passes the
    blank/background color (7th element) and clip_rect (8th-11th elements) through unchanged (beyond
    the same even/clamped normalization), defaulting via xf_color/xf_clip if absent."""
    cw, ch, px, py, pw, ph = xf[:6]
    color = xf_color(xf)
    clx, cly, clw, clh = xf_clip(xf)
    ev = lambda v: max(16, min(16384, int(round(v / 2.0)) * 2))
    rnd = lambda v: int(round(v / 2.0)) * 2
    return (ev(cw), ev(ch), rnd(px), rnd(py), ev(pw), ev(ph), color, rnd(clx), rnd(cly), ev(clw), ev(clh))


# [MAP][INVARIANT] Single source of truth for crop/resize -> ffmpeg -vf.
# xf = (canvas_w, canvas_h, pic_x, pic_y, pic_w, pic_h, bg_color, clip_x, clip_y, clip_w, clip_h), all in
# SOURCE pixels (post-rotation-tag size): the OUTPUT frame is canvas_w x canvas_h; the source picture,
# scaled to pic_w x pic_h, has its top-left corner at (pic_x, pic_y) inside that frame; clip_rect is the
# portion of that picture actually shown (defaults to the full canvas - see xf_clip). bg_color (e.g.
# "#1a2b3c") fills any area not covered by picture-intersect-clip_rect; it defaults to DEFAULT_BG (black)
# when the tuple predates it.
#     pure crop (Video mode)    clip_rect shrinks (canvas mirrors it - see MainWindow.on_stage_commit),
#                                picture unchanged
#     resize                    picture size changes, canvas/clip_rect unchanged; uncovered area is bg_color
#     un-crop (v4)              clip_rect (and canvas, in step) grows back out over the still-intact
#                                picture; overflow beyond it is bg_color
#     pad/letterbox (Canvas mode)  canvas grows/shrinks alone; clip_rect - and so the video content - never
#                                   moves, so the crop can't be undone or redone by resizing the frame
# Filter chain = scale -> crop down to clip_rect (only if it narrows the picture) -> pad (to the union of
# that visible window and canvas, bg_color) -> crop (to the canvas) -> setsar=1. Only the pieces that are
# needed are emitted. The live preview implements the SAME geometry in VideoStage.relayout/visible_rect -
# if you change the math here, change it there too.
def xf_filter(xf):
    """ffmpeg -vf chain: scale the picture, cut it down to the persistent clip_rect (so anything the crop
    hid stays hidden regardless of canvas size), pad with the chosen bg color to the union of that visible
    window and the canvas, then crop to the canvas. Handles crop, shrink (bg borders), grow (overflow
    cropped) and independent canvas padding/cropping (Canvas mode)."""
    cw, ch, px, py, pw, ph = xf[:6]
    color = xf_color(xf)
    clx, cly, clw, clh = xf_clip(xf)
    # V = picture INTERSECT clip_rect: the part of the video that is EVER visible, independent of canvas.
    vx0, vy0 = max(px, clx), max(py, cly)
    vx1, vy1 = min(px + pw, clx + clw), min(py + ph, cly + clh)
    vw, vh = max(2, vx1 - vx0), max(2, vy1 - vy0)
    f = f"scale={pw}:{ph},"
    if (vx0, vy0, vw, vh) != (px, py, pw, ph):
        f += f"crop={vw}:{vh}:{vx0 - px}:{vy0 - py},"
    ux0, uy0, ux1, uy1 = min(0, vx0), min(0, vy0), max(cw, vx0 + vw), max(ch, vy0 + vh)
    if (ux1 - ux0, uy1 - uy0) != (vw, vh):
        f += f"pad={ux1 - ux0}:{uy1 - uy0}:{vx0 - ux0}:{vy0 - uy0}:color={color},"
    if (ux1 - ux0, uy1 - uy0) != (cw, ch) or ux0 or uy0:
        f += f"crop={cw}:{ch}:{-ux0}:{-uy0},"
    return f + "setsar=1"


