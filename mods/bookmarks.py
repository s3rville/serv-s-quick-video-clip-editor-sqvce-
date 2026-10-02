MOD_NAME = "Bookmarks"
MOD_TYPE = "tool"
MOD_ICON = "\u2691"
MOD_DESC = "Click the timeline to drop a coloured bookmark glued to that exact moment of the clip (follows moves/trims). F2 / double-click = rename, Delete = delete selected, right-click = menu."
MOD_AUTHOR = "SQVCE"
# A bookmark stores (media path, SOURCE time) - not a timeline time - so it follows its clip when clips are moved, trimmed,
# extended or re-ordered. If a trim hides that source moment the bookmark is dormant (not drawn) and comes back when the
# clip is extended again. `ref` (the Seg object) disambiguates when the same media sits in several clips.
# History: provider in seq.ext["bookmarks"] (Ctrl+Z / Ctrl+Y). Not saved in recovery.json (like text/audio). Not exported.
# While the tool is selected ALL timeline clicks are consumed (like Loop segment). Left click on empty = new bookmark,
# on a bookmark = select (Ctrl toggle, Shift add), drag = move it (re-attaches to the clip under the cursor; snaps to the
# playhead within SNAP_PX, otherwise to video cut points), double-click / F2 = rename, Ctrl+A = select all,
# Delete = delete selected, right-click = menu.
# Label text colour is chosen by WCAG contrast (black or white), see text_color().
import random
import uuid
from PySide6.QtCore import Qt, QObject, QEvent, QLineF, QRectF
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QMenu, QInputDialog

NAME = "bookmarks"
S = None
SNAP_PX = 8                                               # playhead snap distance while dragging (same as Timeline.snap_t)


def text_color(c):
    """Black or white label text, whichever has the higher WCAG contrast ratio on background colour c."""
    def lin(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    L = 0.2126 * lin(c.red()) + 0.7152 * lin(c.green()) + 0.0722 * lin(c.blue())
    contrast_white = 1.05 / (L + 0.05)
    contrast_black = (L + 0.05) / 0.05
    return QColor("#000000") if contrast_black >= contrast_white else QColor("#ffffff")


class BM:
    def __init__(self, path, src, name, color, id=None):
        self.id, self.path, self.src, self.name, self.color = id or uuid.uuid4().hex, path, src, name, color
        self.ref, self.last = None, 0.0           # ref: Seg object last seen (not in history); last: last timeline time


class BmLane:
    def __init__(self, st):
        self.st = st

    def height(self, tl):
        return 0

    def paint(self, p, tl, y, W):
        st = self.st
        st.rects = {}
        y1, rh, fm = tl.V_Y + tl.V_H, tl.RULER_H, p.fontMetrics()
        for b, t in st.visible():
            x = tl.tx(t)
            if x < tl.HW - 1 or x > W:
                continue
            c, sel = QColor(b.color), b.id in st.sel
            p.setPen(QPen(QColor("#ffffff") if sel else c, 4 if sel else 2))
            if sel:
                p.drawLine(QLineF(x, 0, x, y1))
            p.setPen(QPen(c, 2))
            p.drawLine(QLineF(x, 0, x, y1))
            label = fm.elidedText(b.name, Qt.TextElideMode.ElideRight, 140)
            r = QRectF(x, 0, fm.horizontalAdvance(label) + 12, rh - 2)
            p.setBrush(c)
            # selected = white outline; otherwise a thin dark outline so light labels stay readable on the ruler
            p.setPen(QPen(QColor("#ffffff"), 2) if sel else QPen(QColor(0, 0, 0, 110), 1))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(text_color(c))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
            st.rects[b.id] = r

    def delete_key(self):                              # MainWindow.handle_delete_key asks every lane
        return self.st.on and self.st.delete_selected()

    def clear_sel(self, tl):
        if self.st.sel:
            self.st.sel = set()

    def press(self, e, tl): pass
    def move(self, pos, tl): pass
    def release(self, e, tl): pass
    def dbl(self, e, tl): pass


class State(QObject):
    def __init__(self, api):
        super().__init__()
        self.api, self.items, self.sel, self.prim, self.on = api, [], set(), None, False
        self.rects, self.drag, self.eat, self.hov, self.collapse, self.n = {}, None, False, False, None, 0

    # ---- history provider
    def ext_snapshot(self):
        return [dict(id=b.id, path=b.path, src=b.src, name=b.name, color=b.color) for b in self.items]

    def ext_restore(self, data):
        old = {b.id: b for b in self.items}
        self.items = []
        for d in data:
            b = BM(d["path"], d["src"], d["name"], d["color"], d["id"])
            if d["id"] in old:
                b.ref, b.last = old[d["id"]].ref, old[d["id"]].last
            self.items.append(b)
        self.sel &= {b.id for b in self.items}
        self.changed()

    def record(self, before):
        seq = self.api.seq
        if self.ext_snapshot() != (getattr(before, "ext", None) or {}).get(NAME):
            seq.commit(before)
            seq.edited.emit()
        self.changed()

    def changed(self):
        self.api.tl._invalidate_content()
        self.api.tl.update()

    # ---- playhead snapping
    def drag_time(self, x):
        """Timeline time for cursor x while dragging: playhead if within SNAP_PX, else video-cut snapping."""
        tl = self.api.tl
        if abs(tl.tx(tl.playhead) - x) <= SNAP_PX:       # Timeline.playhead = seconds (set_playhead)
            return tl.playhead
        return tl.snap_t(max(0.0, tl.xt(x)))

    # ---- clip attachment
    def where(self, b):
        """Timeline time of bookmark b, or None while its source moment is trimmed away."""
        seq = self.api.seq
        segs = seq.segs
        if not segs:
            return None
        st = seq.starts()
        cand = [k for k, s in enumerate(segs)
                if getattr(s.media, "path", None) == b.path and s.in_s - 1e-6 <= b.src <= s.out_s + 1e-6]
        if not cand:
            return None
        k = next((k for k in cand if segs[k] is b.ref), None)
        if k is None:
            k = min(cand, key=lambda k: abs(st[k] - b.last))
        s = segs[k]
        b.ref = s
        b.last = st[k] + (b.src - s.in_s) / max(s.speed, 1e-6)
        return b.last

    def attach(self, b, t):
        """Glue b to the clip under timeline time t. False when there is no clip there."""
        seq = self.api.seq
        segs = seq.segs
        if not segs:
            return False
        st, t = seq.starts(), min(max(0.0, t), seq.total())
        for k, s in enumerate(segs):
            d = (s.out_s - s.in_s) / max(s.speed, 1e-6)
            if st[k] <= t < st[k] + d or (k == len(segs) - 1 and t >= st[k]):
                b.path, b.ref = getattr(s.media, "path", None), s
                b.src = min(s.out_s, s.in_s + (t - st[k]) * s.speed)
                b.last = t
                return True
        return False

    def visible(self):
        return sorted(((b, t) for b in self.items for t in [self.where(b)] if t is not None), key=lambda q: q[1])

    def hit(self, x, y):
        tl = self.api.tl
        vis = self.visible()
        for b, t in reversed(vis):
            r = self.rects.get(b.id)
            if r is not None and r.contains(x, y):
                return b
        for b, t in reversed(vis):
            if abs(x - tl.tx(t)) <= 5:
                return b
        return None

    # ---- editing
    def new_color(self):
        return QColor.fromHsv(random.randrange(360), random.randint(170, 255), random.randint(200, 255)).name()

    def place(self, t):
        before = self.api.seq.snapshot()
        self.n += 1
        b = BM(None, 0.0, f"Bookmark {self.n}", self.new_color())
        if not self.attach(b, t):
            self.n -= 1
            self.api.status("Bookmarks: click on the timeline over a clip.")
            return
        self.items.append(b)
        self.sel, self.prim = {b.id}, b.id
        self.record(before)

    def rename(self, b):
        txt, ok = QInputDialog.getText(self.api.win, "Rename bookmark", "Name:", text=b.name)
        if ok and txt.strip() and txt.strip() != b.name:
            before = self.api.seq.snapshot()
            b.name = txt.strip()
            self.record(before)

    def delete_selected(self):
        if not self.sel:
            return False
        before = self.api.seq.snapshot()
        self.items = [b for b in self.items if b.id not in self.sel]
        self.sel, self.prim = set(), None
        self.record(before)
        return True

    def delete_all(self):
        if self.items:
            before = self.api.seq.snapshot()
            self.items, self.sel, self.prim = [], set(), None
            self.record(before)

    def menu(self, b, pos):
        if b is not None and b.id not in self.sel:
            self.sel, self.prim = {b.id}, b.id
            self.changed()
        m = QMenu(self.api.win)
        a_ren = a_go = a_del = None
        if b is not None:
            a_ren = m.addAction("Rename...\tF2")
            a_go = m.addAction("Go to bookmark")
            m.addSeparator()
            a_del = m.addAction(f"Delete {len(self.sel)} bookmarks\tDel" if len(self.sel) > 1 else "Delete bookmark\tDel")
        a_all = m.addAction("Delete all bookmarks")
        a_all.setEnabled(bool(self.items))
        r = m.exec(self.api.tl.mapToGlobal(pos.toPoint()))
        if r is None:
            return
        if r is a_ren:
            self.rename(b)
        elif r is a_go:
            t = self.where(b)
            if t is not None:
                self.api.engine.seek(t, play=False)
        elif r is a_del:
            self.delete_selected()
        elif r is a_all:
            self.delete_all()

    # ---- events (installed on the Timeline while the tool is selected)
    def eventFilter(self, o, ev):
        if not self.on:
            return False
        T, B, tl = QEvent.Type, Qt.MouseButton, self.api.tl
        t = ev.type()
        if t == T.ContextMenu:
            return True
        if t == T.ShortcutOverride:                       # let F2 / Delete / Ctrl+A reach us instead of the window shortcuts
            k, ctrl = ev.key(), bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if self.items and ((k in (Qt.Key.Key_F2, Qt.Key.Key_Delete) and self.sel) or (ctrl and k == Qt.Key.Key_A)):
                ev.accept()
                return True
            return False
        if t == T.KeyPress:
            k = ev.key()
            if k == Qt.Key.Key_F2 and self.sel:
                b = next((b for b in self.items if b.id == self.prim), None) or next(b for b in self.items if b.id in self.sel)
                self.rename(b)
                return True
            if k == Qt.Key.Key_Delete and self.sel:
                self.delete_selected()
                return True
            if k == Qt.Key.Key_A and ev.modifiers() & Qt.KeyboardModifier.ControlModifier and self.items:
                self.sel = {b.id for b, _ in self.visible()}
                self.changed()
                return True
            return False
        if t not in (T.MouseButtonPress, T.MouseButtonRelease, T.MouseButtonDblClick, T.MouseMove):
            return False
        pos = ev.position()
        if t == T.MouseMove:
            if self.drag and ev.buttons() & B.LeftButton:
                b, before, x0, moved = self.drag
                if moved or abs(pos.x() - x0) > 3:
                    self.drag = (b, before, x0, True)
                    self.collapse = None
                    self.attach(b, self.drag_time(pos.x()))      # playhead snap first, then video cut points
                    self.changed()
                return True
            near = not (ev.buttons() & B.LeftButton) and pos.x() >= tl.HW and self.hit(pos.x(), pos.y()) is not None
            if near:
                tl.setCursor(Qt.CursorShape.SizeHorCursor)
                self.hov = True
                return True
            if self.hov:
                self.hov = False
                tl.unsetCursor()
            return self.eat
        if t == T.MouseButtonRelease:
            if self.drag:
                b, before, x0, moved = self.drag
                self.drag = None
                if moved:
                    self.record(before)
            if self.collapse:
                self.sel, self.prim = {self.collapse}, self.collapse
                self.collapse = None
                self.changed()
            if self.eat:
                self.eat = False
                tl.unsetCursor()
                self.hov = False
                return True
            return False
        if pos.x() < tl.HW:
            return False
        tl.setFocus()                                     # so F2 / Delete / Ctrl+A arrive here
        b = self.hit(pos.x(), pos.y())
        if ev.button() == B.RightButton:
            if t == T.MouseButtonPress:
                self.eat = True
                self.menu(b, pos)
                self.eat = False
            return True
        if ev.button() != B.LeftButton:
            return False
        self.eat = True
        if b is None:
            self.place(tl.snap_t(max(0.0, tl.xt(pos.x()))))
            return True
        if t == T.MouseButtonDblClick:
            self.rename(b)
            return True
        mods = ev.modifiers()
        self.collapse = None
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.sel ^= {b.id}
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self.sel.add(b.id)
        elif b.id not in self.sel:
            self.sel = {b.id}
        elif len(self.sel) > 1:
            self.collapse = b.id                          # becomes a single selection on release unless dragged
        self.prim = b.id
        self.drag = (b, self.api.seq.snapshot(), pos.x(), False)
        self.changed()
        return True


def on_load(api):
    global S
    st = S = State(api)
    st.lane = BmLane(st)
    api.tl.lanes.append(st.lane)
    api.tl.installEventFilter(st)
    api.seq.ext[NAME] = st
    st.econn = api.seq.edited.connect(st.changed)


def on_tool_selected(api):
    if S:
        S.on = True
        api.status("Bookmarks: click = new bookmark, F2/double-click = rename, Del = delete, right-click = menu.")


def on_tool_deselected(api):
    if S:
        S.on, S.hov, S.drag, S.eat = False, False, None, False
        S.sel = set()                                     # so the Delete key keeps meaning "delete clip" for other tools
        api.tl.unsetCursor()
        S.changed()


def on_clear(api):
    if S:
        S.items, S.sel, S.prim, S.n = [], set(), None, 0
        S.changed()


def on_unload(api):
    global S
    st = S
    if st is None:
        return
    try:
        api.seq.edited.disconnect(st.econn)
    except Exception:
        pass
    api.tl.removeEventFilter(st)
    if st.lane in api.tl.lanes:
        api.tl.lanes.remove(st.lane)
    api.seq.ext.pop(NAME, None)
    api.tl._invalidate_content()
    S = None
