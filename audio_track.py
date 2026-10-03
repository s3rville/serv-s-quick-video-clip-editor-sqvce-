# ====================================================================================================
# audio_track.py (v52.19) - AUDIO TRACK: audio files dropped on the timeline live in ONE thin strip ABOVE the video row.
# Load order: after plugins.py, before recovery.py / main_window.py.  Wiring: MainWindow creates `self.atrack = AudioTrack(self)`.
#   - Model: AClip(media, t0, in_s, out_s, mute, vol_db, grp, track); clips never overlap WITHIN a track [52.26: several tracks, add_track/remove_track, drag between rows]. History rides in Sequence snapshots (seq.ext).
#   - Timeline: the object is Timeline.atrack (paint/press/move/release/dbl/cursor/clear_sel/delete_key/nav_edit/snap_points);
#     Timeline.V_Y moves down by height() while the track has clips (Timeline.refresh_lanes).
#   - Preview: its own QMediaPlayer follows the Engine (playStateChanged / playheadChanged). Gain is capped at 0 dB in preview.
#   - Export: EXPORT_HOOKS pass after step C - clips are trimmed, delayed, gained and amix-ed into the file's audio.
# [KNOWN] Not saved in recovery.json. Preview does not feed the VU meter.
# ====================================================================================================

import os
import bisect
import itertools

from PySide6.QtCore import Qt, QUrl, QObject, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QMenu, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QDoubleSpinBox,
                               QDialogButtonBox, QSlider, QStyle)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

import utils
import probing
import export_worker

AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma")
MIN_A = 0.3
_ids = itertools.count(1)


def is_audio_media(m):
    """Audio-only file (or a music file with cover art): goes to the audio track instead of the video row."""
    return (not m.has_video) or os.path.splitext(m.path)[1].lower() in AUDIO_EXTS


def _num(v, lo=-1e12, hi=1e12):
    """[52.31] Validated finite number from the (untrusted) recovery file, or ValueError."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= float(v) <= hi:
        raise ValueError("bad number")
    return float(v)


class AClip:
    def __init__(self, media, t0, in_s, out_s, mute=False, vol_db=0.0, grp=None, track=0):
        self.id = next(_ids)
        self.grp = grp                                    # [52.22] cross-layer group id
        self.track = track                                # [52.26] audio row index (0 = top); clips never overlap WITHIN a track
        self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db = media, t0, in_s, out_s, mute, vol_db

    dur = property(lambda s: s.out_s - s.in_s)
    end = property(lambda s: s.t0 + s.out_s - s.in_s)

    def tup(self):
        return (self.id, self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db, self.grp, self.track)


class _ClickSlider(QSlider):
    """[52.22] Click anywhere on the groove to jump there (and drag from it); double-click resets to 0."""
    def _jump(self, e):
        self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()), max(1, self.width())))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._jump(e)
            e.accept()

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self._jump(e)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)
            e.accept()

    def mouseDoubleClickEvent(self, e):
        self.setValue(0)
        e.accept()


class AudioDialog(QDialog):
    """Audio-only clip options: mute + volume."""
    def __init__(self, parent, clip):
        super().__init__(parent)
        self.setWindowTitle("Audio clip options")
        v = QVBoxLayout(self)
        t = QLabel(self.fontMetrics().elidedText(clip.media.name, Qt.TextElideMode.ElideMiddle, 320))
        t.setStyleSheet("color:#eaeaea;font-weight:600;")
        v.addWidget(t)
        self.mute = QCheckBox("Mute this clip")
        self.mute.setChecked(clip.mute)
        v.addWidget(self.mute)
        row = QHBoxLayout()
        row.addWidget(QLabel("Volume"))
        self.sl = _ClickSlider(Qt.Orientation.Horizontal)          # [52.20] slider + exact input, kept in sync
        self.sl.setRange(-600, 120)                           # tenths of a dB
        self.sl.setMinimumWidth(160)
        self.sl.setToolTip("Click anywhere to jump - double-click to reset to 0 dB")
        row.addWidget(self.sl, 1)
        self.vol = QDoubleSpinBox()
        self.vol.setRange(-60.0, 12.0)
        self.vol.setDecimals(1)
        self.vol.setSingleStep(1.0)
        self.vol.setSuffix(" dB")
        self.vol.setValue(clip.vol_db)
        self.vol.setToolTip("Preview is capped at 0 dB; the export applies the full gain.")
        self.sl.setValue(int(round(clip.vol_db * 10)))
        self.sl.valueChanged.connect(lambda x: (self.vol.blockSignals(True), self.vol.setValue(x / 10.0), self.vol.blockSignals(False)))
        self.vol.valueChanged.connect(lambda x: (self.sl.blockSignals(True), self.sl.setValue(int(round(x * 10))), self.sl.blockSignals(False)))
        row.addWidget(self.vol)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class AudioTrack(QObject):
    H = 20

    def __init__(self, win):
        super().__init__(win)
        self.win, self.items, self.sel, self.drag = win, [], None, None
        self.selset = set()                                   # [52.21] box select can pick several clips (sel = primary)
        self.tl, self.seq, self.eng = win.tl, win.seq, win.engine
        self.tl.atrack = self
        self.seq.ext["audio_track"] = self
        self.ntracks, self.show_empty = 1, False              # [52.26] rows in the strip; show_empty: Advanced mode shows row 1 even with no clips
        self.voices = []                                      # [52.26] one preview player per track (clips on different tracks may overlap)
        self._voice(0)
        self.eng.playStateChanged.connect(self.sync)
        self.eng.playheadChanged.connect(self._tick)
        self.seq.edited.connect(self.sync)
        self.hook = _Export(self)
        export_worker.EXPORT_HOOKS.append(self.hook)

    # ------------------------------------------------------------------ history
    # ---- [52.31] crash recovery (see recovery.py: Recovery._build / _restore). JSON-able dicts; media by project key.
    def recovery_export(self, key_of):
        if not self.items:
            return None
        return {"ntracks": self.ntracks, "media_keys": sorted({key_of(c.media) for c in self.items}),
                "items": [{"media": key_of(c.media), "t0": c.t0, "in_s": c.in_s, "out_s": c.out_s, "mute": bool(c.mute),
                           "vol_db": c.vol_db, "grp": c.grp, "track": c.track} for c in self.items]}

    def recovery_import(self, data, by_path):
        items = []
        for d in data.get("items", []):
            try:
                m = by_path.get(os.path.abspath(d["media"]))
                if m is None:
                    continue
                a = max(0.0, _num(d["in_s"], 0.0))
                b = min(_num(d["out_s"], 0.0), m.dur)
                if b - a < 0.05:
                    continue
                g = d.get("grp")
                c = AClip(m, _num(d["t0"], 0.0), a, b, bool(d.get("mute")), _num(d.get("vol_db", 0.0), -400.0, 400.0),
                          g if isinstance(g, int) and not isinstance(g, bool) else None, int(_num(d.get("track", 0), 0, 99)))
                items.append(c)
            except Exception:
                continue
        if not items:
            return 0
        self.items, self.sel, self.selset = items, None, set()
        try:
            self.ntracks = max(1, int(_num(data.get("ntracks", 1), 1, 99)))
        except Exception:
            self.ntracks = 1
        self.changed()
        return len(items)

    pl = property(lambda s: s.voices[0].pl)                   # [52.26] kept for old callers

    def ext_snapshot(self):
        return (self.ntracks, [c.tup() for c in self.items])  # [52.26] was a bare list; ext_restore accepts both

    def ext_restore(self, data):
        if isinstance(data, tuple):
            self.ntracks, data = data
        else:
            self.ntracks = 1
        self.items = []
        for row in data:
            i, m, t0, a, b, mu, vd, g = row[:8]
            c = AClip(m, t0, a, b, mu, vd, g, row[8] if len(row) > 8 else 0)
            c.id = i
            self.items.append(c)
        ids = {c.id for c in self.selset}
        self.sel = next((c for c in self.items if self.sel is not None and c.id == self.sel.id), None)
        self.selset = {c for c in self.items if c.id in ids}
        self.changed()

    def begin(self):
        return self.seq.snapshot()

    def record(self, before):
        ext = getattr(before, "ext", None) or {}
        if self.ext_snapshot() != ext.get("audio_track"):
            self.seq.commit(before)
            self.seq.edited.emit()

    def changed(self):
        self.items.sort(key=lambda c: c.t0)
        self.ntracks = max(1, self.ntracks, 1 + max([c.track for c in self.items], default=0))
        self.tl.refresh_lanes()
        self.tl._invalidate_content()
        self.sync()

    def clear(self):
        self.items, self.sel, self.selset, self.ntracks = [], None, set(), 1
        for v in self.voices:
            v.reset()
        self.changed()

    # ------------------------------------------------------------------ model helpers
    def at(self, t, track=None):
        for c in self.items:
            if (track is None or c.track == track) and c.t0 - 1e-6 <= t < c.end:
                return c
        return None

    def limits(self, c):
        same = [o for o in self.items if o is not c and o.track == c.track]
        prev = max([o.end for o in same if o.end <= c.t0 + 1e-6], default=0.0)
        nxt = min([o.t0 for o in same if o.t0 >= c.end - 1e-6], default=1e12)
        return prev, nxt

    # [52.26] multi-track helpers
    def free_at(self, k, t, dur, ignore=None):
        return not any(o is not ignore and o.track == k and o.t0 < t + dur - 1e-6 and o.end > t + 1e-6 for o in self.items)

    def free_track(self, t, dur):
        return next((k for k in range(self.ntracks) if self.free_at(k, t, dur)), None)

    def add_track(self):
        if "audio" in self.seq.locked:
            self.seq.blocked.emit("audio")
            return
        before = self.begin()
        self.ntracks += 1
        self.changed()
        self.record(before)

    def remove_track(self, k):
        if any(c.track == k for c in self.items):
            self.win.statusBar().showMessage("Only an empty audio track can be removed - move or delete its clips first.", 4000)
            return
        if self.ntracks <= 1:
            return
        before = self.begin()
        for c in self.items:
            if c.track > k:
                c.track -= 1
        self.ntracks -= 1
        self.changed()
        self.record(before)

    def track_rows(self, tl):
        """Header rows for Timeline.rows_ex: [(label, y offset inside the strip, height)]."""
        return [("Audio" if self.ntracks == 1 else f"Audio {k + 1}", k * (self.H + 3), self.H) for k in range(self.ntracks)]

    def add(self, medias, t):
        before, cur = self.begin(), t
        for m in medias:
            a, b = m.mark_in, m.mark_out
            k = self.free_track(cur, b - a)                        # [52.26] a free row at the cursor wins over shifting right
            if k is None:
                k = 0
                for o in sorted((o for o in self.items if o.track == 0), key=lambda c: c.t0):   # first free spot at/after the cursor
                    if o.end > cur and o.t0 < cur + (b - a):
                        cur = o.end
            c = AClip(m, cur, a, b, track=k)
            self.items.append(c)
            cur = c.end
            self.sel = c
        self.tl.sel, self.tl.multi_sel = -1, set()
        self.changed()
        self.record(before)

    def delete_key(self):
        if self.sel is None or self.sel not in self.items:
            return False
        before = self.begin()
        for c in (self.selset | {self.sel}):
            if c in self.items:
                self.items.remove(c)
        self.sel, self.selset = None, set()
        self.changed()
        self.record(before)
        return True

    def duplicate_key(self):
        """[52.20] Ctrl+D: copy of the selected clip in the next free space after it (never stacks; shortened to fit)."""
        c = self.sel
        if c is None or c not in self.items:
            return False
        t0, want, tot = c.end, c.dur, self.seq.total()
        for _ in range(len(self.items) + 2):
            hit = next((o for o in sorted(self.items, key=lambda o: o.t0) if o.track == c.track and o.t0 < t0 + want - 1e-9 and o.end > t0 + 1e-9), None)
            if hit is None:
                break
            if hit.t0 <= t0 or hit.t0 - t0 < MIN_A:
                t0, want = hit.end, c.dur
            else:
                want = hit.t0 - t0
        if tot > 0:
            want = min(want, tot - t0)
        if want < MIN_A - 1e-9:
            self.win.statusBar().showMessage("No room left on the audio track for a duplicate.", 4000)
            return True
        before = self.begin()
        n = AClip(c.media, t0, c.in_s, c.in_s + want, c.mute, c.vol_db, track=c.track)
        self.items.append(n)
        self.sel = n
        self.changed()
        self.record(before)
        return True

    def edit(self, c):
        d = AudioDialog(self.win, c)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        before = self.begin()
        c.mute, c.vol_db = d.mute.isChecked(), d.vol.value()
        self.changed()
        self.record(before)

    # ------------------------------------------------------------------ timeline lane API
    def height(self, tl=None):
        n = self.ntracks if (self.items or self.show_empty or self.ntracks > 1) else 0
        return n * (self.H + 3)

    def _rect(self, tl, c, y):
        return QRectF(tl.tx(c.t0), y + c.track * (self.H + 3) + 1, max(2.0, c.dur * tl.pps), self.H - 2)

    def paint(self, p, tl, y, W):
        if not self.height(tl):
            return
        for k in range(self.ntracks):
            p.fillRect(QRectF(tl.HW, y + k * (self.H + 3), W - tl.HW, self.H), QColor("#181818"))
        fm = p.fontMetrics()
        for c in self.items:
            r = self._rect(tl, c, y)
            if r.right() < tl.HW or r.left() > W:
                continue
            p.setPen(QPen(QColor("#101010"), 1))
            p.setBrush(QColor("#4a5560" if c.mute else "#3f8f7a"))
            p.drawRoundedRect(r, 3, 3)
            if c.grp is not None:                                  # [52.22] group colour bar
                p.fillRect(QRectF(r.left() + 2, r.bottom() - 3, max(1.0, r.width() - 4), 2),
                           QColor(tl.seq.groups.get(c.grp, {}).get("color", "#888888")))
            if r.width() > 24:
                p.setPen(QColor("#0e1a16"))
                p.drawText(QPointF(r.x() + 5, r.y() + 14),
                           fm.elidedText(("[muted] " if c.mute else "") + c.media.name, Qt.TextElideMode.ElideRight,
                                         int(r.width() - 10)))
            if c is self.sel or c in self.selset:
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r, 3, 3)

    def _hit(self, pos, tl):
        y = tl.RULER_H + 2
        for c in self.items:
            r = self._rect(tl, c, y)
            if r.contains(pos):
                edge = "in" if r.width() > 20 and pos.x() - r.left() < 6 else "out" if r.width() > 20 and r.right() - pos.x() < 6 else None
                return c, edge
        return None, None

    def press(self, e, tl):
        c, edge = self._hit(e.position(), tl)
        if c is None:
            return False
        if e.button() == Qt.MouseButton.RightButton and c in self.selset:
            tl._keep_vsel = True                               # [52.22] keep a multi-layer selection for the menu
            self.sel = c
        else:
            for ln in tl.lanes:                                # one selection across all layers
                getattr(ln, "clear_sel", lambda t: None)(tl)
            self.sel, self.selset = c, {c}
            if c.grp is not None and not edge:
                tl.select_group(c.grp)                         # [52.22] a grouped clip selects its whole group
        self.drag = None
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(tl)
            a_opt, a_dup, a_del = m.addAction("Options..."), m.addAction("Duplicate\tCtrl+D"), m.addAction("Delete")
            a_grp = m.addAction("Group Selected Clips") if tl._group_plan() else None
            a_ung = m.addAction("Ungroup") if tl._sel_gid() is not None else None
            act = m.exec(e.globalPosition().toPoint())
            if act is not None and act is a_grp:
                tl.group_selected()
            elif act is not None and act is a_ung:
                tl.ungroup_selected()
            if act == a_opt:
                self.edit(c)
            elif act == a_dup:
                self.duplicate_key()
            elif act == a_del:
                self.delete_key()
            return True
        self.drag = (edge or "move", c, e.position().x(), c.t0, c.in_s, c.out_s, self.begin())
        return True

    def move(self, pos, tl):
        if not self.drag:
            return
        mode, c, x0, t0, a0, b0, _ = self.drag
        dt = (pos.x() - x0) / tl.pps
        lo, hi = self.limits(c)
        if mode == "move":                                        # [52.20] start/end snap to video clip edges
            t = max(0.0, tl.snap_span(t0 + dt, c.dur))
            k = max(0, min(self.ntracks - 1, int((pos.y() - (tl.RULER_H + 2)) // (self.H + 3))))
            if k != c.track and self.free_at(k, t, c.dur, ignore=c):   # [52.26] drag vertically to another audio track (if it fits there)
                c.track, c.t0 = k, t
                lo, hi = self.limits(c)
            else:
                c.t0 = max(lo, min(hi - c.dur, t))
        elif mode == "in":
            dt = tl.snap_t(t0 + dt) - t0
            d = max(-a0, min(dt, b0 - a0 - MIN_A))
            d = max(d, lo - t0)
            c.t0, c.in_s = t0 + d, a0 + d
        else:
            e = tl.snap_t(t0 + (b0 - a0) + dt) - t0                 # new length
            c.out_s = max(a0 + MIN_A, min(c.media.dur, a0 + e, a0 + (hi - t0)))
        tl._invalidate_content()

    def release(self, e, tl):
        d, self.drag = self.drag, None
        if d:
            self.changed()
            self.record(d[6])

    def dbl(self, e, tl):
        c, _ = self._hit(e.position(), tl)
        if c is None:
            return False
        self.sel = c
        tl._invalidate_content()
        self.edit(c)
        return True

    def cursor(self, pos, tl):
        c, edge = self._hit(pos, tl)
        return Qt.CursorShape.SizeHorCursor if (c and edge) else None

    def clear_sel(self, tl):
        if self.sel is not None or self.selset:
            self.sel, self.selset = None, set()

    # [52.21] box select + selection-layer / Alt+Left/Right API (see MainWindow.move_clip)
    def marquee(self, r, tl, add):
        y = tl.RULER_H + 2
        hit = {c for c in self.items if self._rect(tl, c, y).intersects(r)}
        new = hit | (self.selset if add else set())
        if new != self.selset:
            self.selset, self.sel = new, next(iter(new), None)
            tl._invalidate_content()

    # [52.22] generic lane API for cross-layer groups
    def sel_items(self):
        return self._group()

    def all_items(self):
        return self.items

    def select_grp(self, gid, add=False):
        g = {c for c in self.items if c.grp == gid}
        new = (self.selset | g) if add else g
        if new != self.selset:
            self.selset, self.sel = new, next(iter(new), None)
            self.tl._invalidate_content()

    def sel_layers(self):
        return {"audio"} if (self.selset or self.sel is not None) else set()

    def _group(self):
        return sorted((self.selset | ({self.sel} if self.sel else set())) & set(self.items), key=lambda c: c.t0)

    def shift_sel(self, dt, dry=False):
        g = self._group()
        for c in g:
            if c.t0 + dt < -1e-9 or any(o not in g and o.track == c.track and o.t0 < c.end + dt - 1e-9 and o.end > c.t0 + dt + 1e-9 for o in self.items):
                return False
        if not dry:
            for c in g:
                c.t0 = max(0.0, c.t0 + dt)
            self.changed()
        return True

    def hop_sel(self, d):
        g = self._group()
        if not g:
            return False
        if len({c.track for c in g}) != 1:
            return False
        items = sorted((o for o in self.items if o.track == g[0].track), key=lambda c: c.t0)
        idx = [items.index(c) for c in g]
        if idx != list(range(idx[0], idx[-1] + 1)):
            return False
        j = idx[-1] + 1 if d > 0 else idx[0] - 1
        if not 0 <= j < len(items):
            return False
        n, g0, g1 = items[j], g[0].t0, g[-1].end
        if d > 0:
            dx, n.t0 = n.end - g1, g0
        else:
            dx, n.t0 = n.t0 - g0, g1 - n.dur
        for c in g:
            c.t0 += dx
        self.changed()
        return True

    def snap_points(self):
        return [x for c in self.items for x in (c.t0, c.end)]

    def nav_edit(self, d):
        """Up/Down with an audio clip selected: previous/next audio clip (see MainWindow.goto_edit)."""
        if self.sel is None or self.sel not in self.items:
            return None
        i = self.items.index(self.sel)
        tgt = self.items[max(0, min(len(self.items) - 1, i + d))]
        self.sel = tgt
        self.tl._invalidate_content()
        return tgt.t0

    # ------------------------------------------------------------------ preview playback (one player per track, follows the Engine)
    def _voice(self, k):
        while len(self.voices) <= k:
            self.voices.append(_Voice(self))
        return self.voices[k]

    def _tick(self, _t):
        if self.items and self.eng.playing:
            self.sync()

    def sync(self, *_):
        eng, t = self.eng, self.eng.playhead
        for k in range(max(self.ntracks, len(self.voices))):
            c = self.at(t, k) if (self.items and k < self.ntracks) else None
            self._voice(k).follow(c if (eng.playing and c is not None and not c.mute) else None, t)


class _Voice(QObject):
    """[52.26] One preview player (the old single-player logic of AudioTrack.sync, per track)."""
    def __init__(self, at):
        super().__init__(at)
        self.at = at
        self.pl = QMediaPlayer(self)
        self.ao = QAudioOutput(self)
        self.pl.setAudioOutput(self.ao)
        self._src, self._go, self._pos = None, False, 0
        self.pl.mediaStatusChanged.connect(self._status)

    def reset(self):
        self.pl.stop()
        self._src = None

    def follow(self, c, t):
        if c is None:
            if self.pl.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.pl.pause()
            return
        pos = int((c.in_s + t - c.t0) * 1000)
        self.ao.setVolume(max(0.0, min(1.0, 10 ** (c.vol_db / 20.0))))
        path = c.media.path
        if self._src != path:
            self._src, self._go, self._pos = path, True, pos
            self.pl.setSource(QUrl.fromLocalFile(path))
            return
        if self._go:
            self._pos = pos
            return
        if abs(self.pl.position() - pos) > 250:
            self.pl.setPosition(pos)
        if self.pl.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.pl.play()

    def _status(self, st):
        if self._go and st in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._go = False
            self.pl.setPosition(self._pos)
            self.at.sync()


class _Export:
    """EXPORT_HOOKS entry: mixes every unmuted audio clip into the exported file's audio."""
    def __init__(self, at):
        self.at = at

    def active(self, worker):
        return bool(utils.FFMPEG) and any(not c.mute for c in self.at.items)

    def run(self, worker, src, tmpdir, ext):
        info = probing.probe_media(src)
        dur = info.dur if info else 0.0
        clips = [c for c in self.at.items if not c.mute]
        args, chain, labels = ["-i", src], [], []
        norm = "aresample=48000,aformat=channel_layouts=stereo"
        if info and info.acodec.strip():
            chain.append(f"[0:a]{norm}[a0]")
            labels.append("[a0]")
        for n, c in enumerate(clips, 1):
            args += ["-i", c.media.path]
            ms = int(round(c.t0 * 1000))
            chain.append(f"[{n}:a]atrim=start={c.in_s:.3f}:end={c.out_s:.3f},asetpts=PTS-STARTPTS,{norm},"
                         f"volume={c.vol_db:.2f}dB,adelay={ms}|{ms}[a{n}]")
            labels.append(f"[a{n}]")
        chain.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0[aout]")
        dst = os.path.join(tmpdir, "aud_out" + ext)
        ac = ["-c:a", "libopus", "-b:a", "160k"] if ext.lower() == ".webm" else ["-c:a", "aac", "-b:a", "192k"]
        tail = ["-t", f"{dur:.3f}"] if dur > 0 else []
        worker._ffmpeg(args + ["-filter_complex", ";".join(chain), "-map", "0:v?", "-map", "[aout]", "-c:v", "copy"]
                       + ac + tail + [dst])
        return dst
