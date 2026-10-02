# Snake - a "mode" mod for SQVCE: adds a "Snake" tab next to Video / GIF that swaps the window for the classic game.
# Install: put this file in the program's `mods` folder, tick it in Preferences > Plug-ins, press Apply / OK.
# Controls: arrow keys (or WASD) steer, Space / P pause, Enter restarts after Game Over. Walls and your own tail kill you.

MOD_NAME = "Snake"
MOD_TYPE = "mode"
MOD_DESC = "Classic Snake. Arrow keys (or WASD) to steer, Space to pause, Enter to restart."
MOD_AUTHOR = "SQVCE"

import random
from collections import deque

from PySide6.QtCore import Qt, QTimer, QEvent, QRectF, QSettings
from PySide6.QtGui import QPainter, QColor, QFont, QPen
from PySide6.QtWidgets import QWidget

COLS, ROWS = 20, 15                # classic board: 300 tiles
TICK_MS = 110                      # classic: one constant speed, it never changes
BOARD_SCALE = 0.6                  # board is drawn 40 % smaller than "fill the window"
_game = None


class SnakeLogic:
    """The rules, with no Qt in it. head = snake[0]. state: ready / running / paused / over / won."""

    def __init__(self, cols=COLS, rows=ROWS, rng=None):
        self.cols, self.rows = cols, rows
        self.rng = rng or random.Random()
        self.best = 0
        self.reset()

    def reset(self):
        cx, cy = self.cols // 2, self.rows // 2
        self.snake = deque([(cx, cy), (cx - 1, cy), (cx - 2, cy)])
        self.dir = (1, 0)
        self.queue = []                    # buffered turns (max 2) so quick U-turn taps can't reverse into yourself
        self.score = 0
        self.state = "ready"
        self._place_food()

    def _place_food(self):
        taken = set(self.snake)
        free = [(x, y) for x in range(self.cols) for y in range(self.rows) if (x, y) not in taken]
        self.food = self.rng.choice(free) if free else None

    def interval(self):
        """Milliseconds per step: constant (classic Snake does not speed up)."""
        return TICK_MS

    def turn(self, d):
        last = self.queue[-1] if self.queue else self.dir
        if d == last or (d[0] + last[0], d[1] + last[1]) == (0, 0):
            return
        if len(self.queue) < 2:
            self.queue.append(d)

    def step(self):
        """Advance one cell. Returns "ok", "ate", "dead" or "won"."""
        if self.queue:
            self.dir = self.queue.pop(0)
        hx, hy = self.snake[0]
        nx, ny = (hx + self.dir[0]) % self.cols, (hy + self.dir[1]) % self.rows      # no walls: leave one edge, enter the opposite one
        eat = (nx, ny) == self.food
        body = list(self.snake) if eat else list(self.snake)[:-1]      # the tail cell is free again unless we grow
        if (nx, ny) in body:
            self.state = "over"
            self.best = max(self.best, self.score)
            return "dead"
        self.snake.appendleft((nx, ny))
        if not eat:
            self.snake.pop()
            return "ok"
        self.score += 1
        self._place_food()
        if self.food is None:
            self.state = "won"
            self.best = max(self.best, self.score)
            return "won"
        return "ate"


class SnakeGame(QWidget):
    DIRS = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_D: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_W: (0, -1), Qt.Key.Key_Down: (0, 1), Qt.Key.Key_S: (0, 1)}
    PAUSE = (Qt.Key.Key_Space, Qt.Key.Key_P)
    ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(360, 260)
        self.g = SnakeLogic()
        try:
            self.g.best = int(QSettings("QuickCut", "QuickCut").value("snake_best", 0, int))
        except Exception:
            pass
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # --- game flow
    def _start(self):
        self.g.state = "running"
        self.timer.start(TICK_MS)
        self.update()

    def _pause(self, on):
        if on and self.g.state == "running":
            self.g.state = "paused"
            self.timer.stop()
        elif not on and self.g.state == "paused":
            self._start()
        self.update()

    def _tick(self):
        res = self.g.step()
        if res in ("dead", "won"):
            self.timer.stop()
            try:
                QSettings("QuickCut", "QuickCut").setValue("snake_best", self.g.best)
            except Exception:
                pass
        self.update()

    def stop(self):
        self.timer.stop()

    # --- input. The main window owns the arrow keys / Space / Enter as shortcuts (frame step, play, tool OK...); accepting
    # ShortcutOverride for the keys the game uses makes Qt deliver them here instead.
    def _handled(self, k):
        return k in self.DIRS or k in self.PAUSE or k in self.ENTER

    def event(self, e):
        if e.type() == QEvent.Type.ShortcutOverride and self._handled(e.key()):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        k = e.key()
        if not self._handled(k):
            super().keyPressEvent(e)
            return
        e.accept()
        st = self.g.state
        if k in self.DIRS:
            if st == "ready":
                self._start()
            if self.g.state == "running":
                self.g.turn(self.DIRS[k])
        elif k in self.PAUSE and not e.isAutoRepeat():
            if st == "ready":
                self._start()
            elif st == "running":
                self._pause(True)
            elif st == "paused":
                self._pause(False)
        elif k in self.ENTER and st in ("over", "won", "ready", "paused"):
            if st in ("over", "won"):
                self.g.reset()
            self._start()

    def keyReleaseEvent(self, e):
        if self._handled(e.key()):
            e.accept()
        else:
            super().keyReleaseEvent(e)

    def focusOutEvent(self, e):
        self._pause(True)
        super().focusOutEvent(e)

    def hideEvent(self, e):
        self._pause(True)
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.setFocus)

    def mousePressEvent(self, e):
        self.setFocus()

    # --- drawing (classic look: every snake segment and the food fill one whole square tile)
    def paintEvent(self, ev):
        g = self.g
        p = QPainter(self)
        W, H, hud = self.width(), self.height(), 34
        p.fillRect(0, 0, W, H, QColor("#181818"))
        fit = min((W - 24) // COLS, (H - hud - 16) // ROWS)
        cell = max(4, int(fit * BOARD_SCALE))
        bw, bh = cell * COLS, cell * ROWS
        ox, oy = (W - bw) // 2, hud + (H - hud - bh) // 2
        for x in range(COLS):
            for y in range(ROWS):
                p.fillRect(ox + x * cell, oy + y * cell, cell, cell, QColor("#212121" if (x + y) % 2 else "#1d1d1d"))
        p.setPen(QPen(QColor("#3a3a3a"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(ox - 1, oy - 1, bw + 1, bh + 1)
        p.setPen(Qt.PenStyle.NoPen)
        if g.food is not None:
            fx, fy = g.food
            p.fillRect(ox + fx * cell, oy + fy * cell, cell, cell, QColor("#e0413a"))
        for i, (x, y) in enumerate(g.snake):
            p.fillRect(ox + x * cell, oy + y * cell, cell, cell, QColor("#7be08a" if i == 0 else "#3fae52"))
        p.setPen(QColor("#d4d4d4"))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        hud_rect = QRectF(12, 0, W - 24, hud)
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"Score {g.score}     Best {g.best}")
        f.setBold(False)
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QColor("#8f8f8f"))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   "Arrows / WASD move   Space pause   Enter restart")
        msg = {"ready": ("SNAKE", "Press an arrow key to start"), "paused": ("PAUSED", "Space to resume"),
               "over": ("GAME OVER", f"Score {g.score}   -   Enter to play again"),
               "won": ("YOU WIN!", "The board is full   -   Enter to play again")}.get(g.state)
        if msg:
            p.fillRect(ox, oy, bw, bh, QColor(0, 0, 0, 150))
            p.setPen(QColor("#ffffff"))
            f.setPointSize(22)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(ox, oy, bw, bh * 0.5 + 10), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, msg[0])
            f.setPointSize(10)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor("#cfd8e3"))
            p.drawText(QRectF(ox, oy + bh * 0.5 + 18, bw, bh * 0.4), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, msg[1])
        p.end()


def build_mode(api):
    global _game
    _game = SnakeGame()
    return _game


def on_mode_shown(api):
    if _game is not None:
        _game.setFocus()
        _game.update()


def on_unload(api):
    global _game
    if _game is not None:
        _game.stop()
    _game = None
