# ====================================================================================================
# recovery.py (v52.7) - crash recovery ("save aborted project"). Load order: after plugins.py, before main_window.py.
# NOT a project save: an autosave of the timeline + a restore prompt at the next start, so a crash / power cut / kill
# doesn't lose the edit. See the [MAP] notes on Recovery below and ARCHITECTURE.md (52.7).
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
                            QVariantAnimation, QEasingCurve, QStandardPaths)
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
from utils import _replace_with_retry       # underscore names are not exported by `import *`
from media_model import *
from probing import *
from export_worker import *
from dialogs import *
from widgets import *
from preview_stack import *
from timeline import *
from playback import *
from plugins import *

RECOVERY_FORMAT = "sqvce-recovery"
RECOVERY_VERSION = 1            # bump when the JSON layout changes; a file with another version is ignored + deleted
RECOVERY_DEBOUNCE_MS = 2000     # trailing debounce after the last COMMITTED edit
RECOVERY_MAX_WAIT_S = 10.0      # ...but never postpone the save longer than this while edits keep coming


# [MAP] Fixed location: <GenericDataLocation>/QuickCut/recovery.json (Windows %LOCALAPPDATA%, Linux ~/.local/share,
# macOS ~/Library/Application Support) - same "QuickCut" name as the QSettings org/app. GenericDataLocation on purpose:
# AppDataLocation depends on the application name, which this program never sets. Falls back to the temp folder.
def recovery_path():
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation) or tempfile.gettempdir()
    return os.path.join(base, "QuickCut", "recovery.json")


def _f(v, lo=-1e12, hi=1e12):
    """Validated finite number (bools rejected) or ValueError - the recovery file is untrusted input."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    x = float(v)
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError("number out of range")
    return x


def _parse_clip(c):
    """One saved clip -> dict of validated Seg arguments (+ 'media' = project key). Raises on anything odd."""
    if not isinstance(c, dict):
        raise ValueError("clip is not an object")
    key = c["media"]
    if not isinstance(key, str) or not key:
        raise ValueError("bad media key")
    xf = c.get("xf")
    if xf is not None:
        if not isinstance(xf, list) or len(xf) not in (6, 7, 11):
            raise ValueError("bad xf")
        for v in list(xf[:6]) + list(xf[7:]):
            _f(v)
        if len(xf) > 6 and not isinstance(xf[6], str):
            raise ValueError("bad xf colour")
        xf = tuple(xf)                      # Seg.xf is compared with == against tuples (Sequence.export_parts)
    grp = c.get("grp")
    if grp is not None and (isinstance(grp, bool) or not isinstance(grp, int)):
        raise ValueError("bad group id")
    at = c.get("atracks")
    at = [bool(a) for a in at] if isinstance(at, list) and at else None
    tt = c.get("track_type")
    return {"media": os.path.abspath(key), "in_s": _f(c["in_s"], 0.0), "out_s": _f(c["out_s"], 0.0), "xf": xf,
            "mute": bool(c.get("mute")), "speed": _f(c.get("speed", 1.0), 0.01, 1000.0),
            "mirror": bool(c.get("mirror")), "rot": _f(c.get("rot", 0.0), -1e6, 1e6), "rev": bool(c.get("rev")),
            "grp": grp, "atracks": at, "vol_db": _f(c.get("vol_db", 0.0), -400.0, 400.0),
            "track_type": tt if tt in ("stereo", "mono") else "stereo"}


# [MAP] Crash recovery. MainWindow creates ONE (`self.recovery`) after the Engine exists and wires three things:
#   autosave    Sequence.edited (COMMITTED edits only - never Sequence.live, so trim/move drags cost nothing) restarts a
#               2 s trailing debounce (capped at RECOVERY_MAX_WAIT_S). On expiry the payload dict is built on the GUI thread
#               (a few microseconds per clip) and handed to ONE daemon writer thread (latest payload wins); the thread does
#               json.dumps + write to recovery.json.tmp + fsync + os.replace, so a crash mid-write leaves the last good
#               file intact. Empty timeline -> the file is deleted instead. The "Auto-Saving N%" label (MainWindow.
#               autosave_lbl, next to "?") is shown only while a write runs (+0.7 s linger at 100 %).
#   clean exit  MainWindow.closeEvent -> shutdown_clean(): stop the timer, delete the file, never write again.
#   startup     MainWindow.__init__ -> singleShot(0) -> startup(files): if a file exists the last run ended abnormally ->
#               prompt Restore / Discard; corrupt / unknown-version / clip-less file = silently ignored + deleted. Autosave
#               is disarmed until this has finished (an early empty-timeline edit must not delete the file being offered).
#               The command-line files are imported AFTER the prompt.
# What is saved: clips (every Seg field, media by PROJECT KEY = the MainWindow.by_path key: for a baked still image that is
# the ORIGINAL picture, not the temp mp4), groups, Project-panel media + primary, playhead, Keyframes/Precise.
# NOT saved (regenerable): thumbnails, keyframes, reverse/scrub proxies, undo history, selection, zoom.
# [KNOWN] Groups (create/rename/recolour/ungroup), the mode button, the primary star and imports do not emit `edited`,
# so a change of only those is saved with the NEXT committed edit (the mode/primary/playhead are read at save time).
# [INVARIANT] Restore assigns seq.segs wholesale, so it clears both undo stacks, calls _invalidate_geometry and emits
# `edited` (same recipe as MainWindow.clear_project) - on_seq_edited then refreshes proxies/stage/engine/labels.
class Recovery(QObject):
    _progress = Signal(int)             # worker thread -> GUI thread (queued automatically)
    _finished = Signal(bool, str)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.path = recovery_path()
        self._armed = False             # False until startup() has dealt with an existing file
        self._closed = False
        self._gen = 0                   # bumped by discard(): queued / not-yet-started writes of older generations are dropped
        self._last_err = ""
        self._dirty_since = None
        self._cv = threading.Condition()
        self._slot = None               # (gen, payload) waiting for the writer thread - latest wins
        self._io_lock = threading.Lock()    # held by the writer for a whole write; discard() takes it to wait it out
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.setInterval(700)
        self._hide.timeout.connect(self._hide_label)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        win.seq.edited.connect(self._on_edited)          # committed edits only - deliberately NOT seq.live
        threading.Thread(target=self._writer_loop, daemon=True).start()

    # ------------------------------------------------------------------ autosave
    def _on_edited(self):
        if not self._armed or self._closed:
            return
        now = time.monotonic()
        if not self._timer.isActive():
            self._dirty_since = now
        waited = now - (self._dirty_since if self._dirty_since is not None else now)
        self._timer.start(int(max(0.0, min(RECOVERY_DEBOUNCE_MS / 1000.0, RECOVERY_MAX_WAIT_S - waited)) * 1000))

    def _flush(self):
        if self._closed or not self._armed:
            return
        try:
            if not self.win.seq.segs:
                self.discard()                 # nothing worth recovering
                return
            payload = self._build()
        except Exception:
            return                              # autosave must never take the editor down
        with self._cv:
            self._slot = (self._gen, payload)
            self._cv.notify()
        self._hide.stop()
        self._show_pct(0)

    # GUI thread. Plain dict/list/str/number/tuple values only, all copied here, so the writer thread never touches
    # live objects (Seg / Media / Sequence). Field order comes from Sequence.snapshot() == Seg.__init__ order.
    def _build(self):
        w, seq = self.win, self.win.seq
        keys = {m: k for k, m in w.by_path.items()}      # Media -> project key (Media is hashed by identity)
        key_of = lambda m: keys.get(m) or m.path
        clips = [{"media": key_of(m), "in_s": in_s, "out_s": out_s, "xf": list(xf) if xf else None, "mute": bool(mute),
                  "speed": speed, "mirror": bool(mirror), "rot": rot, "rev": bool(rev), "grp": grp,
                  "atracks": [bool(a) for a in atracks], "vol_db": vol_db, "track_type": track_type}
                 for (m, in_s, out_s, xf, mute, speed, mirror, rot, rev, grp, atracks, vol_db, track_type)
                 in seq.snapshot()]
        return {"format": RECOVERY_FORMAT, "version": RECOVERY_VERSION, "saved_at": time.time(),
                "medias": [key_of(m) for m in w.medias],
                "primary": key_of(w.primary) if w.primary is not None else None,
                "clips": clips,
                "groups": {str(g): dict(v) for g, v in seq.groups.items()},
                "next_grp": seq._next_grp,
                "playhead": float(w.engine.playhead),
                "keyframes": bool(seq.snap)}            # True = Keyframes mode, False = Precise

    def _writer_loop(self):
        while True:
            with self._cv:
                while self._slot is None:
                    self._cv.wait()
                gen, payload = self._slot
                self._slot = None
            try:
                with self._io_lock:
                    if gen == self._gen:
                        self._write(payload)
            except Exception as e:  # noqa: BLE001
                try:
                    os.remove(self.path + ".tmp")
                except OSError:
                    pass
                self._finished.emit(False, str(e))

    # Writer thread. tmp file in the SAME folder -> fsync -> os.replace (atomic on the same volume), so the previous
    # good recovery.json survives a kill/power cut at any point. Percent = bytes written (99 max until the replace).
    def _write(self, payload):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        total = len(data)
        step = max(1024, -(-total // 10))
        done = 0
        with open(tmp, "wb") as f:
            while done < total:
                f.write(data[done:done + step])
                done = min(total, done + step)
                self._progress.emit(min(99, done * 100 // total))
            f.flush()
            os.fsync(f.fileno())
        ok, err = _replace_with_retry(tmp, self.path)
        if not ok:
            raise OSError(err)
        self._progress.emit(100)
        self._finished.emit(True, "")

    def _lbl(self):
        return getattr(self.win, "autosave_lbl", None)

    def _show_pct(self, p):
        lb = self._lbl()
        if lb is not None:
            lb.setText(f"Auto-Saving {p}%")
            lb.show()

    def _hide_label(self):
        lb = self._lbl()
        if lb is not None:
            lb.hide()

    def _on_progress(self, p):
        if self._closed:
            return
        self._hide.stop()
        self._show_pct(p)
        if p >= 100:
            self._hide.start()

    def _on_finished(self, ok, err):
        if ok:
            self._last_err = ""
            return
        self._hide_label()
        if err != self._last_err and not self._closed:
            self._last_err = err
            self.win.statusBar().showMessage("Autosave failed: " + err, 6000)

    # ------------------------------------------------------------------ delete / clean exit
    def discard(self):
        """Delete the recovery file. Cancels queued writes and waits out a running one, so nothing can re-create it."""
        self._timer.stop()
        self._gen += 1
        with self._cv:
            self._slot = None
        with self._io_lock:
            for p in (self.path, self.path + ".tmp"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self._hide.stop()
        self._hide_label()

    def shutdown_clean(self):
        """Normal close (MainWindow.closeEvent, after the existing shutdown logic): delete the file, never write again."""
        self._closed = True
        self.discard()

    # ------------------------------------------------------------------ startup / restore
    def startup(self, files=None):
        """First event-loop turn after the window is shown. Prompt (if a file exists) BEFORE the command-line files."""
        try:
            data = self._read()
            if data is not None:
                if self._ask():
                    self._restore(data)
                else:
                    self.discard()
        except Exception as e:  # noqa: BLE001 - a broken recovery file must never stop the program starting
            try:
                self.win.statusBar().showMessage(f"Could not restore the last session: {e}", 8000)
            except Exception:
                pass
        finally:
            self._armed = True
        if files:
            self.win.import_paths(files)

    def _read(self):
        """Parsed file, or None. Missing = None. Corrupt / unknown version / no clips = deleted and ignored."""
        if not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ok = (isinstance(data, dict) and data.get("format") == RECOVERY_FORMAT
                  and type(data.get("version")) is int and data["version"] == RECOVERY_VERSION
                  and isinstance(data.get("clips"), list) and isinstance(data.get("medias"), list))
        except Exception:
            ok = False
        if not ok or not data["clips"]:
            self.discard()
            if not ok:
                self.win.statusBar().showMessage("Ignored an unreadable recovery file.", 6000)
            return None
        return data

    def _ask(self):
        box = QMessageBox(QMessageBox.Icon.Question, APP_NAME,
                          "SQVCE didn't close properly. Restore your last session?",
                          QMessageBox.StandardButton.NoButton, self.win)
        b_restore = box.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_restore)          # no Esc mapping on purpose: closing the box must not delete anything
        box.exec()
        return box.clickedButton() is b_restore

    # [MAP] Rebuild. import_paths() probes synchronously (parallel ffmpeg -i) and registers Media in by_path before it
    # returns, so "media ready" = right after that call (keyframes keep scanning in the background as after any import).
    # Clips are matched to their re-probed Media by project key; a clip is skipped when its file is missing / could not
    # be imported / no longer contains the saved range (in/out are clamped to the new duration first). Everything is
    # built into local lists first and only assigned at the end.
    def _restore(self, data):
        w, seq = self.win, self.win.seq
        clips, bad = [], 0
        for c in data["clips"]:
            try:
                clips.append(_parse_clip(c))
            except Exception:
                bad += 1
        keys = []
        for k in [m for m in data["medias"] if isinstance(m, str)] + [c["media"] for c in clips]:
            ap = os.path.abspath(k)
            if ap not in keys:
                keys.append(ap)
        present = [k for k in keys if os.path.isfile(k)]
        w.engine.pause()
        if present:
            w.import_paths(present)
        by = w.by_path
        segs, skipped = [], {}
        for c in clips:
            k = c["media"]
            m = by.get(k)
            why = None
            if m is None:
                why = "file missing" if k not in present else "could not be imported"
            else:
                out_s = min(c["out_s"], m.dur)
                in_s = max(0.0, min(c["in_s"], out_s))
                if out_s - in_s < MIN_DUR:
                    why = "file changed - the clip no longer fits"
            if why:
                e = skipped.setdefault(k, [0, why])
                e[0] += 1
                continue
            at = c["atracks"]
            if at is not None and len(at) != max(1, len(m.audio_streams)):
                at = None                          # the file's audio layout changed: back to "every track"
            segs.append(Seg(m, in_s, out_s, c["xf"], c["mute"], c["speed"], c["mirror"], c["rot"], c["rev"],
                            c["grp"], at, c["vol_db"], c["track_type"]))
        if segs:
            used = {s.grp for s in segs if s.grp is not None}
            src = data.get("groups") if isinstance(data.get("groups"), dict) else {}
            groups = {}
            for gid in used:
                g = src.get(str(gid)) if isinstance(src.get(str(gid)), dict) else {}
                col, name = g.get("color"), g.get("name")
                groups[gid] = {"color": col if isinstance(col, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", col) else "#888888",
                               "name": name if isinstance(name, str) else f"Group{gid}"}
            ng = data.get("next_grp")
            seq.segs = segs
            seq.groups.clear()
            seq.groups.update(groups)
            seq._next_grp = max(ng if isinstance(ng, int) and not isinstance(ng, bool) else 1, max(groups) + 1 if groups else 1)
            seq.undo_stack.clear()                 # restored state = clean starting point (as in clear_project)
            seq.redo_stack.clear()
            seq._invalidate_geometry()
            w.tl.sel = -1
            w.tl.multi_sel.clear()
            seq.edited.emit()                      # -> MainWindow.on_seq_edited: proxies, stage, engine.seek, labels
            kf = data.get("keyframes")
            if FFPROBE and isinstance(kf, bool) and kf != bool(seq.snap):
                w.set_trim_mode(kf)
            pk = data.get("primary")
            if isinstance(pk, str) and os.path.abspath(pk) in by:
                w.set_primary(by[os.path.abspath(pk)])
            w.tl.zoom_fit(animate=False)
            try:
                t = _f(data.get("playhead", 0.0), 0.0)
            except Exception:
                t = 0.0
            w.engine.seek(max(0.0, min(t, seq.total())), play=False)
            self._timer.start(RECOVERY_DEBOUNCE_MS)    # refresh the file so it matches what is really on the timeline now
        if skipped or bad or not segs:
            lines = [f"  {os.path.normpath(k)} - {n} clip{'s' if n != 1 else ''} ({why})" for k, (n, why) in skipped.items()]
            if bad:
                lines.append(f"  {bad} damaged clip entr{'y' if bad == 1 else 'ies'} in the recovery file")
            head = (f"Restored {len(segs)} clip{'s' if len(segs) != 1 else ''}. These were skipped:" if segs
                    else "Nothing could be restored:")
            more = f"\n  ...and {len(lines) - 15} more" if len(lines) > 15 else ""
            QMessageBox.information(w, "Session restore", head + "\n\n" + "\n".join(lines[:15]) + more)
        else:
            w.statusBar().showMessage(f"Restored your last session ({len(segs)} clips).", 6000)
