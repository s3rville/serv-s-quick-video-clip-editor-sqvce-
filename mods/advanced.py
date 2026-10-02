MOD_NAME = "Advanced"
MOD_TYPE = "mode"
MOD_ICON = "\u2699"
MOD_DESC = "Advanced editing mode (tab next to Video / GIF). Bundles: linked audio rows under the video row (right-click > Unlink audio) and a Properties panel (Clip options of the selected clip, applied live). Normal Video / GIF modes are unchanged."
MOD_AUTHOR = "SQVCE"
# Example "mode" mod that SHARES the editor window: build_mode returns "editor" (plugins.py [52.24]) instead of its own page.
# on_mode_shown -> switch the advanced features ON, on_mode_hidden (user went back to Video / GIF) -> switch them OFF again, so
# normal behaviour is untouched. Nothing here changes the data model: the linked-audio rows are DERIVED from Sequence.segs on every
# paint (they follow moves/trims for free), and the only edit - Unlink - uses ordinary Seg.mute + AudioTrack clips in ONE undo step.
# [INVARIANT] Unlink refuses clips that are not 1x forward (AClip has no speed/reverse) or that would overlap an audio clip.
# [PITFALL] AClip plays the media's default audio stream; per-stream choice (Seg.atracks) is not carried over.
# MEGA PLUGIN: this one mod bundles several features ("parts"), each a self-contained class below, all switched on in
# State.enable() and off in State.disable(): part 1 = LinkedAudio (timeline lane), part 3 = VideoTrack (overlay video rows), part 2 = PropertiesPanel (dock added with
# api.add_dock, i.e. only exists while the Advanced tab is selected). A new part = one class + one line in enable/disable.
# Properties re-uses ClipOptionsDialog as an embedded widget; the selected clip is polled (Timeline has no selection signal);
# edits are debounced (400 ms) and applied like MainWindow.edit_clip_options (grouped clips together, ONE seq.edit step).
# part 3 = overlay video tracks (see below). [TODO] overlay position/scale (PiP), text-layer tracks.
import os
import itertools

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, QObject, QUrl, QSizeF, QEvent
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QMenu, QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea, QFrame, QDialog
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

import utils
import probing
import export_worker
from media_model import Seg
from audio_track import AClip, AudioDialog, is_audio_media
from dialogs import ClipOptionsDialog

ROW_H = 14
S = None


class LinkedAudio:
    """Timeline lane (see Timeline.lanes API), kept at index 0 so it sits directly under the video row."""

    name = "Linked audio"                      # [52.25] row-header label (Timeline.rows)
    lock_key = "advanced:linked_audio"         # [52.25] stable padlock key (Timeline.lane_key)

    def __init__(self, api):
        self.api = api

    @staticmethod
    def _has_audio(s):
        return bool(s.media is not None and s.media.acodec.strip() and not getattr(s.media, "blank", False))

    def height(self, tl):
        n = max([max(1, len(s.media.audio_streams)) for s in tl.seq.segs if self._has_audio(s) and not s.mute] or [0])
        return n * ROW_H + 3 if n else 0

    def _rects(self, tl, y):
        """[(seg index, [QRectF per audio stream])] for every clip that still carries its audio."""
        out, st = [], tl.seq.starts()
        for k, s in enumerate(tl.seq.segs):
            if s.mute or not self._has_audio(s):
                continue
            x0, w = tl.tx(st[k]), max(2.0, s.dur * tl.pps)
            out.append((k, [QRectF(x0, y + 1 + i * ROW_H, w, ROW_H - 2) for i in range(max(1, len(s.media.audio_streams)))]))
        return out

    def _y0(self, tl):
        return tl.lane_y() + sum(int(l.height(tl)) for l in tl.lanes[:tl.lanes.index(self)])

    def paint(self, p, tl, y, W):
        if not self.height(tl):
            return
        fm, sel = p.fontMetrics(), set(tl.multi_sel) | {tl.sel}
        for k, rects in self._rects(tl, y):
            s = tl.seq.segs[k]
            for i, r in enumerate(rects):
                if r.right() < tl.HW or r.left() > W:
                    continue
                on = i >= len(s.atracks) or s.atracks[i]
                p.setPen(QPen(QColor("#101010"), 1))
                p.setBrush(QColor("#2f6f8f" if on else "#3a3f44"))
                p.drawRoundedRect(r, 3, 3)
                if r.width() > 40:
                    nm = (s.media.audio_streams[i].get("name") if i < len(s.media.audio_streams) else None) or f"A{i + 1}"
                    p.setPen(QColor("#0e1a22" if on else "#8a9096"))
                    p.drawText(QPointF(r.x() + 5, r.y() + ROW_H - 4), fm.elidedText(nm, Qt.TextElideMode.ElideRight, int(r.width() - 10)))
                if k in sel:
                    p.setPen(QPen(QColor("#ffffff"), 1.5))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(r, 3, 3)

    def press(self, e, tl):
        if tl.tool != "select":
            return False
        pos = e.position()
        k = next((k for k, rs in self._rects(tl, self._y0(tl)) if any(r.contains(pos) for r in rs)), None)
        if k is None:
            return False
        tl._keep_vsel = True                                   # this press selects the video clip; Timeline must not clear it
        if k not in tl.multi_sel and k != tl.sel or e.button() == Qt.MouseButton.LeftButton:
            tl.select_only(k)
        if e.button() == Qt.MouseButton.RightButton:
            idxs = sorted(set(tl.multi_sel) | {k}) if k in tl.multi_sel else [k]
            m = QMenu(tl)
            a = m.addAction("Unlink audio (to audio track)" if len(idxs) == 1 else f"Unlink audio of {len(idxs)} clips")
            if m.exec(tl.mapToGlobal(pos.toPoint())) is a:
                self.unlink(idxs)
        return True

    def move(self, pos, tl): pass
    def release(self, e, tl): pass
    def dbl(self, e, tl): pass

    def unlink(self, idxs):
        api = self.api
        seq, at, segs = api.seq, api.win.atrack, api.seq.segs
        if seq.locked & {"video", "audio"}:                    # [52.25] unlink edits BOTH rows
            api.status("Can't unlink: the Video or Audio row is locked (click its padlock).")
            return
        st = seq.starts()
        todo = [k for k in idxs if 0 <= k < len(segs) and not segs[k].mute and self._has_audio(segs[k])]
        if not todo:
            return
        if any(abs(segs[k].speed - 1.0) > 1e-6 or segs[k].rev for k in todo):
            api.status("Unlink needs clips at 1x forward speed (audio clips have no speed/reverse).")
            return
        new = [(segs[k], st[k]) for k in todo]

        def do():
            for s, t0 in new:
                s.mute = True
                k = at.free_track(t0, s.src_dur)                  # [52.26] first audio track with room, else a new track
                if k is None:
                    at.ntracks += 1
                    k = at.ntracks - 1
                at.items.append(AClip(s.media, t0, s.in_s, s.out_s, False, s.vol_db, track=k))
            at.sel = at.items[-1]
            at.changed()
            return True
        api.engine.pause()
        seq.edit(do)
        api.status("Audio unlinked - it is now a clip on the audio track." +
                   (" (Only the default audio stream is kept.)" if any(len(s.media.audio_streams) > 1 for s, _ in new) else ""))


# ----------------------------------------------------------------------------- part 2: Properties panel
class _Embedded(ClipOptionsDialog):
    """ClipOptionsDialog living inside a dock: no window centring, no Enter/Esc closing."""
    def showEvent(self, e):
        QWidget.showEvent(self, e)

    def keyPressEvent(self, e):
        QWidget.keyPressEvent(self, e)


class PropertiesPanel(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api, self.dlg, self.sig = api, None, None
        self.seg = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.msg = QLabel("No clip selected")
        self.msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg.setStyleSheet("color:#8a8a8a;")
        lay.addWidget(self.msg)
        self.sa = QScrollArea()
        self.sa.setWidgetResizable(True)
        self.sa.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(self.sa, 1)
        self.sa.hide()
        self.apply_t = QTimer(self)
        self.apply_t.setSingleShot(True)
        self.apply_t.setInterval(400)
        self.apply_t.timeout.connect(self.apply)
        self.poll_t = QTimer(self)
        self.poll_t.setInterval(150)
        self.poll_t.timeout.connect(self.poll)
        self.poll_t.start()

    # ---- selection -> panel
    def current(self):
        tl, segs = self.api.tl, self.api.seq.segs
        return segs[tl.sel] if 0 <= tl.sel < len(segs) else None

    @staticmethod
    def vals(s):
        return (s.mute, round(s.speed, 2), s.mirror, s.rev, tuple(s.atracks), round(s.vol_db, 2), s.track_type)

    def poll(self):
        if self.apply_t.isActive():                       # the user is mid-edit: don't rebuild under them
            return
        s = self.current()
        sig = None if s is None else (id(s), self.vals(s))
        if sig != self.sig:
            self.sig = sig
            self.build(s)

    def build(self, s):
        if self.dlg is not None:
            self.sa.takeWidget()
            self.dlg.deleteLater()
            self.dlg = None
        self.seg = s
        if s is None or getattr(s.media, "blank", False):
            self.sa.hide()
            self.msg.setText("No clip selected" if s is None else "Blank clip - double-click it to change its colour")
            self.msg.show()
            return
        d = _Embedded(self, s.media.name, s.mute, s.speed, bool(s.media.acodec.strip()), s.mirror, s.media.has_video,
                      s.rev, s.media.audio_streams, s.atracks, s.vol_db, s.track_type)
        d.setWindowFlags(Qt.WindowType.Widget)
        d.setModal(False)
        d.setMinimumWidth(0)
        for b in d.findChildren(QPushButton):             # live editing: Cancel/OK make no sense here, Reset stays
            if b.text() in ("OK", "Cancel"):
                b.hide()
        for sig in (d.mute.toggled, d.mirror.toggled, d.rev.toggled, d.slider.valueChanged, d.spin.valueChanged,
                    d.tracks.itemChanged, d.vol_slider.valueChanged, d.vol_spin.valueChanged,
                    d.track_type.currentIndexChanged):
            sig.connect(self.dirty)
        self.dlg = d
        self.msg.hide()
        self.sa.setWidget(d)
        self.sa.show()
        d.show()

    # ---- panel -> clip
    def dirty(self, *_):
        self.apply_t.start()

    def apply(self):
        d, s, win = self.dlg, self.seg, self.api.win
        if d is None or s is None or s not in self.api.seq.segs:
            return
        mute, speed, mirror, rev, atracks, vol_db, track_type = d.values()
        if (mute, speed, mirror, rev, atracks, vol_db, track_type) == self.vals(s):
            return
        group = [x for x in self.api.seq.segs if s.grp is not None and x.grp == s.grp] or [s]

        def do():
            for x in group:
                x.mute, x.speed, x.mirror, x.rev = mute, speed, mirror, rev
                x.atracks, x.vol_db, x.track_type = atracks, vol_db, track_type
            return True
        self.api.seq.edit(do)                              # undo + preview refresh + export re-encode switch, like the dialog
        self.sig = (id(s), self.vals(s))                   # our own change: no rebuild
        self.api.status("Clip options applied - clips with speed / mute / mirror / reverse are re-encoded on export.", 4000)


# ----------------------------------------------------------------------------- part 3: overlay video tracks (merged from video_track.py [52.28])
# "Video 2", "Video 3", ... : VClip(media, t0, in_s, out_s, mute, vol_db, grp, track) with absolute times (no ripple), never overlapping
# WITHIN a track, higher track drawn on top. 1x forward only, no crop/speed/mirror. History: seq.ext["video_overlay"].
# Timeline: VideoTrack is a LANE at tl.lanes[1]; the "+" sits on the BASE Video row (Timeline.vtrack / header_plus=False here).
# Clips get there by dropping files on an overlay row (drop_files), right-click base clip > "Send to overlay Video track",
# and back via right-click overlay clip > "Move to main video row"; dragging vertically moves between overlay tracks.
# Preview: per track a QMediaPlayer + QGraphicsVideoItem inside VideoView's scene (above the base item, below the freeze overlay),
# fitted to the view. Export: EXPORT_HOOKS[0] composites with ffmpeg overlay (scaled to the base size, centred) and mixes overlay audio;
# active only while the Advanced tab is on. [KNOWN] no stills / audio-only files, not in recovery.json, drop probing blocks the GUI briefly.
MIN_V = 0.3
_vids = itertools.count(1)


class VClip:
    def __init__(self, media, t0, in_s, out_s, mute=False, vol_db=0.0, grp=None, track=0):
        self.id = next(_vids)
        self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db = media, t0, in_s, out_s, mute, vol_db
        self.grp, self.track = grp, track

    dur = property(lambda s: s.out_s - s.in_s)
    end = property(lambda s: s.t0 + s.out_s - s.in_s)

    def tup(self):
        return (self.id, self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db, self.grp, self.track)


class _Layer(QObject):
    """Preview of ONE overlay track: own player + video item + audio output."""
    def __init__(self, vt, k):
        super().__init__(vt)
        self.vt, self.k = vt, k
        self.pl, self.ao = QMediaPlayer(self), QAudioOutput(self)
        self.pl.setAudioOutput(self.ao)
        self.item = QGraphicsVideoItem()
        self.item.setZValue(1 + k)
        self.item.setVisible(False)
        vt.view._scene.addItem(self.item)
        self.pl.setVideoOutput(self.item)
        self._src, self._go, self._pos, self._want_play = None, False, 0, False
        self.pl.mediaStatusChanged.connect(self._status)

    def layout(self):
        v = self.vt.view
        self.item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.item.setPos(0, 0)
        self.item.setSize(QSizeF(max(1, v.width()), max(1, v.height())))

    def dispose(self):
        self.pl.stop()
        self.vt.view._scene.removeItem(self.item)

    def reset(self):
        self.pl.stop()
        self._src = None
        self.item.setVisible(False)

    def follow(self, c, t, playing):
        if c is None:
            self.item.setVisible(False)
            if self.pl.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.pl.pause()
            return
        self.layout()
        self.item.setVisible(True)
        pos = int((c.in_s + t - c.t0) * 1000)
        self.ao.setVolume(0.0 if c.mute else max(0.0, min(1.0, 10 ** (c.vol_db / 20.0))))
        self._want_play = playing
        path = c.media.path
        if self._src != path:
            self._src, self._go, self._pos = path, True, pos
            self.pl.setSource(QUrl.fromLocalFile(path))
            return
        if self._go:
            self._pos = pos
            return
        if abs(self.pl.position() - pos) > (250 if playing else 40):
            self.pl.setPosition(pos)
        st = self.pl.playbackState()
        if playing and st != QMediaPlayer.PlaybackState.PlayingState:
            self.pl.play()
        elif not playing and st != QMediaPlayer.PlaybackState.PausedState:
            self.pl.pause()                                   # paused: shows the frame at the playhead

    def _status(self, st):
        if self._go and st in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._go = False
            self.pl.setPosition(self._pos)
            self.vt.sync()


class VideoTrack(QObject):
    H = 30
    name = "Video"
    lock_key = "video_overlay"
    header_plus = False                                       # the "+" is on the base Video row (Timeline.rows_ex)

    def __init__(self, win):
        super().__init__(win)
        self.win, self.tl, self.seq, self.eng = win, win.tl, win.seq, win.engine
        self.view = win.stage.video
        self.items, self.sel, self.selset, self.drag = [], None, set(), None
        self.ntracks = 0                                      # overlay tracks (rows); 0 = none until "+" / first send / drop
        self.enabled = False
        self.layers = []
        self.seq.ext["video_overlay"] = self
        self.view.installEventFilter(self)
        self.eng.playStateChanged.connect(self.sync)
        self.eng.playheadChanged.connect(self._tick)
        self.seq.edited.connect(self.sync)
        self.hook = _Export(self)
        export_worker.EXPORT_HOOKS.insert(0, self.hook)

    # ------------------------------------------------------------------ lifecycle / history
    def set_enabled(self, on):
        self.enabled = bool(on)
        self.tl.vtrack = self if on else None
        self.sync()

    def dispose(self):
        self.set_enabled(False)
        for ly in self.layers:
            ly.dispose()
        self.layers = []
        try:
            export_worker.EXPORT_HOOKS.remove(self.hook)
        except ValueError:
            pass
        self.seq.ext.pop("video_overlay", None)
        self.view.removeEventFilter(self)

    def eventFilter(self, o, e):
        if e.type() == QEvent.Type.Resize:
            for ly in self.layers:
                ly.layout()
        return False

    def ext_snapshot(self):
        return (self.ntracks, [c.tup() for c in self.items])

    def ext_restore(self, data):
        self.ntracks, rows = data
        self.items = []
        for i, m, t0, a, b, mu, vd, g, k in rows:
            c = VClip(m, t0, a, b, mu, vd, g, k)
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
        if self.ext_snapshot() != ext.get("video_overlay"):
            self.seq.commit(before)
            self.seq.edited.emit()

    def changed(self):
        self.items.sort(key=lambda c: c.t0)
        self.ntracks = max(self.ntracks, 1 + max([c.track for c in self.items], default=-1))
        self.tl.refresh_lanes()
        self.tl._invalidate_content()
        self.sync()

    def clear(self):
        self.items, self.sel, self.selset, self.ntracks = [], None, set(), 0
        for ly in self.layers:
            ly.reset()
        if self.enabled:
            self.changed()

    def status(self, msg, ms=4000):
        self.win.statusBar().showMessage(msg, ms)

    # ------------------------------------------------------------------ model helpers
    def at(self, t, k):
        return next((c for c in self.items if c.track == k and c.t0 - 1e-6 <= t < c.end), None)

    def limits(self, c):
        same = [o for o in self.items if o is not c and o.track == c.track]
        return (max([o.end for o in same if o.end <= c.t0 + 1e-6], default=0.0),
                min([o.t0 for o in same if o.t0 >= c.end - 1e-6], default=1e12))

    def free_at(self, k, t, dur, ignore=None):
        return not any(o is not ignore and o.track == k and o.t0 < t + dur - 1e-6 and o.end > t + 1e-6 for o in self.items)

    def free_track(self, t, dur):
        return next((k for k in range(self.ntracks) if self.free_at(k, t, dur)), None)

    def add_track(self):
        if "video_overlay" in self.seq.locked:
            self.seq.blocked.emit("video_overlay")
            return
        before = self.begin()
        self.ntracks += 1
        self.changed()
        self.record(before)

    def remove_track(self, k):
        if any(c.track == k for c in self.items):
            self.status("Only an empty video track can be removed - move or delete its clips first.")
            return
        before = self.begin()
        for c in self.items:
            if c.track > k:
                c.track -= 1
        self.ntracks = max(0, self.ntracks - 1)
        self.changed()
        self.record(before)

    # ------------------------------------------------------------------ moving between the base row and overlay tracks
    def send_seg(self, idx):
        """Base-row clip -> overlay track (same start time; the base row closes the gap, as with any ripple delete)."""
        seq = self.seq
        if not 0 <= idx < len(seq.segs):
            return
        if seq.locked & {"video", "video_overlay"}:
            seq.blocked.emit("video" if "video" in seq.locked else "video_overlay")
            return
        s = seq.segs[idx]
        if (abs(s.speed - 1.0) > 1e-6 or s.rev or s.mirror or abs(s.rot) > 1e-6 or s.xf is not None
                or getattr(s.media, "blank", False) or not s.media.has_video):
            self.status("Only plain clips (1x, forward, no crop/resize/mirror/rotate) can go to an overlay track.")
            return
        t0 = seq.starts()[idx]

        def do():
            k = self.free_track(t0, s.src_dur)
            if k is None:
                self.ntracks += 1
                k = self.ntracks - 1
            self.items.append(VClip(s.media, t0, s.in_s, s.out_s, s.mute, s.vol_db, None, k))
            seq.delete(idx)
            self.sel, self.selset = self.items[-1], {self.items[-1]}
            self.changed()
            return True
        self.eng.pause()
        seq.edit(do)

    def to_base(self, c):
        """Overlay clip -> main video row (inserted at its start time)."""
        if self.seq.locked & {"video", "video_overlay"}:
            self.seq.blocked.emit("video" if "video" in self.seq.locked else "video_overlay")
            return

        def do():
            self.items.remove(c)
            self.sel, self.selset = None, set()
            self.seq.insert_at(c.t0, Seg(c.media, c.in_s, c.out_s, mute=c.mute, vol_db=c.vol_db))
            self.changed()
            return True
        self.eng.pause()
        self.seq.edit(do)

    # ------------------------------------------------------------------ Timeline hooks
    def drop_files(self, paths, t, k):
        """Files dropped on overlay row k -> clips on that track (first free spot at/after the drop)."""
        before, cur, n = self.begin(), t, 0
        for p in paths:
            m = probing.probe_media(p)
            if m is None or not m.has_video or is_audio_media(m):
                self.status("Overlay tracks take video files only.")
                continue
            if m.is_image:
                self.status("Still images aren't supported on overlay tracks yet.")
                continue
            a, b = m.mark_in, m.mark_out
            for o in sorted((o for o in self.items if o.track == k), key=lambda c: c.t0):
                if o.end > cur and o.t0 < cur + (b - a):
                    cur = o.end
            c = VClip(m, cur, a, b, track=k)
            self.items.append(c)
            self.sel, self.selset, cur, n = c, {c}, c.end, n + 1
        if n:
            self.tl.sel, self.tl.multi_sel = -1, set()
            self.changed()
            self.record(before)
        return True

    def track_rows(self, tl):
        return [(f"Video {k + 2}", k * (self.H + 3), self.H) for k in range(self.ntracks)]

    def height(self, tl=None):
        return self.ntracks * (self.H + 3) if self.enabled else 0

    def _rect(self, tl, c, y):
        return QRectF(tl.tx(c.t0), y + c.track * (self.H + 3) + 1, max(2.0, c.dur * tl.pps), self.H - 2)

    def _y0(self, tl):
        return tl.lane_y() + sum(int(l.height(tl)) for l in tl.lanes[:tl.lanes.index(self)])

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
            p.setBrush(QColor("#4a5560" if c.mute else "#7b62b0"))
            p.drawRoundedRect(r, 3, 3)
            if r.width() > 24:
                p.setPen(QColor("#15101f"))
                p.drawText(QPointF(r.x() + 5, r.y() + 14),
                           fm.elidedText(("[muted] " if c.mute else "") + c.media.name, Qt.TextElideMode.ElideRight, int(r.width() - 10)))
            if c is self.sel or c in self.selset:
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r, 3, 3)

    def _hit(self, pos, tl):
        y = self._y0(tl)
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
        for ln in tl.lanes:
            if ln is not self:
                getattr(ln, "clear_sel", lambda t: None)(tl)
        if tl.atrack:
            tl.atrack.clear_sel(tl)
        self.sel, self.selset, self.drag = c, {c}, None
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(tl)
            a_opt, a_base = m.addAction("Options..."), m.addAction("Move to main video row")
            a_dup, a_del = m.addAction("Duplicate\tCtrl+D"), m.addAction("Delete")
            act = m.exec(e.globalPosition().toPoint())
            if act is a_opt:
                self.edit(c)
            elif act is a_base:
                self.to_base(c)
            elif act is a_dup:
                self.duplicate_key()
            elif act is a_del:
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
        if mode == "move":
            t = max(0.0, tl.snap_span(t0 + dt, c.dur))
            k = max(0, min(self.ntracks - 1, int((pos.y() - self._y0(tl)) // (self.H + 3))))
            if k != c.track and self.free_at(k, t, c.dur, ignore=c):     # drag vertically to another overlay track
                c.track, c.t0 = k, t
            else:
                c.t0 = max(lo, min(hi - c.dur, t))
        elif mode == "in":
            d = max(-a0, min(tl.snap_t(t0 + dt) - t0, b0 - a0 - MIN_V))
            d = max(d, lo - t0)
            c.t0, c.in_s = t0 + d, a0 + d
        else:
            e = tl.snap_t(t0 + (b0 - a0) + dt) - t0
            c.out_s = max(a0 + MIN_V, min(c.media.dur, a0 + e, a0 + (hi - t0)))
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
        self.edit(c)
        return True

    def cursor(self, pos, tl):
        c, edge = self._hit(pos, tl)
        return Qt.CursorShape.SizeHorCursor if (c and edge) else None

    def clear_sel(self, tl):
        self.sel, self.selset = None, set()

    def snap_points(self):
        return [x for c in self.items for x in (c.t0, c.end)]

    def edit(self, c):
        d = AudioDialog(self.win, c)                          # mute + volume
        d.setWindowTitle("Overlay clip options")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        before = self.begin()
        c.mute, c.vol_db = d.mute.isChecked(), d.vol.value()
        self.changed()
        self.record(before)

    def delete_key(self):
        if self.sel is None or self.sel not in self.items:
            return False
        before = self.begin()
        self.items.remove(self.sel)
        self.sel, self.selset = None, set()
        self.changed()
        self.record(before)
        return True

    def duplicate_key(self):
        c = self.sel
        if c is None or c not in self.items:
            return False
        t0 = c.end
        while not self.free_at(c.track, t0, c.dur):
            t0 = min(o.end for o in self.items if o.track == c.track and o.end > t0)
        before = self.begin()
        n = VClip(c.media, t0, c.in_s, c.out_s, c.mute, c.vol_db, None, c.track)
        self.items.append(n)
        self.sel, self.selset = n, {n}
        self.changed()
        self.record(before)
        return True

    # ------------------------------------------------------------------ preview
    def _tick(self, _t):
        if self.items and self.eng.playing:
            self.sync()

    def sync(self, *_):
        eng, t = self.eng, self.eng.playhead
        n = self.ntracks if (self.enabled and self.items) else 0
        while len(self.layers) < n:
            self.layers.append(_Layer(self, len(self.layers)))
        for k, ly in enumerate(self.layers):
            ly.follow(self.at(t, k) if k < n else None, t, bool(eng.playing))


class _Export:
    """EXPORT_HOOKS entry: composites every overlay clip over the exported video and mixes its audio in."""
    def __init__(self, vt):
        self.vt = vt

    def active(self, worker):
        return bool(utils.FFMPEG) and self.vt.enabled and bool(self.vt.items)

    def run(self, worker, src, tmpdir, ext):
        info = probing.probe_media(src)
        dur = info.dur if info else 0.0
        W, H = (info.w, info.h) if info and info.w and info.h else (1280, 720)
        W, H = W - W % 2, H - H % 2
        clips = sorted(self.vt.items, key=lambda c: (c.track, c.t0))
        args, chain, alab = ["-i", src], [], []
        norm = "aresample=48000,aformat=channel_layouts=stereo"
        if info and info.acodec.strip():
            chain.append(f"[0:a]{norm}[a0]")
            alab.append("[a0]")
        cur = "[0:v]"
        for n, c in enumerate(clips, 1):
            args += ["-i", c.media.path]
            t0, t1 = c.t0, c.end
            chain.append(f"[{n}:v]trim=start={c.in_s:.3f}:end={c.out_s:.3f},setpts=PTS-STARTPTS+{t0:.3f}/TB,"
                         f"scale={W}:{H}:force_original_aspect_ratio=decrease,format=yuva420p[o{n}]")
            chain.append(f"{cur}[o{n}]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass:enable='between(t,{t0:.3f},{t1:.3f})'[v{n}]")
            cur = f"[v{n}]"
            if not c.mute and c.media.acodec.strip():
                ms = int(round(t0 * 1000))
                chain.append(f"[{n}:a]atrim=start={c.in_s:.3f}:end={c.out_s:.3f},asetpts=PTS-STARTPTS,{norm},"
                             f"volume={c.vol_db:.2f}dB,adelay={ms}|{ms}[a{n}]")
                alab.append(f"[a{n}]")
        mix = len(alab) > 1 or (alab and not (info and info.acodec.strip()))
        if mix:
            chain.append("".join(alab) + f"amix=inputs={len(alab)}:duration=longest:dropout_transition=0:normalize=0[aout]")
        web = ext.lower() == ".webm"
        vc = ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"] if web else ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
        ac = ["-c:a", "libopus", "-b:a", "160k"] if web else ["-c:a", "aac", "-b:a", "192k"]
        amap = ["-map", "[aout]"] + ac if mix else (["-map", "0:a?", "-c:a", "copy"])
        tail = ["-t", f"{dur:.3f}"] if dur > 0 else []
        dst = os.path.join(tmpdir, "ovl_out" + ext)
        worker._ffmpeg(args + ["-filter_complex", ";".join(chain), "-map", cur] + amap + vc + tail + [dst])
        return dst


class State:
    def __init__(self, api):
        self.api, self.lane, self.on = api, LinkedAudio(api), False
        self.dock_key = "mod:advanced.py:properties"
        self.vt = None                                        # [52.27] part 3: overlay video tracks (merged class VideoTrack), created on first enable

    def _blocked(self, key):
        self.api.status("That row is locked - click its padlock in the timeline gutter to unlock it.", 3000)

    def enable(self):
        if self.on:
            return
        self.on = True
        tl = self.api.tl
        tl.lanes.insert(0, self.lane)
        tl.set_headers(True)                                  # [52.25] row names + padlocks in the left gutter
        if self.vt is None:
            self.vt = VideoTrack(self.api.win)
            self.api.win.vtrack_obj = self.vt
        if self.vt not in tl.lanes:
            tl.lanes.insert(1, self.vt)                       # right under Linked audio, above the other lanes
        self.vt.set_enabled(True)
        at = getattr(self.api.win, "atrack", None)
        if at is not None:
            at.show_empty = True                              # [52.26] Audio row (with its "+") is visible even before any audio is added
        self.api.seq.blocked.connect(self._blocked)
        self.api.seq.edited.connect(tl.refresh_lanes)         # lane height follows how many audio streams the clips carry
        tl.refresh_lanes()
        self.api.add_dock(self.dock_key, "Properties", PropertiesPanel(self.api))      # part 2

    def disable(self):
        if not self.on:
            return
        self.on = False
        tl = self.api.tl
        try:
            self.api.seq.edited.disconnect(tl.refresh_lanes)
        except Exception:
            pass
        try:
            self.api.seq.blocked.disconnect(self._blocked)
        except Exception:
            pass
        if self.vt is not None:
            self.vt.set_enabled(False)                        # hides rows, pauses previews, export hook goes inactive
            if self.vt in tl.lanes:
                tl.lanes.remove(self.vt)
        if self.lane in tl.lanes:
            tl.lanes.remove(self.lane)
        at = getattr(self.api.win, "atrack", None)
        if at is not None:
            at.show_empty = False
        tl.set_headers(False)                                 # also clears every lock (normal mode never has hidden locks)
        tl.refresh_lanes()
        self.api.remove_dock(self.dock_key)                   # part 2 (its timers die with the widget)


def on_load(api):
    global S
    S = State(api)


def build_mode(api):
    return "editor"                                            # share the main editor page (see plugins.py header)


def on_mode_shown(api):
    if S:
        S.enable()
        api.status("Advanced mode: linked audio rows under the video row (right-click > Unlink audio), Properties panel on the right.")


def on_mode_hidden(api):
    if S:
        S.disable()


def on_clear(api):
    if S and S.vt is not None:
        S.vt.clear()                                          # New project: drop overlay clips


def on_unload(api):
    global S
    if S:
        S.disable()
        if S.vt is not None:
            S.vt.dispose()
    S = None
