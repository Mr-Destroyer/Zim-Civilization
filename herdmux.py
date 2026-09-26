#!/usr/bin/env python3
"""ZIM-CIVILIZATION — herdmux, the multiplexer.

tmux isn't installable on this box (no sudo, no package manager access), so
this is a from-scratch terminal multiplexer covering the tmux bindings the
herd needs. Ctrl-B is the prefix.

    prefix c    new window
    prefix n    next window          prefix p    previous window
    prefix h    split pane horizontal (top / bottom)
    prefix v    split pane vertical   (side by side)
    prefix o    cycle pane focus     arrows      move focus
    prefix t    change the focused pane's content
    prefix x    kill pane            prefix z    zoom pane
    prefix r    force model rotation on the focused agent
    prefix ?    help                 prefix d    detach / quit
    prefix 0-9  select window by number
    Enter       focus the prompt line   Esc cancel   Tab broadcast toggle

Panes render the harness's own telemetry (agent streams, log, pool, dashboard)
rather than hosting foreign full-screen programs, so no VT emulation is needed.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
import unicodedata

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# --------------------------------------------------------------------------- colour

RESET = "\x1b[0m"
DIM = "\x1b[90m"


def fg256(n: int) -> str:
    return f"\x1b[38;5;{n}m"


C = {
    "none": "", "dim": DIM,
    "green": fg256(84), "red": fg256(203), "yellow": fg256(215),
    "cyan": fg256(81), "magenta": fg256(177), "white": fg256(253),
    "blue": fg256(75), "grey": fg256(242),
}
KIND_COLOR = {
    "limit": "red", "err": "red", "rot": "yellow", "warn": "yellow",
    "acct": "cyan", "ok": "green", "prompt": "magenta", "run": "grey",
    "info": "white", "send": "blue", "reply": "white",
}
CONT = "\x00"   # right half of a double-width glyph


# --------------------------------------------------------------------------- text

def strip_ansi(s: str) -> str:
    return ANSI.sub("", s or "")


def cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip(s: str, width: int) -> str:
    """Truncate to `width` display cells."""
    s = strip_ansi(s).replace("\t", "    ").replace("\r", "")
    out, w = [], 0
    for ch in s:
        c = cw(ch)
        if w + c > width:
            break
        out.append(ch)
        w += c
    return "".join(out)


# --------------------------------------------------------------------------- tree

class Pane:
    def __init__(self, content: str = "dashboard"):
        self.content = content
        self.scroll = 0

    def title(self) -> str:
        return {"dashboard": "herd", "log": "events", "pool": "model pool"}.get(
            self.content, self.content)


class Split:
    def __init__(self, orient: str, a, b):
        self.orient = orient      # 'row' = stacked, 'col' = side by side
        self.a, self.b = a, b


class Window:
    def __init__(self, idx: int, root, focus: Pane):
        self.idx = idx
        self.root = root
        self.focus = focus
        self.zoom = False


def leaves(node):
    if isinstance(node, Pane):
        yield node
    else:
        yield from leaves(node.a)
        yield from leaves(node.b)


def layout(node, x, y, w, h, out):
    if isinstance(node, Pane):
        out.append((node, x, y, w, h))
        return
    if node.orient == "row":
        top = max(3, int(h * 0.5))
        if h - top < 3:
            top = h - 3
        layout(node.a, x, y, w, top, out)
        layout(node.b, x, y + top, w, h - top, out)
    else:
        left = max(6, int(w * 0.5))
        if w - left < 6:
            left = w - 6
        layout(node.a, x, y, left, h, out)
        layout(node.b, x + left, y, w - left, h, out)


def replace_focus(node, target, make):
    """Rewrite the tree, swapping `target` for make(target). Focus returned."""
    if node is target:
        return make(node)
    if isinstance(node, Split):
        node.a = replace_focus(node.a, target, make)
        node.b = replace_focus(node.b, target, make)
    return node


def remove_pane(node, target):
    """Drop `target`; its sibling takes the parent's place. None if last."""
    if isinstance(node, Pane):
        return None if node is target else node
    if node.a is target:
        return node.b
    if node.b is target:
        return node.a
    node.a = remove_pane(node.a, target)
    node.b = remove_pane(node.b, target)
    return node


# --------------------------------------------------------------------------- screen

class Screen:
    def __init__(self, w: int, h: int):
        self.resize(w, h)

    def resize(self, w: int, h: int):
        self.w, self.h = max(w, 20), max(h, 6)
        self.cells = [[(" ", None) for _ in range(self.w)] for _ in range(self.h)]

    def put(self, x: int, y: int, s: str, color: str | None = None, maxw: int | None = None):
        if y < 0 or y >= self.h:
            return
        s = strip_ansi(s).replace("\t", "    ")
        limit = self.w if maxw is None else min(self.w, x + maxw)
        cx = x
        for ch in s:
            if cx >= limit:
                break
            if ch == "\n":
                break
            if ch == "\r":
                continue
            c = cw(ch)
            if c == 0:
                continue
            if cx < 0:
                cx += c
                continue
            self.cells[y][cx] = (ch, color)
            if c == 2:
                if cx + 1 < self.w:
                    self.cells[y][cx + 1] = (CONT, color)
            cx += c

    def fill(self, x: int, y: int, w: int, h: int, ch: str = " ", color=None):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= yy < self.h and 0 <= xx < self.w:
                    self.cells[yy][xx] = (ch, color)

    def box(self, x, y, w, h, title="", focused=False, right=""):
        if w < 4 or h < 3:
            return
        col = "green" if focused else "grey"
        self.fill(x, y, w, h, " ", None)
        self.put(x, y, "┌" + "─" * (w - 2) + "┐", col)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, "│", col)
            self.put(x + w - 1, yy, "│", col)
        self.put(x, y + h - 1, "└" + "─" * (w - 2) + "┘", col)
        label = f" {clip(title, w - 4)} "
        self.put(x + 2, y, label, "green" if focused else "grey", maxw=w - 4)
        if right:
            r = clip(right, w - 4)
            self.put(x + w - 2 - len(r), y, r, "yellow", maxw=w - 4)


# --------------------------------------------------------------------------- tui

CONTENTS = ["dashboard", "agent-stream", "log", "pool"]


class TUI:
    def __init__(self, engine):
        self.eng = engine
        self.fd = sys.stdin.fileno()
        self.old = None
        self.running = True
        self.windows: list[Window] = []
        self.wcur = 0
        self.input_mode = False
        self.buf = ""
        self.broadcast = True
        self.prefix = False
        self.flash = ""
        self.flash_t = 0.0
        self.help = False
        self.view = 0
        self._new_window(first=True)

    # -- windows
    def _agent_pane(self, i):
        p = Pane("dashboard")
        p.agent = self.eng.workers[i % len(self.eng.workers)]
        p.content = "agent"
        return p

    def _new_window(self, first=False):
        p = Pane("dashboard")
        self.windows.append(Window(len(self.windows), p, p))
        if not first:
            self.wcur = len(self.windows) - 1

    @property
    def win(self) -> Window:
        return self.windows[self.wcur]

    def notice(self, msg):
        self.flash = msg
        self.flash_t = time.time()

    # -- input
    def _setup(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()

    def _teardown(self):
        sys.stdout.write("\x1b[?25h\x1b[?1049l" + RESET)
        sys.stdout.flush()
        if self.old:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def _read(self, timeout=0.1):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, 1024)
        except OSError:
            return b""

    # -- key handling
    def key(self, b: bytes):
        if b == b"\x03":                      # Ctrl-C
            self.running = False
            return
        if b == b"\x1b" and not self.prefix:
            self.input_mode = False
            self.buf = ""
            return

        if self.help:
            self.help = False
            return

        if self.prefix:
            self.prefix = False
            self.do_prefix(b)
            return

        if self.input_mode:
            self.do_input(b)
            return

        if b == b"\x02":                      # Ctrl-B
            self.prefix = True
            return
        if b in (b"\x0d", b"\x0a"):
            self.input_mode = True
            return
        if b == b"\x1b[A":
            self.move_focus("up")
        elif b == b"\x1b[B":
            self.move_focus("down")
        elif b == b"\x1b[C":
            self.move_focus("right")
        elif b == b"\x1b[D":
            self.move_focus("left")
        elif b == b"\x1b[5~":
            self.win.focus.scroll += 5
        elif b == b"\x1b[6~":
            self.win.focus.scroll = max(0, self.win.focus.scroll - 5)

    def do_prefix(self, b: bytes):
        ch = b.decode("utf-8", "ignore")
        if ch == "c":
            self._new_window()
            self.notice("new window")
        elif ch == "n":
            self.wcur = (self.wcur + 1) % len(self.windows)
            self.notice(f"window {self.wcur}")
        elif ch == "p":
            self.wcur = (self.wcur - 1) % len(self.windows)
            self.notice(f"window {self.wcur}")
        elif ch == "h":
            self.split("row")
        elif ch == "v":
            self.split("col")
        elif ch == "o":
            self.cycle_focus()
        elif ch == "t":
            self.cycle_content()
        elif ch == "x":
            self.kill_pane()
        elif ch == "z":
            self.win.zoom = not self.win.zoom
            self.notice("zoom " + ("on" if self.win.zoom else "off"))
        elif ch == "r":
            self.force_rotate()
        elif ch == "?":
            self.help = True
        elif ch == "d":
            self.running = False
        elif ch.isdigit():
            i = int(ch)
            if i < len(self.windows):
                self.wcur = i
        else:
            self.notice(f"unbound: {ch}")

    def do_input(self, b: bytes):
        if b in (b"\x0d", b"\x0a"):
            text = self.buf.strip()
            self.input_mode = False
            self.buf = ""
            if text:
                self.send(text)
            return
        if b == b"\x7f" or b == b"\x08":
            self.buf = self.buf[:-1]
            return
        if b == b"\t":
            self.broadcast = not self.broadcast
            return
        try:
            s = b.decode("utf-8")
        except UnicodeDecodeError:
            return
        self.buf += "".join(c for c in s if c.isprintable())

    # -- actions
    def split(self, orient: str):
        p = self.win.focus
        new = Pane(p.content if not p.content == "dashboard" else "dashboard")
        if getattr(p, "agent", None) is not None:
            new.agent = p.agent
        parent = Split(orient, p, new)
        self.win.root = replace_focus(self.win.root, p, lambda _: parent)
        self.win.focus = new
        self.notice(f"split {orient}")

    def cycle_focus(self):
        ls = list(leaves(self.win.root))
        if not ls:
            return
        i = ls.index(self.win.focus) if self.win.focus in ls else 0
        self.win.focus = ls[(i + 1) % len(ls)]

    def cycle_content(self):
        p = self.win.focus
        if getattr(p, "agent", None) is not None:
            p.content = "dashboard"
            del p.agent
            return
        if p.content == "dashboard":
            # next: attach to first agent not already shown in this window
            shown = {id(getattr(q, "agent", None)) for q in leaves(self.win.root)}
            for w in self.eng.workers:
                if id(w) not in shown:
                    p.content, p.agent = "agent", w
                    return
            p.content = "log"
        elif p.content == "agent":
            p.content = "log"
        elif p.content == "log":
            p.content = "pool"
        else:
            p.content = "dashboard"

    def kill_pane(self):
        ls = list(leaves(self.win.root))
        if len(ls) == 1:
            self.notice("last pane — not killed")
            return
        victim = self.win.focus
        self.win.root = remove_pane(self.win.root, victim)
        self.win.focus = list(leaves(self.win.root))[0]
        self.notice("pane killed")

    def force_rotate(self):
        a = getattr(self.win.focus, "agent", None)
        if not a:
            self.notice("focused pane is not an agent")
            return
        a.rotate("manual")
        self.notice(f"{a.name} rotated")

    def send(self, text: str):
        if self.broadcast:
            self.eng.broadcast(text)
            self.notice(f"broadcast: {text[:40]}")
            return
        a = getattr(self.win.focus, "agent", None)
        if not a:
            self.notice("no agent in focus — Tab for broadcast")
            return
        a.enqueue(text)
        self.eng.log("prompt", f"-> {a.name}: {text[:100]}")
        self.notice(f"-> {a.name}: {text[:40]}")

    def move_focus(self, d):
        w, h = getattr(self, "_size", (80, 24))
        rects = {}
        out = []
        layout(self.win.root, 0, 1, w, max(6, h - 4), out)
        for p, x, y, w, h in out:
            rects[p] = (x, y, w, h)
        if self.win.focus not in rects:
            return
        fx, fy, fw, fh = rects[self.win.focus]
        fcx, fcy = fx + fw / 2, fy + fh / 2
        best, bd = None, None
        for p, (x, y, w, h) in rects.items():
            if p is self.win.focus:
                continue
            cx, cy = x + w / 2, y + h / 2
            dx, dy = cx - fcx, cy - fcy
            ok = {"up": dy < 0, "down": dy > 0, "left": dx < 0, "right": dx > 0}[d]
            if not ok:
                continue
            if d in ("up", "down"):
                if abs(dx) > fw / 2 + w / 2 - 1:
                    continue
                dist = (abs(dy), abs(dx))
            else:
                if abs(dy) > fh / 2 + h / 2 - 1:
                    continue
                dist = (abs(dx), abs(dy))
            if bd is None or dist < bd:
                best, bd = p, dist
        if best:
            self.win.focus = best

    # -- content
    def pane_lines(self, p: Pane, w: int, h: int) -> list[tuple[str, str | None]]:
        if p.content == "log":
            return [(f" {ts} {msg[:max(0, w-11)]}", KIND_COLOR.get(k, "none"))
                    for k, ts, msg in reversed(self.eng.log_lines[-400:])]
        if p.content == "pool":
            out = []
            for m in self.eng.cfg["pool"]:
                on = [a.name for a in self.eng.workers if a.model == m]
                mark = "◉" if on else "○"
                col = "green" if on else "grey"
                cap = self.eng.cfg["caps"].get(m) or 0
                out.append((f" {mark} {clip(m, max(8, w-26)):<{max(8,w-26)}} "
                            f"{('← ' + ','.join(on)) if on else ''}", col))
            return out
        # dashboard
        out = []
        for a in self.eng.workers:
            q = a.quota()
            bw = max(8, min(w - 30, 18))
            f = int(q * bw)
            bar, col = ("█" * f + "░" * (bw - f)), (
                "red" if q >= 0.98 else "yellow" if q >= 0.8 else "green")
            out.append((f" {a.name:<9}", "white"))
            out.append((f"   {bar}", col))
            out.append((f"   {clip(a.model, max(6, w-8))}", "cyan"))
            out.append((f"   {a.status:<10} {a.tok_session:>9d} tok  "
                        f"{a.switches}sw {a.limits}lim", "grey"))
            out.append(("", None))
        return out

    # -- draw
    def draw(self):
        try:
            sz = shutil.get_terminal_size()
        except Exception:
            sz = os.terminal_size((80, 24))
        w, h = sz.columns, sz.lines
        self._size = (w, h)
        s = Screen(w, h)
        e = self.eng

        # header
        s.fill(0, 0, w, 1, " ", None)
        title = " ZIM-CIVILIZATION "
        s.put(0, 0, title, "green")
        stats = (f"tokens {e.tokens}  cost ${e.cost:.4f}  agents "
                 f"{sum(1 for a in e.workers if a.status == 'running')}/{len(e.workers)}  "
                 f"switches {e.switches}  limits {e.limits}  acct-rot {e.acct_rot}  "
                 f"up {e.uptime()} ")
        s.put(max(0, w - len(stats) - 1), 0, stats, "grey")

        # panes
        area_y, area_h = 1, h - 4
        out = []
        if self.win.zoom:
            layout(self.win.focus, 0, area_y, w, area_h, out)
        else:
            layout(self.win.root, 0, area_y, w, area_h, out)

        for p, x, y, pw, ph in out:
            right = ""
            if p.content == "agent" and getattr(p, "agent", None):
                a = p.agent
                right = f"{a.status} {a.tok_session}t"
            s.box(x, y, pw, ph, p.title(), focused=(p is self.win.focus), right=right)
            inner_w, inner_h = pw - 2, ph - 2
            lines = self.content_for(p, inner_w)
            maxscroll = max(0, len(lines) - inner_h)
            p.scroll = min(p.scroll, maxscroll)
            start = max(0, len(lines) - inner_h - p.scroll)
            view = lines[start:start + inner_h]
            for i, (text, col) in enumerate(view):
                s.put(x + 1, y + 1 + i, " " + clip(text, inner_w - 1), col, maxw=inner_w)

        # window tabs
        ty = h - 3
        s.fill(0, ty, w, 1, " ", None)
        x = 0
        for wi, win in enumerate(self.windows):
            n = len(list(leaves(win.root)))
            label = f" {wi}:{n}p "
            col = "green" if wi == self.wcur else "grey"
            s.put(x, ty, label, col)
            x += len(label)
        s.put(x + 1, ty, "· prefix c new · n next · h/v split · t content · ? help",
              "grey", maxw=max(0, w - x - 2))

        # prompt line
        iy = h - 2
        s.fill(0, iy, w, 1, " ", None)
        tgt = "ALL" if self.broadcast else (
            getattr(self.win.focus, "agent", None).name
            if getattr(self.win.focus, "agent", None) else "—")
        s.put(0, iy, "›", "green")
        s.put(2, iy, f"[{tgt}]", "yellow")
        s.put(9, iy, self.buf, "white", maxw=max(0, w - 12))
        if self.input_mode:
            s.put(9 + min(len(self.buf), w - 12), iy, "█", "green")

        # status
        sy = h - 1
        s.fill(0, sy, w, 1, " ", None)
        if self.prefix:
            s.put(0, sy, " PREFIX ", "yellow")
        else:
            s.put(0, sy, " ctrl-b ", "grey")
        msg = self.flash if (time.time() - self.flash_t) < 4 else ""
        s.put(9, sy, msg, "cyan", maxw=max(0, w - 20))
        hint = "enter prompt · tab bcast · esc close"
        s.put(max(0, w - len(hint) - 1), sy, hint, "grey")

        # help overlay
        if self.help:
            ow, oh = min(62, w - 4), 18
            ox, oy = (w - ow) // 2, max(1, (h - oh) // 2)
            s.box(ox, oy, ow, oh, "herdmux — prefix is ctrl-b", focused=True)
            rows = [
                "c          new window", "n / p      next / previous window",
                "0-9        select window", "h          split horizontal (top/bottom)",
                "v          split vertical (side by side)", "o          cycle pane focus",
                "arrows     move focus", "t          change pane content",
                "x          kill pane", "z          zoom pane",
                "r          force model rotation", "enter      prompt the herd",
                "tab        toggle broadcast", "d          detach / quit",
                "esc        close this help",
            ]
            for i, r in enumerate(rows):
                s.put(ox + 2, oy + 1 + i, r, "white", maxw=ow - 4)

        self.render(s)

    def content_for(self, p: Pane, w: int):
        """Full, unwrapped logical lines for a pane (scroll applied by caller)."""
        if p.content == "agent" and getattr(p, "agent", None):
            a = p.agent
            q = a.quota()
            bw = max(10, min(w - 12, 30))
            filled = int(q * bw)
            bar = "█" * filled + "░" * (bw - filled)
            bcol = "red" if q >= 0.98 else "yellow" if q >= 0.8 else "green"
            out = [
                (f"model    {a.model}", "cyan"),
                (f"account  {a.profile}", "yellow"),
                (f"state    {a.status}", "green" if a.status == "running" else "grey"),
                (f"quota    {bar} {int(q*100)}%", bcol),
                (f"tokens   {a.tok_model} on this model / {a.tok_session} session", "white"),
                (f"switches {a.switches}   limits {a.limits}   errors {a.errors}", "grey"),
                ("", None),
                (f"task     {a.task}", "grey"),
                ("", None),
                ("── stream " + "─" * max(0, w - 10), "grey"),
            ]
            for k, ts, msg in reversed((a.lines or [])[-200:]):
                out.append((f"{ts} {msg}", KIND_COLOR.get(k, "none")))
            if not a.lines:
                out.append((" (no activity yet — send a prompt)", "grey"))
            return out
        return self.pane_lines(p, w, 10_000)

    def render(self, s: Screen):
        buf = ["\x1b[H"]
        last = None
        for y in range(s.h):
            row = s.cells[y]
            parts = []
            for x in range(s.w):
                ch, col = row[x]
                if ch == CONT:
                    continue
                if col != last:
                    parts.append(C.get(col or "none", "") or RESET)
                    last = col
                parts.append(ch)
            parts.append(RESET)
            buf.append("".join(parts))
            buf.append("\x1b[K")
            if y < s.h - 1:
                buf.append("\r\n")
            last = None
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    # -- loop
    def loop(self):
        def on_resize(sig, frm):
            pass
        try:
            signal.signal(signal.SIGWINCH, on_resize)
        except Exception:
            pass
        self._setup()
        try:
            while self.running and not self.eng.stop:
                b = self._read(0.1)
                i = 0
                while i < len(b):
                    # split escape sequences out
                    if b[i] == 0x1B and i + 1 < len(b) and b[i + 1] == ord("["):
                        j = i + 2
                        while j < len(b) and not (0x40 <= b[j] <= 0x7E):
                            j += 1
                        j = min(j + 1, len(b))
                        self.key(b[i:j])
                        i = j
                    else:
                        self.key(bytes([b[i]]))
                        i += 1
                self.draw()
        finally:
            self._teardown()
        print(f"\nherd paused — tokens={self.eng.tokens} switches={self.eng.switches} "
              f"limits={self.eng.limits} acct_rot={self.eng.acct_rot}")
        return 0


def main():
    import herd
    cfg = herd.load_cfg()
    eng = herd.Engine(cfg, n_agents=cfg["agents"])
    eng.start()
    for a in eng.workers:
        a.enqueue("introduce yourself in one line: your name and your model")
    return TUI(eng).loop()


if __name__ == "__main__":
    raise SystemExit(main())
