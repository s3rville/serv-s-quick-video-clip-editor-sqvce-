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
from PySide6.QtWidgets import QMenu, QWidget, QVBoxLayout, QLabel, QPushButton, QScrollArea, QFrame, QDialog, QGraphicsRectItem, QGraphicsItem, QGraphicsView, QGraphicsScene, QMessageBox, QCheckBox
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

    # [52.35] Overlay clips (Video 2, 3 ...) get their own linked-audio row too, one row per overlay track that has audible clips,
    # listed under the base clip's stream rows. Selecting one selects the overlay clip; right-click > Unlink audio moves it to the audio strip.
    def _base_n(self, tl):
        return max([max(1, len(s.media.audio_streams)) for s in tl.seq.segs if self._has_audio(s) and not s.mute] or [0])

    def _ov_clips(self, tl):
        vt = getattr(tl, "vtrack", None)
        if vt is None or not vt.enabled:
            return []
        return [c for c in vt.items if not c.mute and c.media.acodec.strip() and not getattr(c.media, "blank", False)]

    def _ov_rects(self, tl, y):
        """[(VClip, QRectF)] - one row per overlay track (highest track first), below the base stream rows."""
        cl = self._ov_clips(tl)
        trk = sorted({c.track for c in cl}, reverse=True)
        b = self._base_n(tl)
        return [(c, QRectF(tl.tx(c.t0), y + 1 + (b + trk.index(c.track)) * ROW_H, max(2.0, c.dur * tl.pps), ROW_H - 2)) for c in cl]

    def height(self, tl):
        n = self._base_n(tl) + len({c.track for c in self._ov_clips(tl)})
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
        vt = getattr(tl, "vtrack", None)
        for c, r in self._ov_rects(tl, y):                      # [52.35] overlay clips' audio
            if r.right() < tl.HW or r.left() > W:
                continue
            p.setPen(QPen(QColor("#101010"), 1))
            p.setBrush(QColor("#4f7fa0"))
            p.drawRoundedRect(r, 3, 3)
            if r.width() > 40:
                p.setPen(QColor("#0e1a22"))
                p.drawText(QPointF(r.x() + 5, r.y() + ROW_H - 4), fm.elidedText(c.media.name, Qt.TextElideMode.ElideRight, int(r.width() - 10)))
            if vt is not None and (c is vt.sel or c in vt.selset):
                p.setPen(QPen(QColor("#ffffff"), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r, 3, 3)

    def _press_ov(self, e, tl, c):
        vt, pos = tl.vtrack, e.position()
        tl._keep_vsel = True                                   # we set the selection ourselves (Timeline would clear the strips')
        tl.sel, tl.multi_sel = -1, set()
        for ln in tl.lanes:
            if ln is not self:
                getattr(ln, "clear_sel", lambda t: None)(tl)
        if tl.atrack:
            tl.atrack.clear_sel(tl)
        vt.sel, vt.selset = c, {c}
        vt.sync()
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(tl)
            a = m.addAction("Unlink audio (to audio track)")
            if m.exec(tl.mapToGlobal(pos.toPoint())) is a:
                self.unlink_ov([c])
        return True

    def unlink_ov(self, clips):
        api = self.api
        seq, at, vt = api.seq, api.win.atrack, api.tl.vtrack
        if seq.locked & {"video_overlay", "audio"}:
            api.status("Can't unlink: the overlay Video or Audio row is locked (click its padlock).")
            return
        if at is None or vt is None:
            return

        def do():
            for c in clips:
                c.mute = True
                k = at.free_track(c.t0, c.dur)
                if k is None:
                    at.ntracks += 1
                    k = at.ntracks - 1
                at.items.append(AClip(c.media, c.t0, c.in_s, c.out_s, False, c.vol_db, track=k))
            at.sel = at.items[-1]
            at.changed()
            vt.changed()
            return True
        api.engine.pause()
        seq.edit(do)
        api.status("Audio unlinked - it is now a clip on the audio track.")

    def press(self, e, tl):
        if tl.tool != "select":
            return False
        pos = e.position()
        oc = next((c for c, r in self._ov_rects(tl, self._y0(tl)) if r.contains(pos)), None)
        if oc is not None:
            return self._press_ov(e, tl, oc)
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

    # [52.37] A selected TEXT clip (text_tool mod, registered in seq.ext["text_tool"]) shows the text options here too.
    def text_item(self):
        st = self.api.seq.ext.get("text_tool")
        sel = getattr(st, "selset", None)
        if st is None or not sel or len(sel) != 1 or not hasattr(st, "panel"):
            return None, None
        return st, next(iter(sel))

    @staticmethod
    def tsig(it):
        return ("T", it.id, it.text, it.t0, it.dur, round(it.x, 3), round(it.y, 3), round(it.w, 3), round(it.h, 3))

    def poll(self):
        if self.apply_t.isActive():                       # the user is mid-edit: don't rebuild under them
            return
        if getattr(self.dlg, "flush", None) is not None and self.dlg._t.isActive():
            return                                        # text panel mid-edit
        s = self.current()
        st, it = (None, None) if s is not None else self.text_item()
        sig = (id(s), self.vals(s)) if s is not None else (self.tsig(it) if it is not None else None)
        if sig != self.sig:
            self.sig = sig
            if it is not None:
                self.build_text(st, it)
            else:
                self.build(s)

    def build_text(self, st, it):
        self.teardown()
        self.seg = None
        d = st.panel(it, on_apply=lambda: setattr(self, "sig", self.tsig(it)))     # our own change: no rebuild
        self.dlg = d
        self.msg.hide()
        self.sa.setWidget(d)
        self.sa.show()
        d.show()

    def teardown(self):
        if self.dlg is not None:
            if getattr(self.dlg, "flush", None) is not None:
                self.dlg.flush()                            # pending text edit -> its undo step
            self.sa.takeWidget()
            self.dlg.deleteLater()
            self.dlg = None

    def build(self, s):
        self.teardown()
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


# ----------------------------------------------------------------------------- part 3: overlay video tracks
# "Video 2", "Video 3", ... : VClip(media, t0, in_s, out_s, mute, vol_db, grp, track, rect, crop) with absolute times (no ripple), never
# overlapping WITHIN a track; a higher track is drawn on top. 1x forward only (no speed/mirror). History: seq.ext["video_overlay"].
# [52.29] Layout: the overlay tracks are a STRIP ABOVE the base Video row (Timeline.vtrack / strip_y / strips(); highest track on top).
#   The "+" is on the BASE Video row header. Dragging a clip vertically moves it between overlay tracks.
# [52.29/52.30] Every overlay clip has its OWN placement: `rect` = (x, y, w, h) of the UNCROPPED picture and `crop` = (l, t, r, b)
#   fractions of the picture, all normalised to the OUTPUT FRAME (None rect = "fit and centre"). It is edited with the program's OWN
#   Crop / Resize tools (VideoStage.edit_xf, one set of handles): select an overlay clip, pick the tool, park the playhead inside it.
#   Overlays are drawn on _OvlView, a layer sized to the output frame, so the base clip's crop/resize never clips them and they can sit
#   on blank canvas. Same maths in preview (_Layer) and export (_Export).
# Clips get on a track by dropping files on an overlay row, right-click base clip > "Send to overlay Video track", and back via
# right-click overlay clip > "Move to main video row". Preview: per track a QMediaPlayer + QGraphicsVideoItem (inside a clipping
# container item) in VideoView's scene, above the base item and below the freeze overlay. Export: EXPORT_HOOKS[0] composites with
# ffmpeg overlay and mixes overlay audio; active only while the Advanced tab is on.
# [KNOWN] no stills / audio-only files, no rotation, not in recovery.json, drop probing blocks the GUI briefly. While the base clip is
#   cropped/resized the overlay canvas is the visible preview area (approximate).
MIN_V = 0.3
_vids = itertools.count(1)
MIN_FRAC = 0.05                                                # smallest visible fraction left by cropping


def _num(v, lo=-1e12, hi=1e12):
    """[52.31] Validated finite number from the (untrusted) recovery file, or ValueError."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= float(v) <= hi:
        raise ValueError("bad number")
    return float(v)


class VClip:
    def __init__(self, media, t0, in_s, out_s, mute=False, vol_db=0.0, grp=None, track=0, rect=None, crop=(0.0, 0.0, 0.0, 0.0)):
        self.id = next(_vids)
        self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db = media, t0, in_s, out_s, mute, vol_db
        self.grp, self.track = grp, track
        self.rect = tuple(rect) if rect else None
        self.crop = tuple(crop)

    dur = property(lambda s: s.out_s - s.in_s)
    end = property(lambda s: s.t0 + s.out_s - s.in_s)

    def tup(self):
        return (self.id, self.media, self.t0, self.in_s, self.out_s, self.mute, self.vol_db, self.grp, self.track, self.rect, self.crop)


def eff_rect(c, car):
    """(x, y, w, h) of the uncropped picture, normalised to the canvas; car = canvas aspect (w/h). Default = fit + centre."""
    if c.rect:
        return c.rect
    m = c.media
    ar = (m.w / m.h) if getattr(m, "w", 0) and getattr(m, "h", 0) else car
    if ar >= car:
        h = car / ar
        return (0.0, (1.0 - h) / 2.0, 1.0, h)
    w = ar / car
    return ((1.0 - w) / 2.0, 0.0, w, 1.0)


def shown_rect(c, car):
    """The visible (cropped) region (x, y, w, h), normalised."""
    x, y, w, h = eff_rect(c, car)
    cl, ct, cr, cb = c.crop
    return (x + cl * w, y + ct * h, w * (1 - cl - cr), h * (1 - ct - cb))


class _Layer(QObject):
    """Preview of ONE overlay track: own player + audio output; the video item sits in a clipping container (= the crop)."""
    def __init__(self, vt, k):
        super().__init__(vt)
        self.vt, self.k = vt, k
        self.pl, self.ao = QMediaPlayer(self), QAudioOutput(self)
        self.pl.setAudioOutput(self.ao)
        self.box = QGraphicsRectItem()
        self.box.setPen(QPen(Qt.PenStyle.NoPen))
        self.box.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)
        self.box.setZValue(1 + k * 0.1)
        self.box.setVisible(False)
        self.item = QGraphicsVideoItem(self.box)
        self.item.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)
        vt.view._scene.addItem(self.box)
        self.pl.setVideoOutput(self.item)
        self._src, self._go, self._pos, self._c = None, False, 0, None
        self.pl.mediaStatusChanged.connect(self._status)

    def layout(self, c=None):
        c = c or self._c
        if c is None:
            return
        v = self.vt.view
        vw, vh = max(1, v.width()), max(1, v.height())
        x, y, w, h = eff_rect(c, vw / float(vh))
        cl, ct, cr, cb = c.crop
        fw, fh = w * vw, h * vh
        self.box.setRect(0, 0, max(1.0, fw * (1 - cl - cr)), max(1.0, fh * (1 - ct - cb)))
        self.box.setPos(x * vw + cl * fw, y * vh + ct * fh)
        self.item.setPos(-cl * fw, -ct * fh)
        self.item.setSize(QSizeF(max(1.0, fw), max(1.0, fh)))

    def dispose(self):
        self.pl.stop()
        self.pl.setVideoOutput(None)                          # [52.31] never leave a player pointing at an item that is about to die
        self.pl.setSource(QUrl())
        self.vt.view._scene.removeItem(self.box)

    def reset(self):
        self.pl.stop()
        self._src, self._c = None, None
        self.box.setVisible(False)

    def follow(self, c, t, playing):
        self._c = c
        if c is None:
            self.box.setVisible(False)
            if self.pl.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.pl.pause()
            return
        self.layout(c)
        self.box.setVisible(True)
        pos = int((c.in_s + t - c.t0) * 1000)
        self.ao.setVolume(0.0 if c.mute else max(0.0, min(1.0, 10 ** (c.vol_db / 20.0))))
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


class _OvlView(QGraphicsView):
    """[52.30] Transparent full-CANVAS layer for the overlay clips. Child of the stage, sized to the OUTPUT FRAME rect (not the base clip's
    crop mask), so overlays can sit on blank canvas area and are never clipped by the base clip's crop/resize."""
    def __init__(self, parent):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.BrushStyle.NoBrush)
        self.setStyleSheet("background: transparent; border: 0;")
        self.viewport().setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setInteractive(False)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._scene.setSceneRect(0, 0, max(1, self.width()), max(1, self.height()))


class VideoTrack(QObject):
    H = 30
    name = "Video"
    lock_key = "video_overlay"
    header_plus = False                                       # overlay rows have no "+" of their own: it is on the base Video row

    def __init__(self, win):
        super().__init__(win)
        self.win, self.tl, self.seq, self.eng = win, win.tl, win.seq, win.engine
        self.stage = win.stage
        self.view = _OvlView(win.stage)                       # [52.30] full-canvas layer (see _OvlView)
        self.items, self.sel, self.selset, self.drag = [], None, set(), None
        self.drop, self._drag_track = None, 0                 # [52.35] drop target of a move: ("new"|"base", time) or None
        self._last_canvas, self._live_orig = None, None
        self.ntracks = 0                                      # overlay tracks (rows); 0 = none until "+" / first send / drop
        self.enabled = False
        self.layers = []
        self.seq.ext["video_overlay"] = self
        self.view.installEventFilter(self)
        self._install_stage_hooks()
        self.eng.playStateChanged.connect(self.sync)
        self.eng.playheadChanged.connect(self._tick)
        self.seq.edited.connect(self.sync)
        self.hook = _Export(self)
        export_worker.EXPORT_HOOKS.insert(0, self.hook)

    # ------------------------------------------------------------------ lifecycle / history
    # ------------------------------------------------------------------ [52.30] stage integration (ONE Crop/Resize UI for everything)
    # The program's own Crop/Resize tools (VideoStage + CropOverlay) edit the SELECTED overlay clip through VideoStage.edit_xf; the stage
    # still renders the base clip. Wrapped (instance attributes, undone in dispose): set_state (so a blank spot with no base clip still
    # gets a canvas while editing), set_tool, relayout (keeps the overlay layer on the canvas rect).
    def _install_stage_hooks(self):
        st = self.stage
        self._o_state, self._o_tool, self._o_lay = st.set_state, st.set_tool, st.relayout

        def set_state(xf, src, fx=(0.0, False)):
            if xf is None and self.edit_wanted():
                cw, ch = self._last_canvas or (1920, 1080)
                xf, src = (cw, ch, 0, 0, cw, ch), (cw, ch)
            self._o_state(xf, src, fx)
            if st._eff():
                dw, dh = st._disp_dims()
                if dw > 0 and dh > 0:
                    self._last_canvas = (dw, dh)
            self.refresh_edit()

        def set_tool(tool):
            self._o_tool(tool)
            self.refresh_edit()

        def relayout():
            self._o_lay()
            self.place()
        st.set_state, st.set_tool, st.relayout = set_state, set_tool, relayout
        st.ov_commit, st.ov_reset, st.ov_live = self.stage_commit, self.stage_reset, self.stage_live
        st.cancelClicked.connect(self.restore_live)

    def _remove_stage_hooks(self):
        st = self.stage
        for n in ("set_state", "set_tool", "relayout"):
            try:
                delattr(st, n)
            except AttributeError:
                pass
        st.ov_commit = st.ov_reset = st.ov_live = None
        st.edit_xf = None
        try:
            st.cancelClicked.disconnect(self.restore_live)
        except Exception:
            pass

    def canvas_dims(self):
        st = self.stage
        if st._eff():
            return st._disp_dims()
        return self._last_canvas or (1920, 1080)

    def place(self):
        """Put the overlay layer exactly on the OUTPUT FRAME (canvas) rect of the stage."""
        st = self.stage
        if not (self.enabled and self.items):
            self.view.hide()
            return
        S = st.rect()
        if st._eff() is None or (st.xf is None and st.tool is None and not (st._rot() or st.fx[1])):
            cw, ch = self.canvas_dims() if st._eff() else (self._last_canvas or (S.width(), S.height()))
            k = min(S.width() / float(cw), S.height() / float(ch))     # untouched clip: the frame is the letterboxed fit area
            w, h = cw * k, ch * k
            rc = QRectF((S.width() - w) / 2.0, (S.height() - h) / 2.0, w, h)
        else:
            rc = QRectF(st._rc)
        self.view.setGeometry(rc.toRect())
        self.view.show()
        self.view.stackUnder(st.overlay)
        for ly in self.layers:
            ly.layout()

    def edit_wanted(self):
        c = self.sel
        return bool(self.enabled and c is not None and c in self.items and self.stage.tool in ("crop", "resize")
                    and c.t0 - 1e-6 <= self.eng.playhead < c.end)

    def to_xf(self, c, cw, ch):
        x, y, w, h = eff_rect(c, cw / float(ch))
        cl, ct, cr, cb = c.crop
        return (cw, ch, x * cw, y * ch, w * cw, h * ch, "#000000",
                (x + cl * w) * cw, (y + ct * h) * ch, w * (1 - cl - cr) * cw, h * (1 - ct - cb) * ch)

    def refresh_edit(self):
        st = self.stage
        if self._live_orig is not None and self.edit_wanted():
            return                                              # mid-drag: don't rebuild under the user
        want = self.edit_wanted() and st._eff() is not None
        new = self.to_xf(self.sel, *st._disp_dims()) if want else None
        if not want:
            self.restore_live()
        if new != st.edit_xf:
            st.edit_xf = new
            st.pend = None
            st.relayout()

    def stage_live(self, u):                                    # resize drag: show the overlay at the dragged size
        c = self.sel
        if c is None:
            return
        cw, ch = self.canvas_dims()
        if self._live_orig is None:
            self._live_orig = (c.rect, c.crop)
        c.rect = (u.x() / cw, u.y() / ch, max(0.02, u.width() / cw), max(0.02, u.height() / ch))
        self.sync()

    def restore_live(self, *_):
        c, o = self.sel, self._live_orig
        self._live_orig = None
        if c is not None and o is not None:
            c.rect, c.crop = o
            self.sync()

    def stage_commit(self, tool, u):
        """OK in Crop/Resize while an overlay clip is the edit target. u = new picture rect (resize) / kept window (crop), canvas units."""
        c = self.sel
        if c is None:
            return
        o, self._live_orig = self._live_orig, None
        if o is not None:
            c.rect, c.crop = o                                   # undo the live preview, then apply once (single undo step)
        if "video_overlay" in self.seq.locked:
            self.seq.blocked.emit("video_overlay")
            self.sync()
            return
        cw, ch = self.canvas_dims()
        x, y, w, h = eff_rect(c, cw / float(ch))
        before = self.begin()
        if tool == "resize":
            c.rect = (u.x() / cw, u.y() / ch, max(0.02, u.width() / cw), max(0.02, u.height() / ch))
        else:
            pic = QRectF(x * cw, y * ch, w * cw, h * ch)
            clip = u.intersected(pic)
            if clip.isEmpty() or clip.width() < MIN_FRAC * pic.width() or clip.height() < MIN_FRAC * pic.height():
                self.sync()
                return
            c.rect = (x, y, w, h)
            c.crop = (max(0.0, (clip.left() - pic.left()) / pic.width()), max(0.0, (clip.top() - pic.top()) / pic.height()),
                      max(0.0, (pic.right() - clip.right()) / pic.width()), max(0.0, (pic.bottom() - clip.bottom()) / pic.height()))
        self.changed()
        self.record(before)
        self.stage.edit_xf = None
        self.refresh_edit()

    def stage_reset(self):
        c = self.sel
        if c is None:
            return
        self.restore_live()
        before = self.begin()
        c.rect, c.crop = None, (0.0, 0.0, 0.0, 0.0)
        self.changed()
        self.record(before)
        self.stage.edit_xf = None
        self.refresh_edit()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.tl.vtrack = self if on else None
        self.tl.refresh_lanes()
        self.sync()

    def dispose(self):
        self.set_enabled(False)
        for ly in self.layers:
            ly.dispose()
        self.layers = []
        self._remove_stage_hooks()
        self.stage.relayout()
        self.view.hide()
        self.view.deleteLater()
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
        for i, m, t0, a, b, mu, vd, g, k, rc, cr in rows:
            c = VClip(m, t0, a, b, mu, vd, g, k, rc, cr)
            c.id = i
            self.items.append(c)
        ids = {c.id for c in self.selset}
        self.sel = next((c for c in self.items if self.sel is not None and c.id == self.sel.id), None)
        self.selset = {c for c in self.items if c.id in ids}
        self.changed()

    # ---- [52.31] crash recovery (see recovery.py). JSON-able dicts; media by project key.
    def recovery_export(self, key_of):
        if not self.items:
            return None
        return {"ntracks": self.ntracks, "media_keys": sorted({key_of(c.media) for c in self.items}),
                "items": [{"media": key_of(c.media), "t0": c.t0, "in_s": c.in_s, "out_s": c.out_s, "mute": bool(c.mute),
                           "vol_db": c.vol_db, "track": c.track, "rect": list(c.rect) if c.rect else None, "crop": list(c.crop)}
                          for c in self.items]}

    def recovery_import(self, data, by_path):
        items = []
        for d in data.get("items", []):
            try:
                m = by_path.get(os.path.abspath(d["media"]))
                if m is None or not m.has_video:
                    continue
                a = max(0.0, _num(d["in_s"], 0.0))
                b = min(_num(d["out_s"], 0.0), m.dur)
                if b - a < MIN_V:
                    continue
                rc, cr = d.get("rect"), d.get("crop") or [0, 0, 0, 0]
                rc = tuple(_num(v, -50, 50) for v in rc) if isinstance(rc, list) and len(rc) == 4 else None
                cr = tuple(_num(v, 0, 1) for v in cr) if isinstance(cr, list) and len(cr) == 4 else (0.0, 0.0, 0.0, 0.0)
                items.append(VClip(m, _num(d["t0"], 0.0), a, b, bool(d.get("mute")), _num(d.get("vol_db", 0.0), -400.0, 400.0), None,
                                   int(_num(d.get("track", 0), 0, 99)), rc, cr))
            except Exception:
                continue
        if not items:
            return 0
        self.items, self.sel, self.selset = items, None, set()
        try:
            self.ntracks = max(0, int(_num(data.get("ntracks", 0), 0, 99)))
        except Exception:
            self.ntracks = 0
        self.changed()
        return len(items)

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

    def remove_track(self, row):
        """`row` = display row (0 = top = highest track)."""
        k = self.ntracks - 1 - row
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
    def send_seg(self, idx, track=None, t=None):
        """Base-row clip -> overlay track (same start time; the base row closes the gap, as with any ripple delete).
        [52.35] track = overlay track number, "new" = a new top track, None = first free; t = start time (default: its old start)."""
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
        t0 = seq.starts()[idx] if t is None else max(0.0, t)

        def do():
            at0 = t0
            if track == "new":
                self.ntracks += 1
                k = self.ntracks - 1
            elif track is not None and 0 <= track < self.ntracks:
                k = track
                for o in sorted((o for o in self.items if o.track == k), key=lambda q: q.t0):
                    if o.end > at0 and o.t0 < at0 + s.src_dur:
                        at0 = o.end                             # next free spot on that track
            else:
                k = self.free_track(at0, s.src_dur)
                if k is None:
                    self.ntracks += 1
                    k = self.ntracks - 1
            self.items.append(VClip(s.media, at0, s.in_s, s.out_s, s.mute, s.vol_db, None, k))
            seq.delete(idx)
            self.sel, self.selset = self.items[-1], {self.items[-1]}
            self.changed()
            return True
        self.eng.pause()
        seq.edit(do)

    def to_base(self, c, t=None):
        """Overlay clip -> main video row (inserted at `t`, default its start time; its position/crop are dropped)."""
        if self.seq.locked & {"video", "video_overlay"}:
            self.seq.blocked.emit("video" if "video" in self.seq.locked else "video_overlay")
            return

        def do():
            self.items.remove(c)
            self.sel, self.selset = None, set()
            self.seq.insert_at(c.t0 if t is None else max(0.0, t), Seg(c.media, c.in_s, c.out_s, mute=c.mute, vol_db=c.vol_db))
            self.changed()
            return True
        self.eng.pause()
        self.seq.edit(do)

    # ------------------------------------------------------------------ Timeline hooks (strip above the video row)
    def row_track(self, row):
        return self.ntracks - 1 - row                          # display row 0 = top = highest track

    def drop_files(self, paths, t, row):
        """Files dropped on overlay display row `row` -> clips on that track (first free spot at/after the drop)."""
        k = self.row_track(row)
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
        n = self.ntracks
        return [(f"Video {self.row_track(r) + 2}", r * (self.H + 3), self.H) for r in range(n)]

    def height(self, tl=None):
        return self.ntracks * (self.H + 3) if self.enabled else 0

    def _rect(self, tl, c, y):
        return QRectF(tl.tx(c.t0), y + (self.ntracks - 1 - c.track) * (self.H + 3) + 1, max(2.0, c.dur * tl.pps), self.H - 2)

    def paint(self, p, tl, y, W):
        if not self.height(tl):
            return
        for r in range(self.ntracks):
            p.fillRect(QRectF(tl.HW, y + r * (self.H + 3), W - tl.HW, self.H), QColor("#181818"))
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
        y = tl.strip_y(self)
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
            getattr(ln, "clear_sel", lambda t: None)(tl)
        if tl.atrack:
            tl.atrack.clear_sel(tl)
        self.sel, self.selset, self.drag = c, {c}, None
        self.sync()
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(tl)
            a_opt, a_base = m.addAction("Options..."), m.addAction("Move to main video row")
            a_rst = m.addAction("Reset position / size / crop")
            a_dup, a_del = m.addAction("Duplicate\tCtrl+D"), m.addAction("Delete")
            act = m.exec(e.globalPosition().toPoint())
            if act is a_opt:
                self.edit(c)
            elif act is a_base:
                self.to_base(c)
            elif act is a_rst:
                before = self.begin()
                c.rect, c.crop = None, (0.0, 0.0, 0.0, 0.0)
                self.changed()
                self.record(before)
            elif act is a_dup:
                self.duplicate_key()
            elif act is a_del:
                self.delete_key()
            return True
        self.drag = (edge or "move", c, e.position().x(), c.t0, c.in_s, c.out_s, self.begin())
        self._drag_track, self.drop = c.track, None
        return True

    def move(self, pos, tl):
        if not self.drag:
            return
        mode, c, x0, t0, a0, b0, _ = self.drag
        dt = (pos.x() - x0) / tl.pps
        lo, hi = self.limits(c)
        if mode == "move":
            t = max(0.0, tl.snap_span(t0 + dt, c.dur))
            # [52.35] dragging ABOVE the top overlay row = new track on release; dragging DOWN onto the base Video row = move it there
            self.drop = (("base", t) if pos.y() >= tl.V_Y else ("new", t) if pos.y() < tl.strip_y(self) else None)
            r = max(0, min(self.ntracks - 1, int((pos.y() - tl.strip_y(self)) // (self.H + 3))))
            self._ghost(tl, c, pos, t, r)
            k = self.row_track(r)                              # drag vertically to another overlay track
            if k != c.track and self.free_at(k, t, c.dur, ignore=c):
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

    def _ghost(self, tl, c, pos, t, r):
        """[52.36] Cross-row drag feedback (painted by Timeline.paintEvent): the clip under the mouse + the row it will land on."""
        W, y0, k = tl.width(), tl.strip_y(self), self.row_track(r)
        kind = self.drop[0] if self.drop else None
        alone_top = c.track == self.ntracks - 1 and not any(o is not c and o.track == c.track for o in self.items)
        if kind == "new":
            tgt, new, ok = QRectF(tl.HW, y0 - 4, W - tl.HW, 6), True, not alone_top
        elif kind == "base":
            tgt, new, ok = QRectF(tl.HW, tl.V_Y, W - tl.HW, tl.V_H), False, True
        else:
            tgt, new, ok = QRectF(tl.HW, y0 + r * (self.H + 3), W - tl.HW, self.H), False, (k == c.track or self.free_at(k, t, c.dur, ignore=c))
        tl.ghost = {"rect": QRectF(tl.tx(t), pos.y() - self.H / 2.0, max(8.0, c.dur * tl.pps), self.H), "target": tgt, "new": new, "ok": ok,
                    "label": c.media.name, "color": "#7b62b0"}

    def release(self, e, tl):
        tl.ghost = None
        d, self.drag = self.drag, None
        drop, self.drop = self.drop, None
        if d:
            c = d[1]
            if drop and d[0] == "move":
                kind, t = drop
                if kind == "base":
                    c.t0, c.track = d[3], self._drag_track      # back to where it was: to_base records ONE undo step from there
                    self.changed()
                    self.to_base(c, t)
                    return
                alone_top = c.track == self.ntracks - 1 and not any(o is not c and o.track == c.track for o in self.items)
                if not alone_top:                               # a lone clip on the top row would just leave an empty row behind
                    self.ntracks += 1
                    c.track, c.t0 = self.ntracks - 1, t
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
        self.sync()

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
        n = VClip(c.media, t0, c.in_s, c.out_s, c.mute, c.vol_db, None, c.track, c.rect, c.crop)
        self.items.append(n)
        self.sel, self.selset = n, {n}
        self.changed()
        self.record(before)
        return True

    # ------------------------------------------------------------------ preview
    def _tick(self, _t):
        if self.items:
            self.sync()                                       # also while paused/scrubbing so the overlay follows the playhead

    def sync(self, *_):
        eng, t = self.eng, self.eng.playhead
        n = self.ntracks if (self.enabled and self.items) else 0
        while len(self.layers) < n:
            self.layers.append(_Layer(self, len(self.layers)))
        for k, ly in enumerate(self.layers):
            ly.follow(self.at(t, k) if k < n else None, t, bool(eng.playing))
        self.place()
        self.refresh_edit()


class _Export:
    """EXPORT_HOOKS entry: composites every overlay clip over the exported video (own position/size/crop) and mixes its audio in."""
    def __init__(self, vt):
        self.vt = vt

    def active(self, worker):
        return bool(utils.FFMPEG) and self.vt.enabled and bool(self.vt.items)

    def run(self, worker, src, tmpdir, ext):
        info = probing.probe_media(src)
        dur = info.dur if info else 0.0
        W, H = (info.w, info.h) if info and info.w and info.h else (1280, 720)
        W, H = W - W % 2, H - H % 2
        ev = lambda n: max(2, int(round(n / 2.0)) * 2)
        clips = sorted(self.vt.items, key=lambda c: (c.track, c.t0))     # higher track composited last = on top
        args, chain, alab = ["-i", src], [], []
        norm = "aresample=48000,aformat=channel_layouts=stereo"
        base_a = bool(info and info.acodec.strip())
        if base_a:
            chain.append(f"[0:a]{norm}[a0]")
            alab.append("[a0]")
        cur = "[0:v]"
        for n, c in enumerate(clips, 1):
            args += ["-i", c.media.path]
            t0, t1 = c.t0, c.end
            x, y, w, h = eff_rect(c, W / float(H))
            cl, ct, cr, cb = c.crop
            pw, ph = ev(w * W), ev(h * H)
            cx, cy = int(round(cl * pw)), int(round(ct * ph))
            cw, ch = ev(pw * (1 - cl - cr)), ev(ph * (1 - ct - cb))
            cropf = f",crop={cw}:{ch}:{cx}:{cy}" if any(c.crop) else ""
            ox, oy = int(round(x * W)) + cx, int(round(y * H)) + cy
            chain.append(f"[{n}:v]trim=start={c.in_s:.3f}:end={c.out_s:.3f},setpts=PTS-STARTPTS+{t0:.3f}/TB,"
                         f"scale={pw}:{ph}{cropf},format=yuva420p[o{n}]")
            chain.append(f"{cur}[o{n}]overlay=x={ox}:y={oy}:eof_action=pass:enable='between(t,{t0:.3f},{t1:.3f})'[v{n}]")
            cur = f"[v{n}]"
            if not c.mute and c.media.acodec.strip():
                ms = int(round(t0 * 1000))
                chain.append(f"[{n}:a]atrim=start={c.in_s:.3f}:end={c.out_s:.3f},asetpts=PTS-STARTPTS,{norm},"
                             f"volume={c.vol_db:.2f}dB,adelay={ms}|{ms}[a{n}]")
                alab.append(f"[a{n}]")
        mix = len(alab) > 1 or (alab and not base_a)
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
        self.vt.set_enabled(True)
        at = getattr(self.api.win, "atrack", None)
        if at is not None:
            at.show_empty = True                              # [52.26] Audio row (with its "+") is visible even before any audio is added
        self.api.seq.blocked.connect(self._blocked)
        self.api.seq.edited.connect(tl.refresh_lanes)         # lane height follows how many audio streams the clips carry
        tl.refresh_lanes()
        self.api.add_dock(self.dock_key, "Properties", PropertiesPanel(self.api))      # part 2
        self.api.set_export_toggle(True)                      # [52.33] part 4: Video | GIF toggle next to Export

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
        if self.lane in tl.lanes:
            tl.lanes.remove(self.lane)
        at = getattr(self.api.win, "atrack", None)
        if at is not None:
            at.show_empty = False
        tl.set_headers(False)                                 # also clears every lock (normal mode never has hidden locks)
        tl.refresh_lanes()
        self.api.remove_dock(self.dock_key)                   # part 2 (its timers die with the widget)
        self.api.set_export_toggle(False)                     # part 4


def on_load(api):
    global S
    S = State(api)


def build_mode(api):
    return "editor"                                            # share the main editor page (see plugins.py header)


def on_mode_shown(api):
    if S:
        S.enable()
        api.status("Advanced mode: linked audio rows under the video row (right-click > Unlink audio), Properties panel on the right.")


def confirm_leave(api):
    """[52.32] Warn before leaving Advanced (other modes know nothing about its extra tracks). False = stay."""
    if utils.prefs().value("pref_warn_leave_advanced", True, bool) is False:
        return True
    box = QMessageBox(QMessageBox.Icon.Warning, "Leaving Advanced mode",
                      "Advanced mode features aren't supported by other modes, your project might break.", QMessageBox.StandardButton.NoButton, api.win)
    leave = box.addButton("Leave Advanced", QMessageBox.ButtonRole.DestructiveRole)
    stay = box.addButton("Stay", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(stay)
    box.setEscapeButton(stay)
    cb = QCheckBox("Don't warn me again")
    box.setCheckBox(cb)
    box.exec()
    if box.clickedButton() is leave:
        if cb.isChecked():
            utils.prefs().setValue("pref_warn_leave_advanced", False)
        return True
    return False


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
