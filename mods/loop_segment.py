MOD_NAME = "Loop segment"
MOD_TYPE = "tool"
MOD_ICON = "\u21BB"
MOD_DESC = "Click the timeline to set A, click again to set B, then every click jumps the playhead A <-> B. Playback loops B -> A. Right-click clears."
MOD_AUTHOR = "SQVCE"
# While the tool is selected: ALL clicks on the timeline (ruler included) are consumed by this mod (no clip selection,
# no context menu). Qt delivers a quick 2nd click as a DoubleClick event instead of a Press - it is handled as a click too.
# click 1 = A, click 2 = B (swapped if B < A), click 3+ = playhead jumps A, B, A, B ...; right click = clear.
# Drag an A/B line to fine-tune it. Shift+click = start a new loop (sets A again).
# Playback crossing B naturally (not a scrub) jumps back to A while the tool is selected. Markers are painted by a
# height-0 lane (inside the cached timeline content -> _invalidate_content after every change).
import time
from PySide6.QtCore import Qt, QObject, QEvent, QLineF, QRectF, QTimer
from PySide6.QtGui import QColor, QPen

MIN_GAP = 1.0            # minimum loop length in seconds
S = None


class LoopLane:
    def __init__(self, st):
        self.st = st

    def height(self, tl):
        return 0

    def paint(self, p, tl, y, W):
        st = self.st
        if st.a is None:
            return
        c = QColor("#ffb020") if st.on else QColor("#9a7a30")
        y0, y1, rh = tl.RULER_H, tl.V_Y + tl.V_H, tl.RULER_H
        xa = tl.tx(st.a)
        if st.b is not None:
            xb = tl.tx(st.b)
            fill = QColor(c)
            fill.setAlpha(40)
            p.fillRect(QRectF(xa, 0, xb - xa, y1), fill)
        for lab, t, x in (("A", st.a, xa), ("B", st.b, tl.tx(st.b) if st.b is not None else 0)):
            if t is None:
                continue
            p.setPen(QPen(c, 2))
            p.drawLine(QLineF(x, y0, x, y1))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            flag = QRectF(x, 0, 52, rh - 2)
            if lab == "B":
                flag.moveRight(x)
            p.drawRect(flag)
            p.setPen(QColor("#1a1a1a"))
            p.drawText(flag, Qt.AlignmentFlag.AlignCenter, f"{lab} {t:.2f}s")

    def press(self, e, tl): pass
    def move(self, pos, tl): pass
    def release(self, e, tl): pass
    def dbl(self, e, tl): pass


class State(QObject):
    def __init__(self, api):
        super().__init__()
        self.api, self.a, self.b, self.on, self.nxt, self.eat, self.prev, self.drag = api, None, None, False, 0, False, 0.0, None
        self.hov, self.cool = False, 0.0

    def changed(self):
        tl = self.api.tl
        tl.refresh_lanes()
        tl._invalidate_content()

    def seek(self, t, play=None):
        """Engine.seek keeps/starts playback (play=True). tl.seekRequested -> Engine.scrub is a scrub and PAUSES."""
        e = self.api.engine
        e.seek(max(0.0, t), play=bool(getattr(e, "playing", False)) if play is None else play)

    def clear(self):
        self.a = self.b = self.drag = None
        self.nxt = 0
        self.changed()
        self.api.status("Loop cleared")

    def click(self, t):
        if self.a is None:
            self.a, self.b, self.nxt = t, None, 0
            self.api.status("Loop A set - click again to set B")
            self.seek(t)
        elif self.b is None:
            self.a, self.b = sorted((self.a, t))
            self.fit_gap("b")
            self.nxt = 1
            self.api.status("Loop B set - clicks now jump A <-> B, right-click clears")
            self.seek(self.a)
        else:
            self.seek(self.a if self.nxt == 0 else self.b)
            self.nxt ^= 1
        self.changed()

    def fit_gap(self, moved):
        """Keep B - A >= MIN_GAP: push B right, or A left when B would pass the end of the sequence."""
        if self.b - self.a >= MIN_GAP:
            return
        tot = self.api.seq.total()
        self.b = self.a + MIN_GAP
        if self.b > tot and tot >= MIN_GAP:
            self.b = tot
            self.a = tot - MIN_GAP

    def tick(self, t):
        """Playback loop. Handles B crossed normally, the project's normal loop wrapping to 0 (B at the very end),
        and playback started with the playhead sitting on B."""
        a, b, prev, self.prev = self.a, self.b, self.prev, t
        if not self.on or a is None or b is None or not getattr(self.api.engine, "playing", True):
            return
        if time.monotonic() < self.cool:
            return
        if (prev < b <= t and t - prev < 0.3) or (t < a and prev >= b - 0.05) or (prev >= b and b <= t <= b + 0.3):
            self.cool = time.monotonic() + 0.15
            self.seek(a, play=True)

    def on_ph(self, t):
        self.tick(t)

    def _near(self, x):
        tl = self.api.tl
        for k in ("a", "b"):
            v = getattr(self, k)
            if v is not None and abs(x - tl.tx(v)) <= 6:
                return k
        return None

    def eventFilter(self, o, ev):
        if not self.on:
            return False
        t, B, tl, T = ev.type(), Qt.MouseButton, self.api.tl, QEvent.Type
        if t == T.ContextMenu:                              # never show the timeline menu while this tool is active
            return True
        if t not in (T.MouseButtonPress, T.MouseButtonRelease, T.MouseButtonDblClick, T.MouseMove):
            return False
        pos = ev.position()
        if t == T.MouseMove:
            if self.drag and ev.buttons() & B.LeftButton:
                v = tl.snap_t(max(0.0, tl.xt(pos.x())))          # snaps to clip edges within a few px
                if self.drag == "a":
                    self.a = min(v, self.b - MIN_GAP) if self.b is not None else v
                    self.a = max(0.0, self.a)
                else:
                    self.b = max(v, self.a + MIN_GAP)
                self.changed()
                return True
            near = not (ev.buttons() & B.LeftButton) and pos.x() >= tl.HW and self._near(pos.x())
            if near:                                        # consumed so the timeline's own hover code can't reset it
                tl.setCursor(Qt.CursorShape.SizeHorCursor)
                self.hov = True
                return True
            if self.hov:
                self.hov = False
                tl.unsetCursor()
            return self.eat
        if t == T.MouseButtonRelease:
            if self.eat or self.drag:
                self.eat, self.drag = False, None
                tl.unsetCursor()
                self.hov = False
                return True
            return False
        if pos.x() < tl.HW:
            return False
        if ev.button() == B.RightButton:                    # press AND double-click both clear; the menu never opens
            if t == T.MouseButtonPress or not self.eat:
                self.clear()
            self.eat = True
            return True
        if ev.button() == B.LeftButton:
            self.eat = True
            k = self._near(pos.x())
            if k and not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.drag = k
                tl.setCursor(Qt.CursorShape.SizeHorCursor)
                return True
            if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.a = self.b = None
            try:
                self.click(tl.snap_t(max(0.0, tl.xt(pos.x()))))        # Press OR DblClick: a fast 2nd click is a DblClick event
            except Exception as e:
                self.api.status(f"Loop segment error: {e}", 8000)
            return True
        return False


def on_load(api):
    global S
    st = S = State(api)
    st.lane = LoopLane(st)
    api.tl.lanes.append(st.lane)
    api.tl.installEventFilter(st)
    st.conn = api.engine.playheadChanged.connect(st.on_ph)
    st.tm = QTimer(api.win)                               # playheadChanged is silent when the playhead rests on the end
    st.tm.setInterval(25)
    st.tm.timeout.connect(lambda: st.a is not None and st.tick(api.engine.playhead))
    st.tm.start()
    st.changed()


def on_tool_selected(api):
    if S:
        S.on = True
        S.changed()
        api.status("Loop segment: click = A, click = B, then every click jumps A/B. Right-click clears.")


def on_tool_deselected(api):
    if S:
        S.on, S.hov = False, False
        api.tl.unsetCursor()
        S.changed()


def on_clear(api):
    if S:
        S.a = S.b = None
        S.nxt = 0
        S.changed()


def on_unload(api):
    global S
    st = S
    if st is None:
        return
    try:
        api.engine.playheadChanged.disconnect(st.conn)
    except Exception:
        pass
    st.tm.stop()
    st.tm.deleteLater()
    api.tl.removeEventFilter(st)
    if st.lane in api.tl.lanes:
        api.tl.lanes.remove(st.lane)
    api.tl.refresh_lanes()
    api.tl._invalidate_content()
    S = None
