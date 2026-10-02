MOD_NAME = "Text"
MOD_TYPE = "tool"
MOD_ICON = "T"
MOD_DESC = "Add text on the video: click the preview to place it, drag/resize it, double-click for font, size, colour..."
MOD_AUTHOR = "SQVCE"

# Text clips live on their own lane under the video clips (default 4 s), not in the magnetic clip list.
# Preview = TextOverlay (child of the stage); export = EXPORT_HOOKS pass that burns PNGs of the same painter in.
import os
import uuid
from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QImage, QTextOption, QRegion
from PySide6.QtWidgets import (QWidget, QPlainTextEdit, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton,
                               QFontComboBox, QDoubleSpinBox, QCheckBox, QComboBox, QSpinBox, QColorDialog, QMenu,
                               QDialogButtonBox, QLabel, QApplication)
import export_worker
import probing
import utils

DEFAULT_DUR, MIN_DUR, ROW_H, HS = 4.0, 0.3, 20, 6
S = None
NAME = "text_tool"


class TItem:
    def __init__(self, t0, dur, x, y, w, h):
        self.t0, self.dur, self.x, self.y, self.w, self.h = t0, dur, x, y, w, h
        self.text, self.family, self.size = "Type Here", "Segoe UI", 0.07      # size = fraction of frame height
        self.bold = self.italic = self.underline = False
        self.color, self.align = "#ffffff", 1                                  # align 0 left / 1 centre / 2 right
        self.bg, self.bg_a = "#000000", 0                                      # background box colour + alpha 0-255
        self.outline, self.outline_w = "#000000", 0.0                          # outline px at 1080p
        self.opacity = 100
        self.row = 0
        self.grp = None                                                        # [52.22] cross-layer group id (Sequence.groups)
        self.id = uuid.uuid4().hex


def paint_item(p, r, it, fh, text=None):
    """THE text painter - used by the preview and by the export PNGs, so both look the same. r = box, fh = frame height px."""
    p.save()
    p.setOpacity(it.opacity / 100.0)
    p.setClipRect(r)
    if it.bg_a:
        c = QColor(it.bg)
        c.setAlpha(it.bg_a)
        p.fillRect(r, c)
    px = max(1, int(round(it.size * fh)))
    f = QFont(it.family)
    f.setPixelSize(px)
    f.setBold(it.bold)
    f.setItalic(it.italic)
    f.setUnderline(it.underline)
    p.setFont(f)
    pad = px * 0.15
    tr = r.adjusted(pad, pad, -pad, -pad)
    fl = int(Qt.AlignmentFlag.AlignTop | (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter,
                                           Qt.AlignmentFlag.AlignRight)[it.align] | Qt.TextFlag.TextWordWrap)
    s = it.text if text is None else text
    ow = it.outline_w * fh / 1080.0
    if ow > 0.2:
        p.setPen(QColor(it.outline))
        for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
            p.drawText(tr.translated(dx * ow, dy * ow), fl, s)
    p.setPen(QColor(it.color))
    p.drawText(tr, fl, s)
    p.restore()


class _Ed(QPlainTextEdit):
    done = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.done.emit()
        elif e.key() == Qt.Key.Key_Escape:
            self.done.emit()
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.done.emit()


class State:
    def __init__(self, api):
        self.api, self.items, self.sel, self.selset, self.tool_on = api, [], None, set(), False
        self.hook, self.rows = None, 0

    def total(self):
        return self.api.seq.total()

    # --- history (Ctrl+Z): state rides in Sequence snapshots via seq.ext
    def ext_snapshot(self):
        return [dict(i.__dict__) for i in self.items]

    def ext_restore(self, data):
        ids = {i.id for i in self.selset}
        items = []
        for d in data:
            it = TItem(0, 0, 0, 0, 0, 0)
            it.__dict__.update(d)
            items.append(it)
        self.items = items
        self.selset = {i for i in items if i.id in ids}
        self.sel = next(iter(self.selset), None)
        self.changed()

    def begin(self):
        return self.api.seq.snapshot()

    def record(self, before):
        ext = getattr(before, "ext", None) or {}
        if self.ext_snapshot() != ext.get(NAME):
            self.api.seq.commit(before)
            self.api.seq.edited.emit()

    def limits(self, row, t0, end, exclude):
        """Free span (left, right) around [t0, end] on a layer, or None if it already overlaps something there."""
        left, right = 0.0, float("inf")
        for j in self.items:
            if j in exclude or j.row != row:
                continue
            je = j.t0 + j.dur
            if j.t0 >= end - 1e-9:
                right = min(right, j.t0)
            elif je <= t0 + 1e-9:
                left = max(left, je)
            else:
                return None
        return left, right

    def _drop_video_sel(self):
        tl = self.api.tl                       # ONE selection across all layers: text and video exclude each other
        if tl.sel != -1 or tl.multi_sel:
            tl.sel, tl.multi_sel = -1, set()

    def select(self, it, add=False):
        if it is not None:
            self._drop_video_sel()
        if it is None:
            self.sel = None
            self.selset.clear()
        elif add:
            if it in self.selset:
                self.selset.discard(it)
                self.sel = next(iter(self.selset), None)
            else:
                self.selset.add(it)
                self.sel = it
        else:
            self.sel, self.selset = it, {it}
        self.refresh()

    def refresh(self):
        self.api.tl._invalidate_content()
        self.ov.sync_mask()
        self.ov.update()

    def z_items(self):
        return sorted(self.items, key=lambda i: i.row)          # stable: higher layer = drawn on top

    def compact(self):
        used = sorted({i.row for i in self.items})
        m = {r: n for n, r in enumerate(used)}
        for i in self.items:
            i.row = m[i.row]
        self.rows = len(used)

    def changed(self):
        self.compact()
        self.selset &= set(self.items)
        if self.sel not in self.items:
            self.sel = next(iter(self.selset), None)
        self.lane.sync()
        self.ov.sync_mask()
        self.ov.update()

    def add(self, x, y, new_layer=False, w=0.4, h=0.14):
        b = self.begin()
        tot, ph = self.total(), self.api.engine.playhead
        t0 = max(0.0, ph)                                  # always AT the playhead
        row = (max((i.row for i in self.items), default=-1) + 1) if (new_layer and self.items) else 0
        maxd = DEFAULT_DUR
        if not (new_layer and self.items):                 # layer 0: only if the spot is free; never adds a 2nd clip elsewhere
            same = [j for j in self.items if j.row == 0]
            if any(j.t0 < t0 + MIN_DUR - 1e-9 and j.t0 + j.dur > t0 + 1e-9 for j in same):
                self.api.status("This layer is occupied here - Shift+click to add text on a new layer.", 5000)
                return None
            maxd = min([DEFAULT_DUR] + [j.t0 - t0 for j in same if j.t0 >= t0 + MIN_DUR - 1e-9])
        dur = max(MIN_DUR, min(maxd, tot - t0) if tot > 0 else maxd)
        it = TItem(t0, max(dur, MIN_DUR), min(max(x - w / 2, 0), 1 - w), min(max(y - h / 2, 0), 1 - h), w, h)
        it.row = row
        self.items.append(it)
        self.sel, self.selset = it, {it}
        self._drop_video_sel()
        self.changed()
        self.record(b)
        return it

    def delete(self, its):
        b = self.begin()
        its = set(its) if not isinstance(its, TItem) else {its}
        self.items = [i for i in self.items if i not in its]
        self.changed()
        self.record(b)

    def delete_selected(self):
        if not self.selset:
            return False
        self.delete(set(self.selset))
        return True

    def duplicate(self, it):
        b = self.begin()
        n = TItem(it.t0, it.dur, it.x, it.y, it.w, it.h)
        for k, v in it.__dict__.items():
            if k not in ("id", "x", "y", "grp"):
                setattr(n, k, v)
        tot, t0, want = self.total(), it.t0 + it.dur, it.dur       # next free space on the same layer, shortened to fit
        for _ in range(len(self.items) + 2):
            hit = next((j for j in sorted(self.items, key=lambda i: i.t0)
                        if j.row == it.row and j.t0 < t0 + want - 1e-9 and j.t0 + j.dur > t0 + 1e-9), None)
            if hit is None:
                break
            if hit.t0 <= t0 or hit.t0 - t0 < MIN_DUR:
                t0, want = hit.t0 + hit.dur, it.dur
            else:
                want = hit.t0 - t0
        if tot > 0:
            want = min(want, tot - t0)
        if want >= MIN_DUR - 1e-9:
            n.t0, n.dur = t0, want
        else:                                                      # no room left on this layer: new layer, same time
            n.t0, n.dur = it.t0, it.dur
            n.row = max(i.row for i in self.items) + 1
        self.items.append(n)
        self.sel, self.selset = n, {n}
        self._drop_video_sel()
        self.changed()
        self.record(b)

    def edit_dialog(self, it):
        self.select(it)
        b = self.begin()
        old = dict(it.__dict__)
        dlg = TextDialog(it, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            it.__dict__.update(old)
        self.changed()
        self.record(b)

    def item_menu(self, it, gpos, parent):
        if it not in self.selset:
            self.select(it)
        m = QMenu(parent if isinstance(parent, QWidget) else None)
        a_ed = m.addAction("Edit Text...")
        a_ed.setEnabled(len(self.selset) == 1)
        a_dup = m.addAction("Duplicate\tCtrl+D")
        a_del = m.addAction("Delete\tDel")
        a_grp = a_ung = None
        parent = self.api.tl                               # [52.23] `parent` may be the TextOverlay (preview right-click), not the Timeline
        if parent._group_plan():                           # [52.22] cross-layer grouping
            a_grp = m.addAction("Group Selected Clips")
        if parent._sel_gid() is not None:
            a_ung = m.addAction("Ungroup")
        a = m.exec(gpos)
        if a is not None and a is a_grp:
            parent.group_selected()
        elif a is not None and a is a_ung:
            parent.ungroup_selected()
        if a is a_ed:
            self.edit_dialog(it)
        elif a is a_dup:
            self.duplicate(it)
        elif a is a_del:
            self.delete_selected()


# --------------------------------------------------------------------------------------------- timeline lane
class TextLane:
    def __init__(self, st):
        self.st, self.drag, self.mq, self.mq_base = st, None, None, set()

    def height(self, tl):
        return (self.st.rows * ROW_H + 4) if self.st.items else 0

    def sync(self):
        tl = self.st.api.tl
        tl.refresh_lanes()
        tl.update()

    def _rect(self, tl, it, y0):
        x0, x1 = tl.tx(it.t0), tl.tx(it.t0 + it.dur)
        return QRectF(x0, y0 + it.row * ROW_H + 2, max(2.0, x1 - x0), ROW_H - 3)

    def paint(self, p, tl, y, W):
        for it in self.st.z_items():
            r = self._rect(tl, it, y)
            sel = it in self.st.selset
            p.setPen(QPen(QColor("#3d8f7a"), 1))
            p.setBrush(QColor(47, 111, 94))
            p.drawRoundedRect(r, 3, 3)
            if it.grp is not None:                   # [52.22] group colour bar
                p.fillRect(QRectF(r.left() + 2, r.bottom() - 3, max(1.0, r.width() - 4), 2),
                           QColor(tl.seq.groups.get(it.grp, {}).get("color", "#888888")))
            if sel:                                  # same selection box as video clips: white, 2 px, rounded
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r, 3, 3)
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(5, 0, -3, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       p.fontMetrics().elidedText("T  " + it.text.replace("\n", " "), Qt.TextElideMode.ElideRight,
                                                  max(1, int(r.width() - 8))))
        if self.mq is not None:
            p.setPen(QPen(QColor("#2d8ceb"), 1))
            p.setBrush(QColor(45, 140, 235, 45))
            p.drawRect(self.mq)

    def _hit(self, pos, tl):
        y0 = tl.lane_y()
        for it in reversed(self.st.z_items()):
            r = self._rect(tl, it, y0)
            if r.contains(pos):
                edge = None
                if r.width() > 20:
                    edge = "in" if pos.x() - r.left() <= HS else ("out" if r.right() - pos.x() <= HS else None)
                return it, edge
        return None, None

    def clear_sel(self, tl):
        if self.st.selset:
            self.st.select(None)

    def delete_key(self):
        return self.st.delete_selected()

    def duplicate_key(self):                # [52.20] Ctrl+D
        st = self.st
        if not st.selset:
            return False
        for it in sorted(st.selset, key=lambda i: i.t0):
            st.duplicate(it)
        return True

    def marquee(self, r, tl, add):          # called by Timeline's own marquee when it sweeps into the lane
        y0 = tl.lane_y()
        hit = {i for i in self.st.items if self._rect(tl, i, y0).intersects(r)}
        new = hit | (self.st.selset if add else set())
        if new != self.st.selset:
            self.st.selset, self.st.sel = new, next(iter(new), None)   # [52.21] box select keeps the other layers' selection
            self.st.refresh()

    def press(self, e, tl):
        st = self.st
        it, edge = self._hit(e.position(), tl)
        self.drag = self.mq = None
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if e.button() == Qt.MouseButton.RightButton:
            if it is not None:
                if it in st.selset:
                    tl._keep_vsel = True                   # [52.22] right-click inside a multi-layer selection keeps it
                st.item_menu(it, e.globalPosition().toPoint(), tl)
            return True
        if it is None:                       # [52.22] empty lane space: Timeline's box select (all layers) takes over
            return False
        if e.button() == Qt.MouseButton.RightButton:
            pass
        if ctrl:
            st.select(it, add=True)
            return True
        if it not in st.selset:
            st.select(it)
        else:
            st.sel = it
        if it.grp is not None and not edge:
            tl.select_group(it.grp)                        # [52.22] a grouped item selects its whole group (all layers)
        ph = st.api.engine.playhead
        if not (it.t0 <= ph < it.t0 + it.dur):
            st.api.engine.seek(it.t0, play=False)
        mode = "trim_in" if edge == "in" else "trim_out" if edge == "out" else "move"
        if mode != "move":
            st.select(it)
        self.drag = (mode, e.position().x(), {i: (i.t0, i.dur, i.row) for i in st.selset}, st.begin())
        st.refresh()
        return True

    def move(self, pos, tl):
        st = self.st
        if self.mq is not None:
            self.mq = QRectF(self.mq0, QPointF(max(pos.x(), tl.HW), pos.y())).normalized()
            y0 = tl.lane_y()
            hit = {i for i in st.items if self._rect(tl, i, y0).intersects(self.mq)}
            st.selset, st.sel = hit | self.mq_base, next(iter(hit | self.mq_base), None)
            if st.selset:
                st._drop_video_sel()
            st.refresh()
            return
        it = st.sel
        if not self.drag or it is None:
            return
        mode, x0, orig, _b = self.drag
        dt = (pos.x() - x0) / tl.pps
        tot = st.total()
        t0, dur, r0 = orig[it]
        ex = set(orig)
        if mode == "move":
            tl.setCursor(Qt.CursorShape.ClosedHandCursor)
            if tot > 0:
                dt = min(dt, max(0.0, tot - max(o[0] + o[1] for o in orig.values())))
            dt = max(dt, -min(o[0] for o in orig.values()))
            dt = tl.snap_span(t0 + dt, dur) - t0              # [52.20] snap start/end to video clip edges
            rows = {i: o[2] for i, o in orig.items()}
            if len(orig) == 1:                # vertical drag = change layer (Shift = may open a new one)
                maxr = max(i.row for i in st.items)
                shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
                row = int((pos.y() - tl.lane_y()) // ROW_H)
                row = max(0, min(row, maxr + (1 if shift else 0)))
                if row != r0 and st.limits(row, t0 + dt, t0 + dt + dur, ex) is None:
                    row = r0                  # the target layer is occupied at that time
                rows[it] = row
            lo, hi = -1e18, 1e18              # layers never overlap: clamp against the neighbours (no jumping over)
            for i, o in orig.items():
                lim = st.limits(rows[i], o[0], o[0] + o[1], ex)
                if lim:
                    lo, hi = max(lo, lim[0] - o[0]), min(hi, lim[1] - o[0] - o[1])
            dt = max(lo, min(hi, dt)) if lo <= hi else 0.0
            for i, o in orig.items():
                i.t0 = o[0] + dt
            if rows[it] != it.row:
                it.row = rows[it]
                st.rows = max(i.row for i in st.items) + 1
                self.sync()
        elif mode == "trim_in":
            end = t0 + dur
            left = (st.limits(r0, t0, end, ex) or (0.0, 0))[0]
            it.t0 = max(left, min(max(0.0, tl.snap_t(t0 + dt)), end - MIN_DUR))
            it.dur = end - it.t0
        else:
            right = (st.limits(r0, t0, t0 + dur, ex) or (0, 1e18))[1]
            d = max(MIN_DUR, tl.snap_t(t0 + dur + dt) - t0)
            if tot > 0:
                d = max(MIN_DUR, min(d, tot - t0))
            it.dur = max(MIN_DUR, min(d, right - t0))
        st.refresh()

    def cursor(self, pos, tl):
        if pos.y() > tl.V_Y + tl.V_H:
            it, edge = self._hit(pos, tl)
            if edge:
                return Qt.CursorShape.SizeHorCursor
        return None

    def nav_edit(self, d):
        """[52.17] Up/Down with a text clip selected: go to the previous/next clip on the SAME layer (stays at the ends).
        Returns the time to seek to, or None when nothing here is selected (core then navigates video edits)."""
        st = self.st
        it = st.sel
        if it is None or it not in st.items or it not in st.selset:
            return None
        row = sorted((i for i in st.items if i.row == it.row), key=lambda i: i.t0)
        tgt = row[max(0, min(len(row) - 1, row.index(it) + d))]
        if tgt is not it:
            st.select(tgt)
        return tgt.t0

    def snap_points(self):
        return [x for i in self.st.items for x in (i.t0, i.t0 + i.dur)]

    # [52.21] selection-layer / Alt+Left/Right API used by MainWindow.move_clip + goto_edit
    def sel_layers(self):
        return {("text", i.row) for i in self.st.selset}

    # [52.22] generic lane API for cross-layer groups
    def sel_items(self):
        return list(self.st.selset)

    def all_items(self):
        return self.st.items

    def select_grp(self, gid, add=False):
        g = {i for i in self.st.items if i.grp == gid}
        new = (self.st.selset | g) if add else g
        if new != self.st.selset:
            self.st.selset, self.st.sel = new, next(iter(new), None)
            self.st.refresh()

    def shift_sel(self, dt, dry=False):
        """Move every selected item by dt seconds (mixed-layer group move). False = would overlap / leave 0."""
        st, ex = self.st, set(self.st.selset)
        for it in ex:
            if it.t0 + dt < -1e-9 or st.limits(it.row, it.t0 + dt, it.t0 + dt + it.dur, ex) is None:
                return False
        if not dry:
            for it in ex:
                it.t0 = max(0.0, it.t0 + dt)
            st.changed()
            st.refresh()
        return True

    def hop_sel(self, d):
        """Selected items (one layer, consecutive) swap places with the neighbouring item on that layer."""
        st = self.st
        if not st.selset:
            return False
        row = sorted((i for i in st.items if i.row == next(iter(st.selset)).row), key=lambda i: i.t0)
        idx = [k for k, i in enumerate(row) if i in st.selset]
        if idx != list(range(idx[0], idx[-1] + 1)):
            return False
        j = idx[-1] + 1 if d > 0 else idx[0] - 1
        if not 0 <= j < len(row):
            return False
        n, grp = row[j], [row[k] for k in idx]
        g0, g1 = grp[0].t0, grp[-1].t0 + grp[-1].dur
        if d > 0:
            dx = n.t0 + n.dur - g1
            n.t0 = g0
        else:
            dx = n.t0 - g0
            n.t0 = g1 - n.dur
        for it in grp:
            it.t0 += dx
        st.changed()
        st.refresh()
        return True

    def select_all(self):
        self.st.selset = set(self.st.items)
        self.st.sel = next(iter(self.st.selset), None)
        self.st.refresh()

    def release(self, e, tl):
        d, self.drag, self.mq = self.drag, None, None
        tl.setCursor(Qt.CursorShape.ArrowCursor)
        self.st.changed()
        if d:
            self.st.record(d[3])
        tl._invalidate_content()

    def dbl(self, e, tl):
        it, _ = self._hit(e.position(), tl)
        if it is None:
            return False
        self.drag = None
        self.st.edit_dialog(it)
        return True


# --------------------------------------------------------------------------------------------- preview overlay
class TextOverlay(QWidget):
    _reg = None

    """Always on top of the preview. Text tool active: covers it all (click = new text). Otherwise it is masked down to
    the visible text boxes, so only text can be grabbed (Select tool) and everything else clicks through to the video."""
    def __init__(self, st, parent):
        super().__init__(parent)
        self.st, self.drag, self.ed, self.ed_item, self._mkey = st, None, None, None, None
        self._reg = QRegion()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def frame(self):
        dw, dh = self.st.api.stage._disp_dims()
        if not dw or not dh:
            return None
        k = min(self.width() / dw, self.height() / dh)
        return QRectF((self.width() - dw * k) / 2, (self.height() - dh * k) / 2, dw * k, dh * k)

    def active_items(self):
        t = self.st.api.engine.playhead
        return [i for i in self.st.z_items() if i.t0 <= t < i.t0 + i.dur]

    def box(self, F, it):
        return QRectF(F.x() + it.x * F.width(), F.y() + it.y * F.height(), it.w * F.width(), it.h * F.height())

    def sync_mask(self):
        F = self.frame()
        if F is None or self.st.api.stage.tool is not None:
            key = "none"
        elif self.st.tool_on or self.ed is not None:
            key = "all"
        else:
            key = tuple((round(r.x()), round(r.y()), round(r.width()), round(r.height()))
                        for r in (self.box(F, i) for i in self.active_items()))
        if key == self._mkey:
            return
        self._mkey = key
        if key == "all":
            reg = QRegion(self.rect())
            self.clearMask()
        elif key == "none" or not key:
            reg = QRegion(0, 0, 1, 1)
            self.setMask(reg)
        else:
            m = HS + 3
            reg = QRegion()
            for x, y, w, h in key:
                reg = reg.united(QRegion(x - m, y - m, w + 2 * m, h + 2 * m))
            self.setMask(reg)
        # [52.12] ghosting fix: the area that just left the mask belongs to the video below - make the parent repaint it
        old, self._reg = self._reg, reg
        par = self.parentWidget()
        if par is not None:
            par.update(old.united(reg))

    def update(self, *a):
        super().update(*a)
        par = self.parentWidget()
        if par is not None and not a and self._reg is not None:
            par.update(self._reg.boundingRect())       # [52.12] stale text pixels are cleared by repainting what is below

    def request_sync(self):
        """[52.15] Mask/repaint work is deferred to the next event-loop turn (never inside the engine's seek/tick stack)."""
        if getattr(self, "_pend", False):
            return
        self._pend = True
        QTimer.singleShot(0, self._do_sync)

    def _do_sync(self):
        self._pend = False
        try:
            self.sync_mask()
            self.update()
        except Exception:
            pass

    def set_tool_on(self, on):
        self.st.tool_on = on
        if not on:
            self.commit_edit()
        self.sync_mask()
        self.update()

    def paintEvent(self, e):
        F = self.frame()
        if F is None or self.st.api.stage.tool is not None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        act = self.active_items()
        p.setClipRect(F)
        for it in act:
            if it is not self.ed_item:
                paint_item(p, self.box(F, it), it, F.height())
        p.setClipping(False)
        for it in act:
            if it in self.st.selset:
                r = self.box(F, it)
                multi = len(self.st.selset) > 1
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#2d8ceb") if self.st.tool_on else QColor("#ffd24a"), 1.4,
                              Qt.PenStyle.DashLine if self.st.tool_on else Qt.PenStyle.SolidLine))
                p.drawRect(r)
                if it is self.st.sel and not multi:
                    p.setPen(QPen(QColor("#2d8ceb"), 1))
                    p.setBrush(QColor("#ffffff"))
                    for _, c in self.handles(r):
                        p.drawRect(QRectF(c.x() - HS / 2, c.y() - HS / 2, HS, HS))

    @staticmethod
    def handles(r):
        cx, cy = r.center().x(), r.center().y()
        return [("tl", r.topLeft()), ("t", QPointF(cx, r.top())), ("tr", r.topRight()), ("r", QPointF(r.right(), cy)),
                ("br", r.bottomRight()), ("b", QPointF(cx, r.bottom())), ("bl", r.bottomLeft()), ("l", QPointF(r.left(), cy))]

    def hit(self, pos, F):
        s, act = self.st.sel, self.active_items()
        if s in act and len(self.st.selset) == 1:
            r = self.box(F, s)
            for n, c in self.handles(r):
                if abs(pos.x() - c.x()) <= HS + 1 and abs(pos.y() - c.y()) <= HS + 1:
                    return s, n
        for it in reversed(act):
            if self.box(F, it).contains(pos):
                return it, "move"
        return None, None

    CUR = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
           "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
           "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
           "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor, "move": Qt.CursorShape.SizeAllCursor}

    def mousePressEvent(self, e):
        F = self.frame()
        if F is None:
            return
        self.setFocus()
        self.commit_edit()
        it, how = self.hit(e.position(), F)
        if e.button() == Qt.MouseButton.RightButton:
            if it is not None:
                self.st.item_menu(it, e.globalPosition().toPoint(), self)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if it is None:
            if self.st.tool_on and F.contains(e.position()) and self.st.api.seq.segs:
                self.st.api.engine.pause()
                shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)     # Shift+click = new layer
                self.st.add((e.position().x() - F.x()) / F.width(), (e.position().y() - F.y()) / F.height(), shift)
            elif not ctrl:
                self.st.select(None)
            return                                                                   # new text starts in NON-edit mode
        if ctrl:
            self.st.select(it, add=True)
            return
        if it not in self.st.selset:
            self.st.select(it)
        else:
            self.st.sel = it
        self.drag = (how, e.position(), {i: (i.x, i.y, i.w, i.h) for i in self.st.selset}, self.st.begin())
        self.st.refresh()

    def mouseMoveEvent(self, e):
        F = self.frame()
        if F is None:
            return
        if not self.drag:
            it, how = self.hit(e.position(), F)
            self.setCursor(self.CUR.get(how, Qt.CursorShape.ArrowCursor) if it else
                           (Qt.CursorShape.IBeamCursor if self.st.tool_on else Qt.CursorShape.ArrowCursor))
            return
        how, p0, orig, _b = self.drag
        dx, dy = (e.position().x() - p0.x()) / F.width(), (e.position().y() - p0.y()) / F.height()
        mn = 0.03
        if how == "move":
            dx = max(dx, -min(o[0] for o in orig.values()))
            dx = min(dx, min(1 - o[0] - o[2] for o in orig.values()))
            dy = max(dy, -min(o[1] for o in orig.values()))
            dy = min(dy, min(1 - o[1] - o[3] for o in orig.values()))
            for i, (x, y, w, h) in orig.items():
                i.x, i.y = x + dx, y + dy
        else:
            it = self.st.sel
            x, y, w, h = orig[it]
            l, t, r, b = x, y, x + w, y + h
            if "l" in how:
                l = min(max(l + dx, 0.0), r - mn)
            if "r" in how:
                r = max(min(r + dx, 1.0), l + mn)
            if "t" in how:
                t = min(max(t + dy, 0.0), b - mn)
            if "b" in how:
                b = max(min(b + dy, 1.0), t + mn)
            it.x, it.y, it.w, it.h = l, t, r - l, b - t
        self.sync_mask()
        self.update()

    def mouseReleaseEvent(self, e):
        d, self.drag = self.drag, None
        if d:
            self.st.changed()
            self.st.record(d[3])

    def mouseDoubleClickEvent(self, e):
        F = self.frame()
        if F is None or e.button() != Qt.MouseButton.LeftButton:
            return
        it, _ = self.hit(e.position(), F)
        if it is not None:
            self.drag = None
            self.st.select(it)
            self.start_edit(it)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Delete and self.st.selset:
            self.st.delete_selected()
        elif e.key() == Qt.Key.Key_Escape:
            self.st.select(None)
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.st.sel is not None:
            self.start_edit(self.st.sel)
        else:
            super().keyPressEvent(e)

    # inline editing (double-click / Enter on a selected text): Enter = save, Shift+Enter = new line, Esc / click away = save
    def start_edit(self, it):
        F = self.frame()
        if F is None or it is None:
            return
        self.commit_edit()
        self._mkey = None
        self._eb = self.st.begin()
        self.ed_item = it
        ed = _Ed(self)
        self.ed = ed
        self.sync_mask()
        px = max(1, int(round(it.size * F.height())))
        f = QFont(it.family)
        f.setPixelSize(px)
        f.setBold(it.bold)
        f.setItalic(it.italic)
        f.setUnderline(it.underline)
        ed.setFont(f)
        bg = QColor(it.bg)
        bg.setAlpha(it.bg_a)
        ed.setStyleSheet("QPlainTextEdit{background:rgba(%d,%d,%d,%d);color:%s;border:1px solid #2d8ceb;padding:0;}"
                         % (bg.red(), bg.green(), bg.blue(), bg.alpha(), it.color))
        ed.document().setDocumentMargin(px * 0.15)
        ed.document().setDefaultTextOption(QTextOption((Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter,
                                                        Qt.AlignmentFlag.AlignRight)[it.align]))
        ed.setPlainText(it.text)
        ed.setGeometry(self.box(F, it).toRect())
        ed.done.connect(self.commit_edit)
        ed.show()
        ed.selectAll()
        ed.setFocus()
        self.update()

    def abort_edit(self):
        ed, self.ed, self.ed_item = self.ed, None, None
        if ed is not None:
            try:
                ed.done.disconnect()
            except Exception:
                pass
            ed.hide()
            ed.deleteLater()
        self._mkey = None

    def commit_edit(self):
        ed, it = self.ed, self.ed_item
        if ed is None:
            return
        self.ed = self.ed_item = None
        txt = ed.toPlainText()
        try:
            ed.done.disconnect()
        except Exception:
            pass
        ed.hide()
        ed.deleteLater()
        if not txt.strip():
            self.st.delete(it)
        else:
            it.text = txt
            self.st.changed()
            self.st.record(self._eb)
        self.setFocus()
        self._mkey = None
        self.sync_mask()
        self.update()


# --------------------------------------------------------------------------------------------- options dialog
class _ColorBtn(QPushButton):
    changed = Signal()

    def __init__(self, color):
        super().__init__()
        self.setFixedWidth(60)
        self.set(color)
        self.clicked.connect(self._pick)

    def set(self, c):
        self.c = c
        self.setStyleSheet("background:%s;border:1px solid #777;" % c)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.c), self)
        if c.isValid():
            self.set(c.name())
            self.changed.emit()


class TextDialog(QDialog):
    def __init__(self, it, st):
        super().__init__(st.api.win)
        self.it, self.st = it, st
        self.setWindowTitle("Text options")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        self.txt = QPlainTextEdit(it.text)
        self.txt.setFixedHeight(70)
        lay.addWidget(self.txt)
        f = QFormLayout()
        lay.addLayout(f)
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(it.family))
        f.addRow("Font", self.font)
        self.size = QDoubleSpinBox()
        self.size.setRange(4, 1000)
        self.size.setDecimals(0)
        self.size.setSuffix(" px (1080p)")
        self.size.setValue(round(it.size * 1080))
        f.addRow("Size", self.size)
        row = QHBoxLayout()
        self.b, self.i, self.u = QCheckBox("Bold"), QCheckBox("Italic"), QCheckBox("Underline")
        for w, v in ((self.b, it.bold), (self.i, it.italic), (self.u, it.underline)):
            w.setChecked(v)
            row.addWidget(w)
        f.addRow("Style", row)
        self.align = QComboBox()
        self.align.addItems(["Left", "Center", "Right"])
        self.align.setCurrentIndex(it.align)
        f.addRow("Align", self.align)
        self.color = _ColorBtn(it.color)
        f.addRow("Text colour", self.color)
        self.bg = _ColorBtn(it.bg)
        self.bg_a = QSpinBox()
        self.bg_a.setRange(0, 100)
        self.bg_a.setSuffix(" %")
        self.bg_a.setValue(round(it.bg_a / 2.55))
        r2 = QHBoxLayout()
        r2.addWidget(self.bg)
        r2.addWidget(self.bg_a)
        f.addRow("Background", r2)
        self.ol = _ColorBtn(it.outline)
        self.ol_w = QDoubleSpinBox()
        self.ol_w.setRange(0, 30)
        self.ol_w.setSingleStep(0.5)
        self.ol_w.setValue(it.outline_w)
        r3 = QHBoxLayout()
        r3.addWidget(self.ol)
        r3.addWidget(self.ol_w)
        f.addRow("Outline", r3)
        self.op = QSpinBox()
        self.op.setRange(0, 100)
        self.op.setSuffix(" %")
        self.op.setValue(it.opacity)
        f.addRow("Opacity", self.op)
        self.t0, self.dur = QDoubleSpinBox(), QDoubleSpinBox()
        for w, v in ((self.t0, it.t0), (self.dur, it.dur)):
            w.setRange(0, 36000)
            w.setDecimals(2)
            w.setSingleStep(0.1)
            w.setSuffix(" s")
            w.setValue(v)
        self.dur.setMinimum(MIN_DUR)
        f.addRow("Start", self.t0)
        f.addRow("Duration", self.dur)
        self.pos = []
        r4 = QHBoxLayout()
        for lab, v in (("X", it.x), ("Y", it.y), ("W", it.w), ("H", it.h)):
            s = QSpinBox()
            s.setRange(0, 100)
            s.setSuffix("%")
            s.setValue(round(v * 100))
            self.pos.append(s)
            r4.addWidget(QLabel(lab))
            r4.addWidget(s)
        f.addRow("Box", r4)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.txt.textChanged.connect(self.apply)
        self.font.currentFontChanged.connect(self.apply)
        for w in (self.size, self.ol_w, self.t0, self.dur):
            w.valueChanged.connect(self.apply)
        for w in (self.bg_a, self.op, *self.pos):
            w.valueChanged.connect(self.apply)
        for w in (self.b, self.i, self.u):
            w.toggled.connect(self.apply)
        self.align.currentIndexChanged.connect(self.apply)
        for w in (self.color, self.bg, self.ol):
            w.changed.connect(self.apply)

    def apply(self, *_):
        it = self.it
        it.text = self.txt.toPlainText() or " "
        it.family = self.font.currentFont().family()
        it.size = self.size.value() / 1080.0
        it.bold, it.italic, it.underline = self.b.isChecked(), self.i.isChecked(), self.u.isChecked()
        it.align, it.color = self.align.currentIndex(), self.color.c
        it.bg, it.bg_a = self.bg.c, int(round(self.bg_a.value() * 2.55))
        it.outline, it.outline_w, it.opacity = self.ol.c, self.ol_w.value(), self.op.value()
        it.t0, it.dur = self.t0.value(), self.dur.value()
        x, y, w, h = (s.value() / 100.0 for s in self.pos)
        w, h = max(w, 0.03), max(h, 0.03)
        it.x, it.y, it.w, it.h = min(x, 1 - w), min(y, 1 - h), w, h
        self.st.changed()


# --------------------------------------------------------------------------------------------- export
class TextExport:
    def __init__(self, st):
        self.st = st

    def active(self, worker):
        return bool(self.st.items) and bool(utils.FFMPEG)

    def run(self, worker, src, tmpdir, ext):
        m = probing.probe_media(src)
        W, H = (int(m.w), int(m.h)) if m and m.w and m.h else (1920, 1080)
        items = self.st.z_items()
        args, chain, last = ["-i", src], [], "0:v"
        for n, it in enumerate(items, 1):
            w, h = max(2, int(round(it.w * W))), max(2, int(round(it.h * H)))
            img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            q = QPainter(img)
            q.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            paint_item(q, QRectF(0, 0, w, h), it, H)
            q.end()
            png = os.path.join(tmpdir, f"txt{n}.png")
            img.save(png, "PNG")
            args += ["-i", png]
            out = f"[o{n}]"
            chain.append(f"[{last}][{n}:v]overlay={int(round(it.x * W))}:{int(round(it.y * H))}:"
                         f"enable='between(t,{it.t0:.3f},{it.t0 + it.dur:.3f})':format=auto{out}")
            last = out[1:-1]
        chain.append(f"[{last}]format=yuv420p[vout]")
        dst = os.path.join(tmpdir, "txt_out" + ext)
        vc = (["-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0", "-cpu-used", "4", "-row-mt", "1"] if ext.lower() == ".webm"
              else ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-threads", "0"])
        worker._ffmpeg(args + ["-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "0:a?"] + vc
                       + ["-c:a", "copy", dst])
        return dst


# --------------------------------------------------------------------------------------------- mod hooks
def on_load(api):
    global S
    st = S = State(api)
    st.ov = TextOverlay(st, api.stage)
    st.ov.setGeometry(api.stage.rect())
    st.ov.show()
    st.lane = TextLane(st)
    api.tl.lanes.append(st.lane)
    st.hook = TextExport(st)
    api.seq.ext[NAME] = st
    export_worker.EXPORT_HOOKS.append(st.hook)
    stage = api.stage
    orig = stage.relayout                                  # the stage raises its own children on every relayout

    def relayout():
        orig()
        st.ov.setGeometry(stage.rect())
        st.ov.raise_()
        st.ov._mkey = None
        st.ov.request_sync()
    stage.relayout = relayout
    st.relayout_patch = True
    st.conn = api.engine.playheadChanged.connect(lambda _t: st.ov.request_sync() if st.items else None)
    QTimer.singleShot(0, relayout)


def on_unload(api):
    global S
    st = S
    if st is None:
        return
    st.ov.commit_edit()
    try:
        api.engine.playheadChanged.disconnect(st.conn)
    except Exception:
        pass
    api.stage.__dict__.pop("relayout", None)
    api.seq.ext.pop(NAME, None)
    if st.lane in api.tl.lanes:
        api.tl.lanes.remove(st.lane)
    api.tl.refresh_lanes()
    if st.hook in export_worker.EXPORT_HOOKS:
        export_worker.EXPORT_HOOKS.remove(st.hook)
    st.ov.deleteLater()
    S = None


def on_clear(api):
    if S:
        S.ov.abort_edit()
        S.items, S.selset, S.sel = [], set(), None
        S.changed()


def on_tool_selected(api):
    if S:
        S.ov.set_tool_on(True)
        S.ov.raise_()
        api.status("Text tool: click the preview to add text (Enter saves, double-click edits, drag corners to resize).", 6000)


def on_tool_deselected(api):
    if S:
        S.ov.set_tool_on(False)
