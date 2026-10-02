# ====================================================================================================
# crashlog.py (v52.8) - crash reports. Load order: after recovery.py (it only needs utils; recovery is imported lazily).
# Writes a readable log into <program folder>/crash_logs/ whenever something goes wrong:
#   * an uncaught Python exception (main thread, Qt slots/timers, worker threads, "unraisable" ones) -> crash_YYYYmmdd_HHMMSS.log
#   * a NATIVE crash (segfault / abort inside Qt, ffmpeg backend...) -> Python's faulthandler writes the stack of every thread
#     into crash_logs/native_crash.log while the process dies; the NEXT start renames it to crash_<time>_native.log.
# Hooks are installed by install_crash_logging() as the very first thing in main(); crash_set_window(win) later lets the
# report include the project state. Nothing here may ever raise: a failing report must not become a second crash.
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
import atexit
import faulthandler
import platform
import traceback
import collections

from utils import *

CRASH_KEEP = 20            # newest N crash_*.log files are kept
CRASH_SAME_MAX = 3         # the same error (type + last frame) writes at most this many files per run (timer loops!)

_S = {"win": None, "dir": None, "seen": {}, "qt": collections.deque(maxlen=60), "fh": None, "adopted": None,
      "lock": threading.Lock(), "installed": False}


def _program_dir():
    """Folder of the program (next to main.py, or next to the frozen .exe) - same idea as utils.find_tool."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, ".w")
        with open(t, "w") as f:
            f.write("x")
        os.remove(t)
        return True
    except OSError:
        return False


# [MAP] <program folder>/crash_logs. If that folder can't be written (e.g. installed under Program Files) it falls back to
# <user data>/QuickCut/crash_logs, then the temp folder - a report somewhere beats no report. Resolved once.
def crash_dir():
    if _S["dir"] is None:
        cands = [os.path.join(_program_dir(), "crash_logs")]
        try:
            from PySide6.QtCore import QStandardPaths
            base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
            if base:
                cands.append(os.path.join(base, "QuickCut", "crash_logs"))
        except Exception:
            pass
        cands.append(os.path.join(tempfile.gettempdir(), "quickcut_crash_logs"))
        _S["dir"] = next((c for c in cands if _writable(c)), cands[-1])
    return _S["dir"]


def _versions():
    out = [f"Program : {APP_NAME}", f"Python  : {sys.version.split()[0]} ({platform.architecture()[0]})",
           f"OS      : {platform.platform()}", f"Frozen  : {bool(getattr(sys, 'frozen', False))}"]
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        out.append(f"PySide6 : {PySide6.__version__}   Qt {qVersion()}")
    except Exception:
        pass
    out += [f"ffmpeg  : {FFMPEG}", f"ffprobe : {FFPROBE}"]
    return out


def _state_lines():
    w = _S["win"]
    if w is None:
        return ["(main window was not created yet)"]
    out = []

    def g(label, fn):
        try:
            out.append(f"{label}: {fn()}")
        except Exception as e:  # noqa: BLE001
            out.append(f"{label}: <unavailable: {e}>")
    g("Clips on timeline", lambda: len(w.seq.segs))
    g("Timeline length", lambda: f"{w.seq.total():.2f} s")
    g("Trim mode", lambda: "Keyframes" if w.seq.snap else "Precise")
    g("Playhead", lambda: f"{w.engine.playhead:.2f} s (playing={w.engine.playing})")
    g("Engine file", lambda: w.engine.path)
    g("Tool", lambda: w.tl.tool)
    g("RAM used/total MB", lambda: system_memory())
    g("Recovery file", lambda: (lambda p: f"{p} (exists={os.path.isfile(p)})")(__import__("recovery").recovery_path()))

    def medias():
        segs = list(w.seq.segs)
        lines = []
        for m in list(w.medias)[:30]:
            n = sum(1 for s in segs if s.media is m)
            lines.append(f"    {m.path}  [{m.w}x{m.h} {m.fps:g}fps {m.vcodec}/{m.acodec} {m.dur:.1f}s] clips={n}")
        return "\n" + "\n".join(lines) + ("\n    ..." if len(w.medias) > 30 else "")
    g("Project media", medias)
    return out


def _report_text(kind, et, ev, tb, thread):
    lines = [f"{APP_NAME} crash report", "=" * 60,
             f"Time    : {time.strftime('%Y-%m-%d %H:%M:%S')}", f"Kind    : {kind}", f"Thread  : {thread}", ""]
    lines += _versions() + ["", "--- Exception ---"]
    lines.append("".join(traceback.format_exception(et, ev, tb)).rstrip() if et else "(none)")
    lines += ["", "--- Project state ---"] + _state_lines()
    lines += ["", "--- Last Qt messages (oldest first) ---"] + (list(_S["qt"]) or ["(none)"])
    lines += ["", "--- Threads at report time ---"]
    try:
        for tid, fr in sys._current_frames().items():
            lines.append(f"Thread {tid}:")
            lines.append("".join(traceback.format_stack(fr)).rstrip())
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def _prune(d):
    try:
        files = sorted(glob.glob(os.path.join(d, "crash_*.log")))
        for f in files[:-CRASH_KEEP]:
            os.remove(f)
    except OSError:
        pass


# [MAP] Writes one report and returns its path (None if the same error already hit CRASH_SAME_MAX files this run, or the
# write failed). Safe to call from any thread and callable by hand: write_crash_report("manual", *sys.exc_info()).
def write_crash_report(kind, et=None, ev=None, tb=None, thread=None):
    try:
        thread = thread or threading.current_thread().name
        sig = (getattr(et, "__name__", "?"), "")
        if tb is not None:
            last = traceback.extract_tb(tb)[-1]
            sig = (sig[0], f"{last.filename}:{last.lineno}")
        with _S["lock"]:
            n = _S["seen"].get(sig, 0) + 1
            _S["seen"][sig] = n
            if n > CRASH_SAME_MAX:
                return None
            d = crash_dir()
            base = os.path.join(d, "crash_" + time.strftime("%Y%m%d_%H%M%S"))
            path, k = base + ".log", 1
            while os.path.exists(path):
                k += 1
                path = f"{base}_{k}.log"
            with open(path, "w", encoding="utf-8") as f:
                f.write(_report_text(kind, et, ev, tb, thread))
            _prune(d)
            return path
    except Exception:
        return None


def _notify(path):
    """Status-bar hint (GUI thread only - a worker thread must not touch widgets)."""
    try:
        w = _S["win"]
        if path and w is not None and threading.current_thread() is threading.main_thread():
            w.statusBar().showMessage(f"An error occurred - report saved to {path}", 15000)
    except Exception:
        pass


def _excepthook(et, ev, tb):
    if issubclass(et, KeyboardInterrupt):
        sys.__excepthook__(et, ev, tb)
        return
    path = write_crash_report("Uncaught exception", et, ev, tb)
    try:
        if sys.stderr is not None:
            traceback.print_exception(et, ev, tb)
    except Exception:
        pass
    _notify(path)


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    path = write_crash_report("Uncaught exception in a worker thread", args.exc_type, args.exc_value,
                              args.exc_traceback, getattr(args.thread, "name", None))
    try:
        if sys.stderr is not None:
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass


def _unraisable_hook(u):
    write_crash_report(f"Unraisable exception ({u.err_msg or 'no message'})", u.exc_type, u.exc_value, u.exc_traceback)


def _qt_handler(mode, ctx, msg):
    try:
        _S["qt"].append(f"{time.strftime('%H:%M:%S')} [{getattr(mode, 'name', mode)}] {msg}")
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
    except Exception:
        pass


# Native crash file: faulthandler keeps this handle open and writes into it from the signal handler. An empty file at the
# next start means the last run ended without a native crash (clean exit removes it; a kill leaves it empty) -> just delete.
def _arm_native():
    d = crash_dir()
    p = os.path.join(d, "native_crash.log")
    try:
        if os.path.isfile(p):
            if os.path.getsize(p) > 0:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
                stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(os.path.getmtime(p)))
                dst = os.path.join(d, f"crash_{stamp}_native.log")
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(f"{APP_NAME} crash report (NATIVE crash of the previous session)\n" + "=" * 60 + "\n"
                            f"Time    : {stamp}\n"
                            "The previous run died inside native code (segfault / abort - Qt, the media backend or a driver).\n"
                            "Python stack of every thread at that moment:\n\n" + body)
                _S["adopted"] = dst
                _prune(d)
            os.remove(p)
        _S["fh"] = open(p, "w")
        faulthandler.enable(file=_S["fh"], all_threads=True)
    except Exception:
        _S["fh"] = None


def _finalize():
    """Clean interpreter exit: no native crash happened -> drop the (empty) native log."""
    try:
        faulthandler.disable()
        if _S["fh"] is not None:
            _S["fh"].close()
            p = os.path.join(_S["dir"] or crash_dir(), "native_crash.log")
            if os.path.isfile(p) and os.path.getsize(p) == 0:
                os.remove(p)
    except Exception:
        pass


# [MAP] Call ONCE, first thing in main() (before QApplication, so start-up errors are caught too).
def install_crash_logging():
    if _S["installed"]:
        return
    _S["installed"] = True
    try:
        sys.excepthook = _excepthook
        threading.excepthook = _thread_hook
        sys.unraisablehook = _unraisable_hook
        try:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(_qt_handler)
        except Exception:
            pass
        _arm_native()
        atexit.register(_finalize)
    except Exception:
        pass


def crash_set_window(win):
    """Give the reports access to the project state; tells the user if the previous run crashed natively."""
    _S["win"] = win
    if _S["adopted"]:
        try:
            win.statusBar().showMessage(f"The last session crashed - report saved to {_S['adopted']}", 20000)
        except Exception:
            pass
