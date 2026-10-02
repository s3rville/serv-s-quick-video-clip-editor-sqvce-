MOD_NAME = "GIFs"
MOD_TYPE = "tab"
MOD_DESC = "GIF browser: Tenor / KLIPY / GIPHY in a masonry grid with animated previews. Drag a GIF onto the timeline or the Project panel (imports it as MP4)."
MOD_AUTHOR = "SQVCE"

# [MAP] Tab mod (see plugins.py). One State object (S) owns everything; widgets: provider buttons, search box, key button, message label,
# Grid (masonry QScrollArea of Tile labels with QMovie previews), footer ("Powered by ...").
# NETWORK: stdlib urllib only, always inside a ThreadPoolExecutor (Bridge) - the GUI thread never blocks. Results come back through a Qt signal.
# PROVIDERS: each fetcher f_<name>(key, q, cursor, customer_id) -> (items, next_cursor). Item = plain dict (see mk()), JSON-serialisable
# because it travels inside the drag MIME. Empty query = trending/featured. Cursor: GIPHY offset, KLIPY page number, Tenor "pos" token.
# DOWNLOAD: previews (small .gif) are cached in a temp dir that is removed on unload; the file that gets IMPORTED is the MP4 rendition
# (QMediaPlayer cannot play .gif) and is saved to <program folder>/gif_downloads/ so it outlives the session (the Project keeps referring to it).
# DROP: like fx_tab - the core only knows file URLs, so an event filter on the Timeline and on the Project list accepts our own MIME, downloads
# the file in the background and THEN calls MainWindow.on_files_dropped(paths, t) (timeline: imports + inserts) or import_paths(paths) (Project).
# [PITFALL] Tenor shut its public API down on 2026-06-30 (new keys were refused from 2026-01-13). The tab is kept (code is the v2 API) and
# shows an explanation when the request fails. KLIPY is the Tenor-compatible replacement.
import os
import re
import json
import uuid
import atexit
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt, QObject, Signal, QMimeData, QTimer, QRect, QSettings, QEvent
from PySide6.QtGui import QDrag
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton,
                               QLineEdit, QScrollArea, QButtonGroup, QMenu, QInputDialog, QFrame)

MIME = "application/x-sqvce-gif"
PAGE = 24
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SQVCE-GifBrowser/1.0"
NAMES = {"tenor": "Tenor", "klipy": "KLIPY", "giphy": "GIPHY"}
KEY_HELP = {"tenor": "Tenor stopped issuing keys in January 2026 and shut its API down on June 30, 2026",
            "klipy": "free key at partner.klipy.com",
            "giphy": "free key at developers.giphy.com"}
S = None


# --------------------------------------------------------------------------------------------- network helpers (worker threads only)
def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _json(url):
    return json.loads(_get(url).decode("utf-8", "replace"))


def _i(x):
    try:
        return int(float(x))
    except Exception:
        return 0


def mk(prov, id_, title, prev, dl, ext, w, h):
    return {"prov": prov, "id": str(id_ or uuid.uuid4().hex[:10]), "title": str(title or ""), "prev": prev, "dl": dl,
            "ext": ext, "w": _i(w), "h": _i(h)}


def f_giphy(key, q, cur, cust):
    off = _i(cur)
    p = {"api_key": key, "limit": PAGE, "offset": off, "rating": "pg-13"}
    if q:
        p["q"] = q
    d = _json("https://api.giphy.com/v1/gifs/" + ("search" if q else "trending") + "?" + urllib.parse.urlencode(p))
    items = []
    for g in d.get("data") or []:
        im = g.get("images") or {}
        fw = im.get("fixed_width") or {}
        prev = fw.get("url")
        dl = (im.get("original_mp4") or {}).get("mp4") or (im.get("original") or {}).get("mp4") or fw.get("mp4")
        ext = ".mp4"
        if not dl:
            dl, ext = (im.get("original") or {}).get("url"), ".gif"
        if prev and dl:
            items.append(mk("giphy", g.get("id"), g.get("title"), prev, dl, ext, fw.get("width"), fw.get("height")))
    total = _i((d.get("pagination") or {}).get("total_count")) or 10 ** 9
    nxt = off + len(d.get("data") or [])
    return items, (nxt if items and nxt < min(total, 499) else None)


def f_klipy(key, q, cur, cust):
    page = _i(cur) or 1
    p = {"page": page, "per_page": PAGE, "customer_id": cust}
    if q:
        p["q"] = q
    d = _json(f"https://api.klipy.com/api/v1/{urllib.parse.quote(key, safe='')}/gifs/" + ("search" if q else "trending")
              + "?" + urllib.parse.urlencode(p))
    if d.get("result") is False:
        raise RuntimeError("KLIPY refused the request: " + json.dumps(d)[:200])
    data = d.get("data") or {}
    arr = data.get("data") or []

    def u(f, sz, fmt):
        g = (f.get(sz) or {}).get(fmt)
        if isinstance(g, dict):
            return g.get("url"), g.get("width"), g.get("height")
        return (g, 0, 0) if isinstance(g, str) else (None, 0, 0)
    items = []
    for it in arr:
        f = it.get("file") or {}
        prev = (None, 0, 0)
        for sz in ("sm", "md", "xs", "hd"):
            prev = u(f, sz, "gif")
            if prev[0]:
                break
        dl, ext = None, ""
        for fmt in ("mp4", "webm", "gif"):
            for sz in ("hd", "md", "sm"):
                dl = u(f, sz, fmt)[0]
                if dl:
                    ext = "." + fmt
                    break
            if dl:
                break
        if prev[0] and dl:
            items.append(mk("klipy", it.get("id") or it.get("slug"), it.get("title"), prev[0], dl, ext, prev[1], prev[2]))
    return items, (page + 1 if data.get("has_next") and arr else None)


def f_tenor(key, q, cur, cust):
    p = {"key": key, "client_key": "sqvce", "limit": PAGE, "media_filter": "tinygif,mp4,gif"}
    if cur:
        p["pos"] = cur
    if q:
        p["q"] = q
    d = _json("https://tenor.googleapis.com/v2/" + ("search" if q else "featured") + "?" + urllib.parse.urlencode(p))
    items = []
    for r in d.get("results") or []:
        mf = r.get("media_formats") or {}
        tg, mp = mf.get("tinygif") or {}, mf.get("mp4") or {}
        dims = tg.get("dims") or [0, 0]
        if tg.get("url") and mp.get("url"):
            items.append(mk("tenor", r.get("id"), r.get("content_description"), tg["url"], mp["url"], ".mp4", dims[0], dims[1]))
    return items, (d.get("next") or None)


FETCH = {"tenor": f_tenor, "klipy": f_klipy, "giphy": f_giphy}


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:80]


def fetch_preview(item, folder):
    ext = os.path.splitext(urllib.parse.urlparse(item["prev"]).path)[1].lower() or ".gif"
    path = os.path.join(folder, _safe(item["prov"] + "_" + item["id"]) + ext)
    if not os.path.isfile(path):
        data = _get(item["prev"])
        with open(path, "wb") as f:
            f.write(data)
    return path


def download_item(item, folder):
    path = os.path.join(folder, _safe(item["prov"] + "_" + item["id"]) + item["ext"])
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    data = _get(item["dl"], timeout=60)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


class Bridge(QObject):
    """Runs fn(*a) on a worker thread, then cb(result, error) on the GUI thread."""
    sig = Signal(object)

    def __init__(self):
        super().__init__()
        self.alive, self.pool = True, ThreadPoolExecutor(max_workers=6)
        self.sig.connect(self._run)

    def _run(self, pack):
        cb, res, err = pack
        if self.alive:
            try:
                cb(res, err)
            except RuntimeError:                      # widget already deleted
                pass

    def go(self, fn, cb, *a):
        def job():
            try:
                r, e = fn(*a), None
            except Exception as ex:
                r, e = None, ex
            if self.alive:
                self.sig.emit((cb, r, e))
        if self.alive:
            self.pool.submit(job)

    def close(self):
        self.alive = False
        self.pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------------------------- grid
class Tile(QLabel):
    def __init__(self, st, item, parent):
        super().__init__(parent)
        self.st, self.item, self.mv, self.dead, self._p = st, item, None, False, None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#2c2c30;border-radius:6px;color:#666;")
        self.setText("\u2026")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip((item.get("title") or "") + "\nDouble-click: add to Project  |  drag onto the timeline or Project")

    def set_file(self, path):
        mv = QMovie(path)
        if not mv.isValid():
            self.setText("?")
            return
        mv.setCacheMode(QMovie.CacheMode.CacheAll)
        self.mv = mv
        self.setText("")
        self.setMovie(mv)
        mv.jumpToFrame(0)
        nat = mv.currentImage().size()
        if not self.item["w"] and nat.width() > 0:                 # provider gave no size: learn it from the frame
            self.item["w"], self.item["h"] = nat.width(), nat.height()
            self.st.grid.relayout_soon()
        self.fit()
        mv.start()
        self.st.grid.visible_sync()                                 # pauses it again if it is off-screen

    def fit(self):
        if self.mv is None:
            return
        nat = self.mv.currentImage().size()
        if nat.width() > 0 and nat.height() > 0:
            k = min(self.width() / nat.width(), self.height() / nat.height())
            self.mv.setScaledSize(nat * k)

    def play(self, on):
        if self.mv is None:
            return
        s = self.mv.state()
        if on:
            if s == QMovie.MovieState.NotRunning:
                self.mv.start()
            elif s == QMovie.MovieState.Paused:
                self.mv.setPaused(False)
        elif s == QMovie.MovieState.Running:
            self.mv.setPaused(True)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()

    def mousePressEvent(self, e):
        self._p = e.position().toPoint() if e.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._p is not None and e.buttons() & Qt.MouseButton.LeftButton
                and (e.position().toPoint() - self._p).manhattanLength() >= QApplication.startDragDistance()):
            self._p = None
            md = QMimeData()
            md.setData(MIME, json.dumps(self.item).encode())
            d = QDrag(self)
            d.setMimeData(md)
            d.setPixmap(self.grab().scaledToWidth(120, Qt.TransformationMode.SmoothTransformation))
            d.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.st.add(self.item)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a1 = m.addAction("Add to Project")
        a2 = m.addAction("Add to Timeline at playhead")
        r = m.exec(e.globalPos())
        if r is a1:
            self.st.add(self.item)
        elif r is a2:
            self.st.add(self.item, self.st.api.engine.playhead)


class Grid(QScrollArea):
    """Masonry: N equal-width columns, every tile goes to the currently shortest column."""
    GAP = 6

    def __init__(self, st):
        super().__init__()
        self.st, self.tiles = st, []
        self.inner = QWidget()
        self.inner.setObjectName("gifInner")
        self.setWidget(self.inner)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)       # always on: no relayout loop when it appears
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea{background:#1e1e1e;border:none}QWidget#gifInner{background:#1e1e1e}"
                           "QScrollBar:vertical{background:#111;width:14px;margin:0}"
                           "QScrollBar::handle:vertical{background:#3a3a3a;border-radius:6px;min-height:30px;margin:2px}"
                           "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
                           "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}")
        self.rl = QTimer(self)
        self.rl.setSingleShot(True)
        self.rl.setInterval(20)
        self.rl.timeout.connect(self.relayout)
        self.verticalScrollBar().valueChanged.connect(self.on_scroll)

    def relayout_soon(self):
        self.rl.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.relayout_soon()

    def relayout(self):
        W, g = self.viewport().width(), self.GAP
        cols = max(2, (W - g) // (150 + g))
        cw = max(40, (W - g * (cols + 1)) // cols)
        hs = [g] * cols
        for t in self.tiles:
            w, h = t.item["w"] or 1, t.item["h"] or 1
            th = max(30, int(cw * max(0.4, min(2.5, h / w))))
            c = hs.index(min(hs))
            t.setGeometry(g + c * (cw + g), hs[c], cw, th)
            t.show()
            hs[c] += th + g
        self.inner.resize(W, max(hs))
        self.visible_sync()

    def visible_sync(self):
        vp = QRect(0, self.verticalScrollBar().value(), self.viewport().width(), self.viewport().height())
        for t in self.tiles:
            t.play(t.geometry().intersects(vp))

    def on_scroll(self, v):
        self.visible_sync()
        if v >= self.verticalScrollBar().maximum() - 400:
            self.st.load_more()

    def clear(self):
        for t in self.tiles:
            t.dead = True
            if t.mv is not None:
                t.mv.stop()
            t.setMovie(None) if t.mv is not None else None
            t.deleteLater()
        self.tiles = []
        self.verticalScrollBar().setValue(0)
        self.inner.resize(self.viewport().width(), 1)

    def add(self, items):
        folder = self.st.prev_folder()
        for it in items:
            t = Tile(self.st, it, self.inner)
            self.tiles.append(t)
            self.st.bridge.go(fetch_preview, lambda r, e, t=t: (None if (t.dead or e or not r) else t.set_file(r)), it, folder)
        self.relayout()


# --------------------------------------------------------------------------------------------- state
class State:
    def __init__(self, api):
        self.api, self.alive = api, True
        self.bridge = Bridge()
        self.set = QSettings("QuickCut", "QuickCut")
        self.gen, self.loading, self.more, self.cur, self.q, self.started = 0, False, True, None, "", False
        p = self.set.value("gif_provider", "", str)
        self.prov = p if p in FETCH else "klipy"
        self.cust = self.set.value("gif_customer", "", str)
        if not self.cust:
            self.cust = uuid.uuid4().hex
            self.set.setValue("gif_customer", self.cust)
        self._prev = None
        base = os.path.dirname(os.path.abspath(api.mods_dir))
        self.dl_dir = os.path.join(base, "gif_downloads")
        try:
            os.makedirs(self.dl_dir, exist_ok=True)
        except OSError:
            self.dl_dir = os.path.join(tempfile.gettempdir(), "quickcut_gif_downloads")
            os.makedirs(self.dl_dir, exist_ok=True)
        atexit.register(self.cleanup_prev)

    def prev_folder(self):
        if self._prev is None:
            self._prev = tempfile.mkdtemp(prefix="quickcut_gifprev_")
        return self._prev

    def cleanup_prev(self):
        if self._prev:
            shutil.rmtree(self._prev, ignore_errors=True)
            self._prev = None

    def key(self, prov):
        return self.set.value("gif_key_" + prov, "", str).strip()

    # --- UI
    def build(self):
        w = Page(self)
        w.setMinimumWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(8, 8, 8, 6)
        top.setSpacing(10)
        top.addStretch(1)
        grp = QButtonGroup(w)
        grp.setExclusive(True)
        self.btns = {}
        for k in ("tenor", "klipy", "giphy"):
            b = QPushButton(NAMES[k])
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setStyleSheet("QPushButton{background:#3a3a40;border:1px solid #2e2e33;border-radius:8px;padding:8px 16px;"
                            "font-weight:700;font-size:10pt;color:#ffffff}QPushButton:hover{background:#46464d}"
                            "QPushButton:checked{background:#2d8ceb;border-color:#2d8ceb}")
            b.clicked.connect(lambda _=False, k=k: self.set_provider(k))
            grp.addButton(b)
            top.addWidget(b)
            self.btns[k] = b
        top.addStretch(1)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setContentsMargins(8, 0, 8, 6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search GIFs (empty = trending)")
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self.reload)
        self.edit.textChanged.connect(lambda t: self.reload() if (not t.strip() and self.q) else None)
        row.addWidget(self.edit, 1)
        kb = QToolButton()
        kb.setText("\U0001F511")
        kb.setToolTip("Set the API key for the selected provider")
        kb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        kb.clicked.connect(self.ask_key)
        row.addWidget(kb)
        lay.addLayout(row)
        self.msg_lbl = QLabel("")
        self.msg_lbl.setWordWrap(True)
        self.msg_lbl.setStyleSheet("color:#f4c430;padding:2px 10px 6px 10px;")
        self.msg_lbl.hide()
        lay.addWidget(self.msg_lbl)
        self.grid = Grid(self)
        lay.addWidget(self.grid, 1)
        self.foot = QLabel("")
        self.foot.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.foot.setStyleSheet("color:#7a7a7a;font-size:8pt;padding:2px 8px;")
        lay.addWidget(self.foot)
        self.btns[self.prov].setChecked(True)
        self.foot.setText("Powered by " + NAMES[self.prov])
        return w

    def msg(self, text):
        self.msg_lbl.setText(text)
        self.msg_lbl.setVisible(bool(text))

    def shown(self):
        if not self.started:
            self.started = True
            QTimer.singleShot(0, self.reload)

    def set_provider(self, k):
        if k == self.prov:
            return
        self.prov = k
        self.set.setValue("gif_provider", k)
        self.foot.setText("Powered by " + NAMES[k])
        self.reload()

    def ask_key(self):
        p = self.prov
        txt, ok = QInputDialog.getText(self.api.win, f"{NAMES[p]} API key", f"{NAMES[p]} API key ({KEY_HELP[p]}):",
                                       QLineEdit.EchoMode.Normal, self.key(p))
        if ok:
            self.set.setValue("gif_key_" + p, txt.strip())
            self.reload()

    # --- loading
    def reload(self):
        self.gen += 1
        self.grid.clear()
        self.cur, self.more, self.loading = None, True, False
        self.q = self.edit.text().strip()
        self.msg("")
        self.load_more()

    def load_more(self):
        if self.loading or not self.more or not self.alive:
            return
        prov = self.prov
        key = self.key(prov)
        if not key:
            self.more = False
            self.msg(f"{NAMES[prov]} needs an API key - click the key button above ({KEY_HELP[prov]}).")
            return
        self.loading, gen = True, self.gen
        self.bridge.go(FETCH[prov], lambda r, e: self.on_page(gen, prov, r, e), key, self.q, self.cur, self.cust)

    def on_page(self, gen, prov, r, e):
        if gen != self.gen:
            return
        self.loading = False
        if e is not None:
            self.more = False
            self.msg(self.explain(prov, e))
            return
        items, nxt = r
        self.cur, self.more = nxt, bool(items) and nxt is not None
        if not items and not self.grid.tiles:
            self.msg("No results.")
        self.grid.add(items)
        QTimer.singleShot(200, self.fill_check)

    def fill_check(self):
        bar = self.grid.verticalScrollBar()
        if self.alive and self.more and bar.maximum() - bar.value() < 400:
            self.load_more()

    @staticmethod
    def explain(prov, e):
        code = e.code if isinstance(e, urllib.error.HTTPError) else None
        if prov == "tenor":
            return ("Tenor shut down its public API on June 30, 2026, so this tab can no longer load results "
                    "(use KLIPY - it is Tenor's compatible replacement).  [" + (str(code) if code else str(e)) + "]")
        if code in (401, 403):
            return f"{NAMES[prov]} rejected the API key (HTTP {code}). Check it with the key button."
        if code == 429:
            return f"{NAMES[prov]} rate limit reached - wait a bit and try again."
        return f"{NAMES[prov]} request failed: {e}"

    # --- adding to the project / timeline
    def add(self, item, t=None):
        self.api.status("Downloading GIF ...", 8000)
        self.bridge.go(download_item, lambda p, e: self.on_dl(p, e, t), item, self.dl_dir)

    def on_dl(self, path, e, t):
        if e is not None or not path:
            self.api.status(f"GIF download failed: {e}", 8000)
            return
        w = self.api.win
        if t is None:
            w.import_paths([path])
        else:
            w.on_files_dropped([path], t)
        self.api.status("GIF added: " + os.path.basename(path), 5000)


class Page(QWidget):
    def __init__(self, st):
        super().__init__()
        self.st = st

    def showEvent(self, e):
        super().showEvent(e)
        self.st.shown()


class DropFilter(QObject):
    """Accepts our MIME on the Timeline (drop at the time under the cursor) and on the Project list (import only)."""
    def __init__(self, st):
        super().__init__()
        self.st = st

    def eventFilter(self, o, ev):
        t, tl = ev.type(), self.st.api.tl
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()
            if o is tl:
                tl.drop_x = ev.position().x()
                tl.update()
            return True
        if t == QEvent.Type.DragLeave and o is tl:
            tl.drop_x = None
            tl.update()
            return False
        if t == QEvent.Type.Drop and ev.mimeData().hasFormat(MIME):
            try:
                item = json.loads(bytes(ev.mimeData().data(MIME)).decode())
            except Exception:
                return False
            ev.acceptProposedAction()
            if o is tl:
                tl.drop_x = None
                tl.update()
                self.st.add(item, max(0.0, tl.xt(ev.position().x())))
            else:
                self.st.add(item)
            return True
        return False


# --------------------------------------------------------------------------------------------- mod hooks
def build_tab(api):
    return S.build()


def on_load(api):
    global S
    st = S = State(api)
    st.drop = DropFilter(st)
    st.targets = [api.tl, api.win.plist, api.win.plist.viewport()]
    for w in st.targets:
        w.installEventFilter(st.drop)


def on_unload(api):
    global S
    st = S
    if st is None:
        return
    st.alive = False
    for w in st.targets:
        try:
            w.removeEventFilter(st.drop)
        except RuntimeError:
            pass
    st.bridge.close()
    try:
        st.grid.clear()
    except RuntimeError:
        pass
    st.cleanup_prev()
    S = None
