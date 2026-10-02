MOD_NAME = "FX"
MOD_TYPE = "tab"
MOD_DESC = "Effects tab: Fade-in / Fade-out / Dissolve (video) and Fade-in / Fade-out (audio) on clip intersections."
MOD_AUTHOR = "SQVCE"

# [MAP] FX = items anchored to a BOUNDARY of the video row (index k: 0 = timeline start, n = timeline end, else the cut
# between clip k-1 (A) and clip k (B)). Window = [cut+off, cut+off+dur] in timeline seconds, derived every time from
# seq.starts() so FX follow ripple edits. Clamping (never stored): fade-in lives inside B, fade-out inside A,
# dissolve inside the inner halves of A and B and must touch the cut (shifting it = changing the focus A <-> B).
# Fade semantics = opacity/gain keyframes inside the clip: fade-in is 0 before its window, fade-out is 0 after it.
# Dissolve = crossfade with FROZEN handles: before the cut the last... see FxExport (B's first frame fades in over A, after
# the cut A's last frame fades out over B) - total length never changes. Preview == export (same maths in gain()/vlayers()).
# Preview = FxOverlay (child of the stage, black + frozen dissolve stills) and a per-tick gain on Engine.audio (NO Engine edit).
# History = seq.ext provider; export = EXPORT_HOOKS pass over the assembled file; lane = FxLane (height 0): paints its layers ON the clip row, mouse via DropFilter.
import os
import uuid
import queue
import atexit
import shutil
import tempfile
import threading
import subprocess
from PySide6.QtCore import Qt, QObject, Signal, QRectF, QLineF, QEvent, QMimeData, QTimer, QSize, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QDrag, QIcon, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QListWidget, QListWidgetItem, QLabel, QMenu
import export_worker
import probing
import utils

MIME = "application/x-sqvce-fx"
NAME = "fx_tab"
DEF_DUR, MINFX, ROW_H, HS = 1.0, 0.1, 20, 6
LABEL = {"fadein": "Fade in", "fadeout": "Fade out", "dissolve": "Dissolve"}
S = None


class FItem:
    def __init__(self, kind, typ, k, sig, off, dur):
        self.kind, self.typ, self.k, self.sig, self.off, self.dur = kind, typ, k, sig, off, dur   # kind "v"/"a"
        self.id = uuid.uuid4().hex


# --------------------------------------------------------------------------------------------- frozen frames (dissolve preview)
class Frames(QObject):
    ready = Signal()

    def __init__(self):
        super().__init__()
        self.pix, self.pend, self.q, self.dir = {}, set(), queue.SimpleQueue(), None
        self.ready.connect(self._drain)
        atexit.register(self.close)

    def get(self, path, t):
        key = (path, round(t, 3))
        if key in self.pix:
            return self.pix[key]
        if key not in self.pend and utils.FFMPEG:
            self.pend.add(key)
            if self.dir is None:
                self.dir = tempfile.mkdtemp(prefix="quickcut_fx_")
            threading.Thread(target=self._grab, args=(key, self.dir), daemon=True).start()
        return None

    def _grab(self, key, d):
        out = os.path.join(d, uuid.uuid4().hex + ".png")
        try:
            subprocess.run([utils.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{key[1]:.3f}",
                            "-i", key[0], "-frames:v", "1", "-vf", "scale=960:-2", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=utils.NOWIN, timeout=30)
        except Exception:
            pass
        self.q.put((key, out if os.path.isfile(out) else ""))
        self.ready.emit()

    def _drain(self):
        while True:
            try:
                key, out = self.q.get_nowait()
            except queue.Empty:
                break
            self.pend.discard(key)
            self.pix[key] = QPixmap(out) if out else QPixmap()
        if S is not None:
            S.on_ph()

    def close(self):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None


# --------------------------------------------------------------------------------------------- state
class State:
    def __init__(self, api):
        self.api, self.items, self.sel, self.mod_audio = api, [], None, False
        self.frames = Frames()
        self.atimer = None

    # --- geometry
    def n(self):
        return len(self.api.seq.segs)

    def cut(self, k):
        st = self.api.seq.starts()
        return st[k] if k < len(st) else self.api.seq.total()

    def sig(self, k):
        segs, n = self.api.seq.segs, len(self.api.seq.segs)
        if not 0 <= k <= n:
            return None
        return (segs[k - 1].media.path if k > 0 else None, segs[k].media.path if k < n else None)

    def info(self, f):
        """(cut, lo, hi, s, e) - clamped window + the allowed region, or None when the FX is not placeable right now."""
        segs, n = self.api.seq.segs, len(self.api.seq.segs)
        if not 0 <= f.k <= n:
            return None
        c = self.cut(f.k)
        A = segs[f.k - 1] if f.k > 0 else None
        B = segs[f.k] if f.k < n else None
        if f.typ == "fadein":
            if B is None:
                return None
            lo, hi = c, c + B.dur
        elif f.typ == "fadeout":
            if A is None:
                return None
            lo, hi = c - A.dur, c
        else:
            if A is None or B is None:
                return None
            lo, hi = c - A.dur / 2, c + B.dur / 2
        s, e = max(lo, c + f.off), min(hi, c + f.off + f.dur)
        if f.typ == "dissolve":
            s, e = min(s, c), max(e, c)
        if e - s < MINFX - 1e-9:
            return None
        return c, lo, hi, s, e

    def gain(self, f, t):
        """Fade gain at t (None when t is outside the clip the fade belongs to)."""
        i = self.info(f)
        if not i:
            return None
        c, lo, hi, s, e = i
        if not lo <= t < hi:
            return None
        d = e - s
        if f.typ == "fadein":
            return 0.0 if t < s else ((t - s) / d if t < e else 1.0)
        return 1.0 if t < s else (1.0 - (t - s) / d if t < e else 0.0)

    # --- history (Ctrl+Z) via seq.ext
    def ext_snapshot(self):
        return [dict(i.__dict__) for i in self.items]

    def ext_restore(self, data):
        sid = self.sel.id if self.sel else None
        items = []
        for d in data:
            f = FItem("v", "fadein", 0, None, 0.0, 1.0)
            f.__dict__.update(d)
            items.append(f)
        self.items = items
        self.sel = next((i for i in items if i.id == sid), None)
        self.changed()

    def record(self, before):
        ext = getattr(before, "ext", None) or {}
        if self.ext_snapshot() != ext.get(NAME):
            self.api.seq.commit(before)
            self.api.seq.edited.emit()

    def changed(self):
        self.lane.sync()
        self.on_ph()

    def on_edited(self):
        n, keep = self.n(), []
        for f in self.items:
            if self.sig(f.k) == f.sig:
                keep.append(f)
                continue
            c = [k for k in range(n + 1) if self.sig(k) == f.sig]          # clips moved: re-find the same boundary
            if c:
                f.k = min(c, key=lambda k: abs(k - f.k))
                keep.append(f)
        if len(keep) != len(self.items):
            self.api.status("FX removed: the clips they belonged to are gone.", 5000)
        self.items = keep
        if self.sel not in keep:
            self.sel = None
        self.changed()

    # --- editing
    def select(self, f):
        self.sel = f
        tl = self.api.tl
        if f is not None:
            for ln in tl._all_lanes():
                if ln is not self.lane:
                    getattr(ln, "clear_sel", lambda t: None)(tl)
            if tl.sel != -1 or tl.multi_sel:                 # one selection across all layers (video clip too)
                tl.sel, tl.multi_sel = -1, set()
        tl._invalidate_content()

    def apply_at(self, key, t):
        kind, typ = key.split(":")
        n = self.n()
        ks = [k for k in range(n + 1) if (typ == "fadein" and k < n) or (typ == "fadeout" and k > 0)
              or (typ == "dissolve" and 0 < k < n)]
        if not ks:
            self.api.status("Add clips first (a dissolve needs two clips)." if typ == "dissolve" else "Add a clip first.", 5000)
            return
        k = min(ks, key=lambda q: abs(self.cut(q) - t))
        ex = next((f for f in self.items if (f.kind, f.typ, f.k) == (kind, typ, k)), None)
        if ex:
            self.select(ex)
            self.api.status("That effect is already on this intersection.", 4000)
            return
        segs = self.api.seq.segs
        A = segs[k - 1] if k > 0 else None
        B = segs[k] if k < n else None
        if typ == "fadein":
            d = min(DEF_DUR, B.dur)
            off = 0.0
        elif typ == "fadeout":
            d = min(DEF_DUR, A.dur)
            off = -d
        else:
            lf, rt = min(DEF_DUR / 2, A.dur / 2), min(DEF_DUR / 2, B.dur / 2)
            off, d = -lf, lf + rt
        if d < MINFX:
            self.api.status("Clip too short for this effect.", 4000)
            return
        b = self.api.seq.snapshot()
        f = FItem(kind, typ, k, self.sig(k), off, d)
        self.items.append(f)
        self.sel = f
        self.select(f)
        self.changed()
        self.record(b)

    def apply_playhead(self, key):
        self.apply_at(key, self.api.engine.playhead)

    def delete_selected(self):
        if self.sel is None or self.sel not in self.items:
            return False
        b = self.api.seq.snapshot()
        self.items.remove(self.sel)
        self.sel = None
        self.changed()
        self.record(b)
        return True

    # --- preview
    @staticmethod
    def head(s):
        return s.media.path, (max(s.in_s, s.out_s - 0.05) if s.rev else s.in_s)

    @staticmethod
    def tail(s):
        return s.media.path, (s.in_s + 0.04 if s.rev else max(s.in_s, s.out_s - 1.5 / (s.media.fps or 30.0)))

    def vlayers(self, t):
        black, imgs = 1.0, []
        for f in self.items:
            if f.kind != "v":
                continue
            if f.typ == "dissolve":
                i = self.info(f)
                if i and i[3] <= t < i[4]:
                    c, lo, hi, s, e = i
                    a = (t - s) / (e - s)
                    segs = self.api.seq.segs
                    pm, al = (self.frames.get(*self.head(segs[f.k])), a) if t < c else \
                             (self.frames.get(*self.tail(segs[f.k - 1])), 1.0 - a)
                    if pm is not None and not pm.isNull():
                        imgs.append((pm, al))
            else:
                g = self.gain(f, t)
                if g is not None:
                    black *= g
        return 1.0 - black, imgs

    def on_ph(self):
        t = self.api.engine.playhead
        if self.items or self.ov.isVisible():
            self.ov.refresh(t)
        self.apply_audio(t)

    def restore_audio(self):
        if self.mod_audio:
            self.mod_audio = False
            self.api.engine._apply_fx()

    def apply_audio(self, t):
        e = self.api.engine
        if not self.items and not self.mod_audio:
            return
        if not e.seq.segs or e._blip or e._scrub_active:
            return
        g = 1.0
        for f in self.items:
            if f.kind == "a":
                x = self.gain(f, t)
                if x is not None:
                    g *= x
        if g > 0.9999:
            self.restore_audio()
            return
        seg = e.seq.segs[min(e.idx, len(e.seq.segs) - 1)]
        base = 10 ** (float(seg.vol_db) / 20.0)
        if e._boost_on:
            e._boost_gain = base * g
        else:
            e.audio.setVolume(max(0.0, min(1.0, base)) * g)
        self.mod_audio = True


class FxOverlay(QWidget):
    def __init__(self, st, parent):
        super().__init__(parent)
        self.st, self.black, self.imgs = st, 0.0, []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def frame(self):
        dw, dh = self.st.api.stage._disp_dims()
        if not dw or not dh:
            return None
        k = min(self.width() / dw, self.height() / dh)
        return QRectF((self.width() - dw * k) / 2, (self.height() - dh * k) / 2, dw * k, dh * k)

    def refresh(self, t):
        stage = self.st.api.stage
        black, imgs = (0.0, []) if (stage.tool is not None or not self.st.items) else self.st.vlayers(t)
        self.black, self.imgs = black, imgs
        if black > 0.002 or imgs:
            if not self.isVisible():
                self.setGeometry(stage.rect())
                self.show()
                self.raise_()
            self.update()
        elif self.isVisible():
            self.hide()

    def paintEvent(self, e):
        F = self.frame()
        if F is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for pm, al in self.imgs:
            k = min(F.width() / pm.width(), F.height() / pm.height())
            w, h = pm.width() * k, pm.height() * k
            p.setOpacity(max(0.0, min(1.0, al)))
            p.drawPixmap(QRectF(F.x() + (F.width() - w) / 2, F.y() + (F.height() - h) / 2, w, h), pm,
                         QRectF(0, 0, pm.width(), pm.height()))
        if self.black > 0.002:
            p.setOpacity(min(1.0, self.black))
            p.fillRect(F, QColor("#000000"))


# --------------------------------------------------------------------------------------------- timeline lane
class FxLane:
    def __init__(self, st):
        self.st, self.drag = st, None

    def height(self, tl):
        return 0                                  # no row of its own: effects are painted ON the clip row (see paint)

    def sync(self):
        tl = self.st.api.tl
        tl.refresh_lanes()
        tl.update()

    def _rect(self, tl, f):
        """Overlay rectangle on the clip row: video FX = upper half of the row, audio FX = lower half."""
        i = self.st.info(f)
        if not i:
            return None, None
        c, lo, hi, s, e = i
        x0, x1 = tl.tx(s), tl.tx(e)
        h = tl.V_H / 2.0
        top = tl.V_Y + (0.0 if f.kind == "v" else h)
        return QRectF(x0, top + 1, max(3.0, x1 - x0), h - 2), c

    def paint(self, p, tl, y, W):                 # y is ignored on purpose: we draw over the clips, not below them
        for f in self.st.items:
            r, c = self._rect(tl, f)
            if r is None:
                continue
            p.save()
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.setBrush(QColor(255, 255, 255, 128))                # 50 % white layer
            p.drawRoundedRect(r, 3, 3)
            if r.width() > 14:
                p.setPen(QPen(QColor(16, 20, 28, 170), 1.2))
                l, t, rt, b = r.left() + 2, r.top() + 3, r.right() - 2, r.bottom() - 3
                if f.typ != "fadeout":
                    p.drawLine(QLineF(l, b, rt, t))
                if f.typ != "fadein":
                    p.drawLine(QLineF(l, t, rt, b))
            if f.typ == "dissolve":                                 # the cut the transition is centred on
                x = tl.tx(c)
                p.setPen(QPen(QColor(16, 20, 28, 200), 1, Qt.PenStyle.DashLine))
                p.drawLine(QLineF(x, r.top(), x, r.bottom()))
            if r.width() > 70:
                p.setPen(QColor("#10141c"))
                p.drawText(r.adjusted(5, 0, -3, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           ("Audio " if f.kind == "a" else "") + LABEL[f.typ])
            if f is self.st.sel:
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r, 3, 3)
            p.restore()

    def _hit(self, pos, tl):
        if not (tl.V_Y <= pos.y() < tl.V_Y + tl.V_H and pos.x() >= tl.HW):
            return None, None
        for f in reversed(self.st.items):
            r, _c = self._rect(tl, f)
            if r is not None and r.contains(pos):
                edge = None
                if r.width() > 20:
                    edge = "in" if pos.x() - r.left() <= HS else ("out" if r.right() - pos.x() <= HS else None)
                return f, edge
        return None, None

    def clear_sel(self, tl):
        if self.st.sel is not None:
            self.st.sel = None
            tl._invalidate_content()

    def delete_key(self):
        return self.st.delete_selected()

    def hover_cursor(self, pos, tl):                 # used by DropFilter (the core only asks lanes BELOW the clip row)
        f, edge = self._hit(pos, tl)
        if f is None:
            return None
        return Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.ArrowCursor

    def press(self, e, tl):
        st = self.st
        f, edge = self._hit(e.position(), tl)
        self.drag = None
        if f is None:
            return False
        st.select(f)
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(tl)
            a_del = m.addAction("Delete\tDel")
            if m.exec(e.globalPosition().toPoint()) is a_del:
                st.delete_selected()
            return True
        i = st.info(f)
        mode = "trim_in" if edge == "in" else "trim_out" if edge == "out" else "move"
        self.drag = (mode, e.position().x(), i, f, st.api.seq.snapshot())
        tl._invalidate_content()
        return True

    def move(self, pos, tl):
        if not self.drag:
            return
        mode, x0, i, f, _b = self.drag
        c, lo, hi, s0, e0 = i
        dt = (pos.x() - x0) / tl.pps

        def sn(t):                                                   # snap to the cut within 8 px
            return c if abs(t - c) * tl.pps <= 8 else t
        dis = f.typ == "dissolve"
        if mode == "move":
            tl.setCursor(Qt.CursorShape.ClosedHandCursor)
            d = e0 - s0
            lo_s, hi_s = lo, hi - d
            if dis:
                lo_s, hi_s = max(lo_s, c - d), min(hi_s, c)
            s = s0 + dt
            ds = [x for x in (sn(s) - s, sn(s + d) - (s + d)) if abs(x) > 1e-9]
            if ds:
                s += min(ds, key=abs)
            s = max(lo_s, min(hi_s, s))
            e = s + d
        elif mode == "trim_in":
            s = max(lo, min(sn(s0 + dt), e0 - MINFX))
            if dis:
                s = min(s, c)
            e = e0
        else:
            e = min(hi, max(sn(e0 + dt), s0 + MINFX))
            if dis:
                e = max(e, c)
            s = s0
        f.off, f.dur = s - c, e - s
        tl._invalidate_content()
        self.st.on_ph()

    def release(self, e, tl):
        d, self.drag = self.drag, None
        tl.setCursor(Qt.CursorShape.ArrowCursor)
        if d:
            self.st.record(d[4])
        tl._invalidate_content()

    def dbl(self, e, tl):
        f, _ = self._hit(e.position(), tl)
        self.drag = None
        return f is not None


# --------------------------------------------------------------------------------------------- tab UI + drag source
class FxList(QListWidget):
    def __init__(self):
        super().__init__()
        self._pi = self._pp = None
        self.setViewMode(QListWidget.ViewMode.IconMode)          # [grid] tiles flow left->right, wrap at the panel width
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWrapping(True)
        self.setSpacing(6)
        self.setIconSize(QSize(64, 40))
        self.setGridSize(QSize(96, 80))
        self.setUniformItemSizes(True)
        self.setWordWrap(True)

    # [drag fix] IconMode swallows Qt's built-in drag start, so the drag is started by hand (press item + move distance)
    def mousePressEvent(self, e):
        self._pi = self.itemAt(e.position().toPoint())
        self._pp = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        it = getattr(self, "_pi", None)
        if (it is not None and e.buttons() & Qt.MouseButton.LeftButton
                and (e.position().toPoint() - self._pp).manhattanLength() >= QApplication.startDragDistance()):
            self._pi = None
            key = it.data(Qt.ItemDataRole.UserRole)
            if key:
                md = QMimeData()
                md.setData(MIME, key.encode())
                d = QDrag(self)
                d.setMimeData(md)
                d.setPixmap(it.icon().pixmap(64, 40))
                d.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(e)


class DropFilter(QObject):
    """Timeline event filter. (1) accepts FX dragged out of the tab (the core only knows file URLs); (2) gives the FX
    overlays painted ON the clip row first refusal on mouse events there (the core would treat them as clip clicks)."""
    def __init__(self, st):
        super().__init__()
        self.st, self.grab = st, False

    def mouse(self, ev, t):
        st, tl, lane = self.st, self.st.api.tl, self.st.lane
        T, B = QEvent.Type, Qt.MouseButton
        if t == T.MouseButtonRelease:
            if self.grab:
                self.grab = False
                lane.release(ev, tl)
                return True
            return False
        if self.grab:
            if t == T.MouseMove:
                lane.move(ev.position(), tl)
            return True
        if tl.mode is not None or tl._lane_grab is not None or tl.tool == "razor":
            return False                                     # the core is busy with its own drag / razor tool
        pos = ev.position()
        f, _edge = lane._hit(pos, tl)
        if f is None:
            return False
        if t == T.MouseMove:
            if ev.buttons() != B.NoButton:
                return False
            tl.setCursor(lane.hover_cursor(pos, tl))
            return True
        if ev.button() not in (B.LeftButton, B.RightButton):
            return False
        if t == T.MouseButtonDblClick:
            return True                                      # do not open Clip Options through the overlay
        tl.setFocus(Qt.FocusReason.MouseFocusReason)
        lane.press(ev, tl)
        self.grab = ev.button() == B.LeftButton and lane.drag is not None
        return True

    def eventFilter(self, o, ev):
        t = ev.type()
        if (t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease,
                  QEvent.Type.MouseButtonDblClick) and (self.st.items or self.grab)):
            return self.mouse(ev, t)
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()
            tl = self.st.api.tl
            tl.drop_x = ev.position().x()
            tl.update()
            return True
        if t == QEvent.Type.Drop and ev.mimeData().hasFormat(MIME):
            tl = self.st.api.tl
            tl.drop_x = None
            key = bytes(ev.mimeData().data(MIME)).decode()
            ev.acceptProposedAction()
            tl.update()
            self.st.apply_at(key, max(0.0, tl.xt(ev.position().x())))
            return True
        return False


def _fx_icon(typ, kind):
    """Small glyph for a grid tile: ramp up (fade-in), ramp down (fade-out), two crossing ramps (dissolve)."""
    pm = QPixmap(128, 80)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor("#4aa3ff" if kind == "v" else "#5fcf80")
    p.setPen(QPen(c, 4))
    p.setBrush(QColor(c.red(), c.green(), c.blue(), 70))
    up = [QPointF(10, 70), QPointF(118, 10)]
    dn = [QPointF(10, 10), QPointF(118, 70)]
    if typ == "fadein":
        p.drawPolygon(QPolygonF(up + [QPointF(118, 70)]))
    elif typ == "fadeout":
        p.drawPolygon(QPolygonF(dn + [QPointF(10, 70)]))
    else:
        p.drawLine(up[0], up[1])
        p.drawLine(dn[0], dn[1])
    p.end()
    return QIcon(pm)


def build_tab(api):
    st = S
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(6, 6, 6, 6)
    cats = QListWidget()                                  # left: categories only
    cats.setFixedWidth(120)
    grid = FxList()                                       # right: effects as a grid
    effs = {"Video": ("v", (("Fade-in", "fadein"), ("Fade-out", "fadeout"), ("Dissolve", "dissolve"))),
            "Audio": ("a", (("Fade-in", "fadein"), ("Fade-out", "fadeout")))}
    for cat in effs:
        cats.addItem(cat)

    def show(row):
        grid.clear()
        kind, items = effs[cats.item(max(row, 0)).text()]
        for label, typ in items:
            it = QListWidgetItem(_fx_icon(typ, kind), label)
            it.setData(Qt.ItemDataRole.UserRole, f"{kind}:{typ}")
            it.setToolTip("Double-click: apply at the intersection closest to the playhead. Drag onto the timeline "
                          "to apply at the closest intersection to the drop.")
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            grid.addItem(it)
    cats.currentRowChanged.connect(show)
    cats.setCurrentRow(0)
    grid.itemDoubleClicked.connect(lambda it: st.apply_playhead(it.data(Qt.ItemDataRole.UserRole))
                                   if it.data(Qt.ItemDataRole.UserRole) else None)
    lay.addWidget(cats)
    lay.addWidget(grid, 1)

    return w


# --------------------------------------------------------------------------------------------- export
class FxExport:
    def __init__(self, st):
        self.st = st

    def active(self, worker):
        return bool(utils.FFMPEG) and any(self.st.info(f) for f in self.st.items)

    def run(self, worker, src, tmpdir, ext):
        st = self.st
        m = probing.probe_media(src)
        if not m:
            return None
        W, H = int(m.w) or 1920, int(m.h) or 1080
        W, H = W - W % 2, H - H % 2
        fps, dur = float(m.fps or 30.0), float(m.dur or 0.0) + 1.0
        plan = []
        for f in st.items:
            i = st.info(f)
            if i:
                plan.append((f,) + i)                                # (f, cut, lo, hi, s, e)
        args, vc, ac, last, nin = ["-i", src], [], [], "0:v", 1
        if m.has_video:
            for n, (f, c, lo, hi, s, e) in enumerate(plan):
                if f.kind != "v":
                    continue
                d = e - s
                if f.typ == "dissolve":
                    for tag, tt in (("b", c - 0.5 / fps), ("a", c - 1.5 / fps)):     # B's first frame / A's last frame
                        png = os.path.join(tmpdir, f"fx{n}{tag}.png")
                        worker._ffmpeg(["-ss", f"{max(0.0, tt):.4f}", "-i", src, "-frames:v", "1", png])
                        args += ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{dur:.3f}", "-i", png]
                        if tag == "b":
                            ramp, en = "in", f"gte(t,{s:.3f})*lt(t,{c:.3f})"
                        else:
                            ramp, en = "out", f"gte(t,{c:.3f})*lte(t,{e:.3f})"
                        vc.append(f"[{nin}:v]scale={W}:{H},format=yuva420p,fade=t={ramp}:st={s:.3f}:d={d:.3f}:alpha=1[x{n}{tag}]")
                        vc.append(f"[{last}][x{n}{tag}]overlay=0:0:enable='{en}':shortest=1:format=auto[o{n}{tag}]")
                        last = f"o{n}{tag}"
                        nin += 1
                else:
                    if f.typ == "fadein":
                        ramp, en = "out", f"gte(t,{lo:.3f})*lt(t,{e:.3f})"      # opaque black until s, then clears
                    else:
                        ramp, en = "in", f"gte(t,{s:.3f})*lt(t,{hi:.3f})"       # clears until s, then goes black
                    vc.append(f"color=c=black:s={W}x{H}:r={fps:.3f}:d={dur:.3f},format=yuva420p,"
                              f"fade=t={ramp}:st={s:.3f}:d={d:.3f}:alpha=1[x{n}]")
                    vc.append(f"[{last}][x{n}]overlay=0:0:enable='{en}':shortest=1:format=auto[o{n}]")
                    last = f"o{n}"
        if m.acodec.strip():
            for f, c, lo, hi, s, e in plan:
                if f.kind != "a":
                    continue
                d = e - s
                g = (f"if(lt(t,{s:.3f}),0,if(lt(t,{e:.3f}),(t-{s:.3f})/{d:.3f},1))" if f.typ == "fadein" else
                     f"if(lt(t,{s:.3f}),1,if(lt(t,{e:.3f}),1-(t-{s:.3f})/{d:.3f},0))")
                ac.append(f"volume=volume='if(gte(t,{lo:.3f})*lt(t,{hi:.3f}),{g},1)':eval=frame")
        if not vc and not ac:
            return None
        parts = list(vc)
        if vc:
            parts.append(f"[{last}]format=yuv420p[vout]")
        if ac:
            parts.append("[0:a]" + ",".join(ac) + "[aout]")
        webm = ext.lower() == ".webm"
        cmd = args + ["-filter_complex", ";".join(parts), "-map", "[vout]" if vc else "0:v?",
                      "-map", "[aout]" if ac else "0:a?"]
        if vc:
            cmd += (["-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0", "-cpu-used", "4", "-row-mt", "1"] if webm
                    else ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-threads", "0"])
        else:
            cmd += ["-c:v", "copy"]
        if ac:
            cmd += ["-c:a", "libopus", "-b:a", "128k"] if webm else ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-c:a", "copy"]
        dst = os.path.join(tmpdir, "fx_out" + ext)
        worker._ffmpeg(cmd + [dst])
        return dst


# --------------------------------------------------------------------------------------------- mod hooks
def on_load(api):
    global S
    st = S = State(api)
    stage = api.stage
    st.ov = FxOverlay(st, stage)
    st.ov.setGeometry(stage.rect())
    st.lane = FxLane(st)
    api.tl.lanes.append(st.lane)
    st.hook = FxExport(st)
    api.seq.ext[NAME] = st
    export_worker.EXPORT_HOOKS.append(st.hook)
    st.prev = stage.__dict__.get("relayout")
    orig = stage.relayout

    def relayout():
        orig()
        if S is st:
            st.ov.setGeometry(stage.rect())
            if st.ov.isVisible():
                st.ov.raise_()
    st.relayout = relayout
    stage.relayout = relayout
    st.drop = DropFilter(st)
    api.tl.installEventFilter(st.drop)
    st.econn = api.seq.edited.connect(st.on_edited)
    st.conn = api.engine.playheadChanged.connect(lambda _t: st.on_ph())
    # [fade-in fix] re-apply the audio gain every 25 ms independently of playheadChanged, which can fire late/rarely
    # right after Play (the clip then starts at full volume until the first tick arrives).
    st.atimer = QTimer(st.api.win)
    st.atimer.setInterval(25)
    st.atimer.timeout.connect(lambda: st.items and st.apply_audio(st.api.engine.playhead))
    st.atimer.start()


def on_unload(api):
    global S
    st = S
    if st is None:
        return
    for sig, c in ((api.seq.edited, st.econn), (api.engine.playheadChanged, st.conn)):
        try:
            sig.disconnect(c)
        except Exception:
            pass
    api.tl.removeEventFilter(st.drop)
    stage = api.stage
    if stage.__dict__.get("relayout") is st.relayout:            # only undo OUR patch (the Text mod may sit on top of it)
        if st.prev is None:
            stage.__dict__.pop("relayout", None)
        else:
            stage.relayout = st.prev
    if st.atimer:
        st.atimer.stop()
        st.atimer.deleteLater()
    st.restore_audio()
    api.seq.ext.pop(NAME, None)
    if st.lane in api.tl.lanes:
        api.tl.lanes.remove(st.lane)
    api.tl.refresh_lanes()
    if st.hook in export_worker.EXPORT_HOOKS:
        export_worker.EXPORT_HOOKS.remove(st.hook)
    st.ov.deleteLater()
    st.frames.close()
    S = None


def on_clear(api):
    if S:
        S.items, S.sel = [], None
        S.changed()
