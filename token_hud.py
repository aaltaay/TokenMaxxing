"""Always-on-top Token HUD.

Tab 1: this billing cycle (the 75% question).
Tab 2: the selected chat's context window, plus billed $ for that chat.
Tab 3: Claude / Codex provider reset countdowns and desktop buzz.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from cursor_usage import (
    billed_for_chat,
    cache_path,
    connect,
    cursor_state_db,
    fetch_cycle,
    followed_chat_id,
    fmt_int,
    fmt_money,
    recent_chats,
    snapshot_for,
)
from reset_schedule import (
    consume_due_events,
    config_path,
    fmt_countdown,
    fmt_et,
    load_config,
    mark_session_started,
    now_et,
    play_buzz,
    save_config,
    snapshot_resets,
)

REFRESH_MS = 800
BILLING_POLL_MS = 4000
RESET_TICK_MS = 1000
LOCK_PORT = 47821
_lock_sock = None

# Apple-inspired dark tokens (macOS Settings / Activity Monitor).
BG = "#1c1c1e"
CARD = "#2c2c2e"
CARD_HI = "#3a3a3c"
SEG_ON = "#5c5c5e"
SEP = "#38383a"
TRACK = "#3a3a3c"
FG = "#f5f5f7"
FG2 = "#98989d"
FG3 = "#636366"
ACCENT = "#0A84FF"
ACCENT_HI = "#409CFF"
OK = "#30D158"
SOON = "#FF9F0A"
NOW = "#FF453A"
BANNER_BG = "#1c3050"
BANNER_BG2 = "#27406a"
BANNER_FG = "#7ec8ff"
FLASH = ("#1c1c1e", "#22222c", "#2a3040", "#22222c", "#1c1c1e")

PAD = 20
GAP = 12
INSET = 14
WIN_W = 640
WIN_H = 920
WIN_MIN_W = 580
WIN_MIN_H = 720


def claim_single_instance() -> bool:
    global _lock_sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        _lock_sock = sock
        return True
    except OSError:
        sock.close()
        return False


def first_family(root: tk.Misc, names: tuple[str, ...]) -> str:
    try:
        have = {n.lower(): n for n in tkfont.families(root)}
    except tk.TclError:
        return names[-1]
    for name in names:
        hit = have.get(name.lower())
        if hit:
            return hit
    return names[-1]


def bar(pct: float, width: int = 22) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "#" * filled + "." * (width - filled)


def pct_color(pct: float) -> str:
    if pct >= 75:
        return NOW
    if pct >= 50:
        return SOON
    return OK


def status_kind(left: float) -> str:
    if left <= 120:
        return "now"
    if left <= 900:
        return "soon"
    return "ok"


def kind_color(kind: str) -> str:
    return {"now": NOW, "soon": SOON, "ok": OK, "off": FG2}.get(kind, FG2)


def _capsule(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, fill: str) -> None:
    if x2 <= x1:
        return
    h = max(1.0, y2 - y1)
    r = h / 2.0
    if x2 - x1 <= h:
        canvas.create_oval(x1, y1, x1 + h, y2, fill=fill, outline="")
        return
    canvas.create_oval(x1, y1, x1 + h, y2, fill=fill, outline="")
    canvas.create_oval(x2 - h, y1, x2, y2, fill=fill, outline="")
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")


class Fonts:
    def __init__(self, root: tk.Misc) -> None:
        ui = first_family(root, ("Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "DejaVu Sans", "TkDefaultFont"))
        mono = first_family(
            root,
            ("Cascadia Mono", "SF Mono", "Consolas", "JetBrains Mono", "Menlo", "DejaVu Sans Mono", "TkFixedFont"),
        )
        self.ui = ui
        self.mono_family = mono
        self.title = (ui, 22)
        self.section = (ui, 12)
        self.body = (ui, 10)
        self.caption = (ui, 9)
        self.micro = (ui, 8)
        self.segment = (ui, 10)
        self.action = (ui, 9)
        self.pill = (ui, 8)
        self.mono = (mono, 10)
        self.mono_sm = (mono, 9)
        self.mono_lg = (mono, 22)
        self.mono_md = (mono, 16)


class ThinMeter(tk.Canvas):
    """Capsule usage / countdown bar."""

    def __init__(self, parent: tk.Misc, *, bg: str = BG, height: int = 6) -> None:
        super().__init__(parent, height=height + 4, bg=bg, highlightthickness=0, bd=0)
        self._pct = 0.0
        self._fill = OK
        self._track = TRACK
        self._bar_h = height
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, pct: float, fill: str | None = None) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self._fill = fill or pct_color(self._pct)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w = max(int(self.winfo_width()), 1)
        h = int(self.cget("height"))
        y1 = max(0, (h - self._bar_h) // 2)
        y2 = y1 + self._bar_h
        _capsule(self, 0, y1, w, y2, self._track)
        fw = w * self._pct / 100.0
        if fw > 2:
            _capsule(self, 0, y1, fw, y2, self._fill)


class StatusPill(tk.Label):
    _LABELS = {"ok": "OK", "soon": "Soon", "now": "Now", "off": "Off"}
    _CHIP = {"ok": "#163226", "soon": "#3a2a12", "now": "#3a1618", "off": CARD_HI}

    def __init__(self, parent: tk.Misc, fonts: Fonts, bg: str = CARD) -> None:
        super().__init__(parent, text="OK", bg=self._CHIP["ok"], fg=OK, font=fonts.pill, padx=9, pady=3)
        self.set("ok")

    def set(self, kind: str) -> None:
        kind = kind if kind in self._LABELS else "ok"
        self.configure(text=self._LABELS[kind], fg=kind_color(kind), bg=self._CHIP[kind])


class SegmentedNotebook:
    """Equal-width pill tabs with a ttk.Notebook-compatible subset."""

    def __init__(self, parent: tk.Misc, fonts: Fonts) -> None:
        self._fonts = fonts
        self.outer = tk.Frame(parent, bg=BG)
        self._bar = tk.Frame(self.outer, bg=CARD, highlightthickness=1, highlightbackground=SEP, highlightcolor=SEP)
        self._bar.pack(fill="x")
        self.body = tk.Frame(self.outer, bg=BG)
        self.body.pack(fill="both", expand=True, pady=(GAP, 0))
        self._items: list[dict] = []
        self._current: int | None = None

    def pack(self, **kw) -> None:
        self.outer.pack(**kw)

    def add(self, frame: tk.Misc, text: str = "") -> None:
        idx = len(self._items)
        cell = tk.Frame(self._bar, bg=CARD)
        cell.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        btn = tk.Label(cell, text=text.strip(), bg=CARD, fg=FG2, font=self._fonts.segment, pady=7)
        btn.pack(fill="both", expand=True)
        rule = tk.Frame(cell, bg=CARD, height=2)
        rule.pack(fill="x", side="bottom")
        btn.bind("<Button-1>", lambda _e, i=idx: self.select(i))
        cell.bind("<Button-1>", lambda _e, i=idx: self.select(i))
        btn.bind("<Enter>", lambda _e, i=idx: self._hover(i, True))
        btn.bind("<Leave>", lambda _e, i=idx: self._hover(i, False))
        frame.configure(bg=BG)
        self._items.append({"frame": frame, "text": text, "btn": btn, "cell": cell, "rule": rule})
        if self._current is None:
            self.select(0)

    def tabs(self) -> tuple[str, ...]:
        return tuple(str(i) for i in range(len(self._items)))

    def tab(self, tab_id, option: str | None = None, **_kw) -> str:
        item = self._items[self._index(tab_id)]
        if option == "text":
            return item["text"]
        return item["text"]

    def select(self, tab_id) -> None:
        idx = self._index(tab_id)
        self._current = idx
        for i, item in enumerate(self._items):
            on = i == idx
            bg = SEG_ON if on else CARD
            fg = FG if on else FG2
            item["btn"].configure(bg=bg, fg=fg)
            item["cell"].configure(bg=bg)
            item["rule"].configure(bg=ACCENT if on else bg)
            if on:
                item["frame"].pack(in_=self.body, fill="both", expand=True)
            else:
                item["frame"].pack_forget()

    def _index(self, tab_id) -> int:
        if isinstance(tab_id, tk.Misc):
            for i, item in enumerate(self._items):
                if item["frame"] is tab_id:
                    return i
            raise KeyError(tab_id)
        return int(tab_id)

    def _hover(self, idx: int, entering: bool) -> None:
        if idx == self._current:
            return
        item = self._items[idx]
        bg = CARD_HI if entering else CARD
        item["btn"].configure(bg=bg)
        item["cell"].configure(bg=bg)


class ProviderCard(tk.Frame):
    """Claude / Codex card. cget('text') stays a test-friendly summary."""

    def __init__(self, parent: tk.Misc, title: str, fonts: Fonts, on_mark) -> None:
        super().__init__(parent, bg=CARD, highlightthickness=1, highlightbackground=SEP, highlightcolor=SEP)
        self._fonts = fonts
        self._summary = ""
        self.soonest = 10**9
        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=INSET, pady=(10, 4))
        tk.Label(head, text=title, bg=CARD, fg=FG, font=fonts.section, anchor="w").pack(side="left")
        self.pill = StatusPill(head, fonts, bg=CARD)
        self.pill.pack(side="right")
        self.session = self._clock(self, "Session")
        hairline(self, padx=INSET)
        self.weekly = self._clock(self, "Weekly")
        btn_row = tk.Frame(self, bg=CARD)
        btn_row.pack(fill="x", padx=INSET, pady=(6, 10))
        make_action(btn_row, "Mark session now", on_mark, fonts=fonts).pack(side="left")

    def cget(self, key):
        if key == "text":
            return self._summary
        return super().cget(key)

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        if "text" in kw:
            self._summary = kw.pop("text")
        kw.pop("fg", None)
        if kw:
            return super().configure(**kw)
        return None

    def config(self, cnf=None, **kw):
        return self.configure(cnf, **kw)

    def apply(self, rows: list[dict], now) -> None:
        lines: list[str] = []
        soonest = 10**9
        enabled_any = False
        for row in rows:
            left = (row["next"] - now).total_seconds()
            soonest = min(soonest, left)
            enabled_any = enabled_any or bool(row.get("enabled"))
            flags: list[str] = []
            if not row.get("enabled"):
                flags.append("off")
            if row["kind"] == "session":
                flags.append("anchored" if row.get("anchored") else "rolling midnight ET")
            if row.get("placeholder"):
                flags.append("PLACEHOLDER")
            if row.get("error"):
                flags.append(row["error"])
            last = row.get("last_buzzed") or "--"
            lines.extend(
                [
                    f"{row['kind'].upper():<8} {fmt_countdown(left):>8}   next {fmt_et(row['next'])}",
                    f"         {' · '.join(flags) if flags else 'ready'}",
                    f"         last buzz {last}",
                    "",
                ]
            )
            self._paint_clock(self.session if row["kind"] == "session" else self.weekly, row, left, flags, last)
        self._summary = "\n".join(lines).rstrip() or "--"
        self.soonest = soonest
        self.pill.set(status_kind(soonest) if enabled_any else "off")

    def _clock(self, parent: tk.Misc, title: str) -> dict:
        wrap = tk.Frame(parent, bg=CARD)
        wrap.pack(fill="x", padx=INSET, pady=(0, 6))
        top = tk.Frame(wrap, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=title, bg=CARD, fg=FG2, font=self._fonts.caption, anchor="w").pack(side="left")
        left = tk.Label(top, text="--", bg=CARD, fg=FG, font=self._fonts.mono_md, anchor="e")
        left.pack(side="right")
        nxt = tk.Label(wrap, text="", bg=CARD, fg=FG3, font=self._fonts.mono_sm, anchor="w")
        nxt.pack(fill="x", pady=(2, 0))
        meta = tk.Label(wrap, text="", bg=CARD, fg=FG3, font=self._fonts.micro, anchor="w")
        meta.pack(fill="x")
        meter = ThinMeter(wrap, bg=CARD, height=4)
        meter.pack(fill="x", pady=(8, 0))
        return {"left": left, "next": nxt, "meta": meta, "meter": meter}

    def _paint_clock(self, widgets: dict, row: dict, left: float, flags: list[str], last: str) -> None:
        kind = status_kind(left)
        color = kind_color(kind)
        hours = float(row.get("hours") or 5)
        window_s = max(hours * 3600.0, 1.0)
        remain = max(0.0, min(100.0, 100.0 * left / window_s))
        widgets["left"].configure(text=fmt_countdown(left), fg=color)
        widgets["next"].configure(text=f"next {fmt_et(row['next'])}")
        extra = " · ".join(flags) if flags else "ready"
        widgets["meta"].configure(text=f"{extra}  ·  last buzz {last}")
        widgets["meter"].set(remain, color)


def hairline(parent: tk.Misc, *, padx: int = 0, pady: int = 4) -> tk.Frame:
    line = tk.Frame(parent, bg=SEP, height=1)
    line.pack(fill="x", padx=padx, pady=pady)
    return line


def make_card(parent: tk.Misc) -> tk.Frame:
    return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=SEP, highlightcolor=SEP)


def make_action(parent: tk.Misc, text: str, command, *, fonts: Fonts, primary: bool = False) -> tk.Frame:
    bg = ACCENT if primary else CARD_HI
    fg = "#ffffff" if primary else FG
    hover = ACCENT_HI if primary else "#505053"
    fr = tk.Frame(parent, bg=bg)
    lbl = tk.Label(fr, text=text, bg=bg, fg=fg, font=fonts.action, padx=14, pady=6)
    lbl.pack()

    def enter(_e=None) -> None:
        fr.configure(bg=hover)
        lbl.configure(bg=hover)

    def leave(_e=None) -> None:
        fr.configure(bg=bg)
        lbl.configure(bg=bg)

    def click(_e=None) -> None:
        command()

    for w in (fr, lbl):
        w.bind("<Enter>", enter)
        w.bind("<Leave>", leave)
        w.bind("<Button-1>", click)
        w.configure(cursor="hand2")
    return fr


def section_label(parent: tk.Misc, text: str, fonts: Fonts, *, bg: str = BG) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=bg, fg=FG2, font=fonts.caption, anchor="w")
    lbl.pack(fill="x", pady=(10, 4))
    return lbl


class VerticalScroll(tk.Frame):
    """Page scroller so padded cards can overflow without clipping."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._set_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _set_scroll(self, first, last) -> None:
        self.vsb.set(first, last)

    def _bind_wheel(self, on: bool) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            if on:
                self.canvas.bind_all(seq, self._wheel)
            else:
                self.canvas.unbind_all(seq)

    def _wheel(self, event) -> str | None:
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            self.canvas.yview_scroll(-2, "units")
        else:
            self.canvas.yview_scroll(2, "units")
        return "break"


class SimpleTable(tk.Frame):
    """Quiet column list. Same insert/delete/heading hooks apply_billing already uses."""

    def __init__(self, parent: tk.Misc, columns: tuple[str, ...], widths: dict, fonts: Fonts) -> None:
        super().__init__(parent, bg=CARD)
        self.columns = columns
        self.widths = widths
        self.fonts = fonts
        self._head_text = {c: c for c in columns}
        self._rows: list[tuple] = []
        self._head = tk.Frame(self, bg=CARD_HI)
        self._head.pack(fill="x")
        self._body = tk.Frame(self, bg=CARD)
        self._body.pack(fill="x")
        self._render_head()

    def heading(self, col, text=None, **_kw) -> None:
        if text is not None:
            self._head_text[col] = text
            self._render_head()

    def column(self, col, **kw) -> None:
        if "width" in kw:
            self.widths[col] = kw["width"]

    def get_children(self) -> tuple[str, ...]:
        return tuple(str(i) for i in range(len(self._rows)))

    def delete(self, *items) -> None:
        if not items:
            return
        drop = {str(x) for x in items}
        self._rows = [row for i, row in enumerate(self._rows) if str(i) not in drop]
        self._render_rows()

    def insert(self, _parent, _index, values=()) -> str:
        self._rows.append(tuple(values))
        self._render_rows()
        return str(len(self._rows) - 1)

    def _stretch(self, col: str) -> int:
        return 1 if col in ("name", "model") else 0

    def _render_head(self) -> None:
        for child in self._head.winfo_children():
            child.destroy()
        for i, col in enumerate(self.columns):
            tk.Label(
                self._head,
                text=self._head_text[col],
                bg=CARD_HI,
                fg=FG3,
                font=self.fonts.micro,
                anchor="w",
            ).grid(row=0, column=i, sticky="ew", padx=8, pady=6)
            self._head.grid_columnconfigure(i, weight=self._stretch(col), minsize=min(self.widths.get(col, 60), 90))

    def _render_rows(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        if not self._rows:
            tk.Label(self._body, text="No rows yet", bg=CARD, fg=FG3, font=self.fonts.caption, anchor="w").pack(
                fill="x", padx=8, pady=10
            )
            return
        for r, values in enumerate(self._rows):
            row_bg = CARD if r % 2 == 0 else "#323234"
            fr = tk.Frame(self._body, bg=row_bg)
            fr.pack(fill="x")
            for i, col in enumerate(self.columns):
                val = values[i] if i < len(values) else ""
                tk.Label(
                    fr,
                    text=str(val),
                    bg=row_bg,
                    fg=FG,
                    font=self.fonts.mono_sm,
                    anchor="w",
                ).grid(row=0, column=i, sticky="ew", padx=8, pady=5)
                fr.grid_columnconfigure(i, weight=self._stretch(col), minsize=min(self.widths.get(col, 60), 90))


class TokenHud:
    def __init__(self) -> None:
        self.pinned: str | None = None
        self.recent: list[tuple[str, str]] = []
        self.report: dict | None = None
        self.billing_error: str | None = None
        self._fetching = False
        self._last_fetch_attempt = 0.0
        self._reset_cfg_mtime = 0.0
        self._syncing_toggles = False
        self._banner_gen = 0
        self._flash_n = 0
        self._wrap = 560
        self._alive = True
        self._pause_local = False
        self._after_ids: list = []

        self.root = tk.Tk()
        self.root.title("Token HUD")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)
        self.root.minsize(WIN_MIN_W, WIN_MIN_H)
        self.root.geometry(f"{WIN_W}x{WIN_H}+40+60")
        self._tk_destroy = self.root.destroy
        self.root.destroy = self._destroy
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_destroy)
        self.f = Fonts(self.root)
        self._style()

        self.banner = tk.Frame(self.root, bg=BG)
        self.banner_pill = tk.Frame(self.banner, bg=BANNER_BG)
        self.banner_pill.pack(fill="x")
        self.banner_label = tk.Label(
            self.banner_pill,
            text="",
            bg=BANNER_BG,
            fg=BANNER_FG,
            font=self.f.body,
            wraplength=560,
            justify="left",
            anchor="w",
            padx=14,
            pady=8,
        )
        self.banner_label.pack(fill="x")

        self.chrome = tk.Frame(self.root, bg=BG)
        self.chrome.pack(fill="both", expand=True, padx=PAD, pady=(16, 16))

        self.header = tk.Frame(self.chrome, bg=BG)
        self.header.pack(fill="x")
        title_row = tk.Frame(self.header, bg=BG)
        title_row.pack(fill="x")
        self.header_plan = tk.Label(
            title_row, text="Loading cycle...", bg=BG, fg=FG, anchor="w", font=self.f.title
        )
        self.header_plan.pack(side="left", fill="x", expand=True)
        tk.Label(title_row, text="Token HUD", bg=BG, fg=FG3, font=self.f.caption).pack(side="right")
        self.header_status = tk.Label(self.header, text="", bg=BG, fg=FG2, anchor="w", font=self.f.body)
        self.header_status.pack(fill="x", pady=(2, 10))

        self.meter_api = self._header_meter("Other Models", "named / API")
        self.meter_auto = self._header_meter("Cursor Models", "Grok / Composer")
        self.header_note = tk.Label(
            self.header, text="", bg=BG, fg=FG3, anchor="w", font=self.f.micro, wraplength=560, justify="left"
        )
        self.header_note.pack(fill="x", pady=(8, 0))

        self.nb = SegmentedNotebook(self.chrome, self.f)
        self.tab_cycle = tk.Frame(self.nb.body, bg=BG)
        self.tab_chat = tk.Frame(self.nb.body, bg=BG)
        self.tab_resets = tk.Frame(self.nb.body, bg=BG)
        self._build_cycle_tab()
        self._build_chat_tab()
        self._build_resets_tab()
        self.nb.add(self.tab_cycle, text="This cycle")
        self.nb.add(self.tab_chat, text="This chat")
        self.nb.add(self.tab_resets, text="Resets")
        self.nb.pack(fill="both", expand=True, pady=(16, 0))

        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Control-Key-1>", lambda _e: self.nb.select(0))
        self.root.bind("<Control-Key-2>", lambda _e: self.nb.select(1))
        self.root.bind("<Control-Key-3>", lambda _e: self.nb.select(2))
        self.refresh_local()
        self._after(200, self._billing_tick)
        self._after(400, self._reset_tick)

    def _cancel_afters(self) -> None:
        self._alive = False
        for aid in list(self._after_ids):
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            leftover = self.root.tk.call("after", "info")
            for aid in str(leftover).split():
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    def _destroy(self, *args, **kwargs) -> None:
        self._cancel_afters()
        try:
            self._tk_destroy(*args, **kwargs)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        self._destroy()

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._alive = False

    def _after(self, ms: int, fn) -> None:
        if not self._alive:
            return
        try:
            self._after_ids.append(self.root.after(ms, fn))
        except tk.TclError:
            pass

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "Dark.Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=FG,
            rowheight=26,
            font=self.f.body,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=CARD_HI,
            foreground=FG2,
            relief="flat",
            font=self.f.micro,
            borderwidth=0,
        )
        style.map("Dark.Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=CARD_HI,
            troughcolor=BG,
            borderwidth=0,
            arrowsize=12,
        )

    def _header_meter(self, name: str, hint: str) -> ThinMeter:
        row = tk.Frame(self.header, bg=BG)
        row.pack(fill="x", pady=(0, 2))
        left = tk.Label(row, text=name, bg=BG, fg=FG2, font=self.f.caption, anchor="w")
        left.pack(side="left")
        if name == "Other Models":
            self.header_api = left
            pct_holder = "header_api_pct"
        else:
            self.header_auto = left
            pct_holder = "header_auto_pct"
        tk.Label(row, text=hint, bg=BG, fg=FG3, font=self.f.micro).pack(side="right")
        pct = tk.Label(row, text="--%", bg=BG, fg=FG, font=self.f.mono, padx=8)
        pct.pack(side="right")
        setattr(self, pct_holder, pct)
        meter = ThinMeter(self.header, bg=BG, height=5)
        meter.pack(fill="x", pady=(0, 8))
        return meter

    def _build_cycle_tab(self) -> None:
        scroll = VerticalScroll(self.tab_cycle)
        scroll.pack(fill="both", expand=True)
        page = scroll.inner
        btn_row = tk.Frame(page, bg=BG)
        btn_row.pack(fill="x", pady=(0, 8))
        make_action(btn_row, "Refresh usage", lambda: self._try_fetch(force=True), fonts=self.f).pack(side="left")
        self.cycle_status = tk.Label(btn_row, text="", bg=BG, fg=FG3, anchor="w", font=self.f.micro)
        self.cycle_status.pack(side="left", padx=10)

        spend = make_card(page)
        spend.pack(fill="x", pady=(0, 4))
        inner = tk.Frame(spend, bg=CARD)
        inner.pack(fill="x", padx=INSET, pady=12)
        tk.Label(inner, text="Spend", bg=CARD, fg=FG2, font=self.f.caption, anchor="w").pack(fill="x")
        self.spend_big = tk.Label(inner, text="$--", bg=CARD, fg=FG, font=self.f.mono_lg, anchor="w")
        self.spend_big.pack(fill="x", pady=(0, 8))
        self.kv_included = self._kv(inner, "Included")
        self.kv_ondemand = self._kv(inner, "On-demand")
        self.kv_cloud = self._kv(inner, "Cloud")
        self.kv_interactive = self._kv(inner, "Interactive")
        self.kv_tokens = self._kv(inner, "Tokens")
        self.cycle_stats = tk.Label(
            inner,
            text="Fetching billing cycle from cursor.com...",
            justify="left",
            anchor="nw",
            font=self.f.mono_sm,
            bg=CARD,
            fg=FG2,
            wraplength=560,
        )

        section_label(page, "Where the dollars went", self.f)
        chats_card = make_card(page)
        chats_card.pack(fill="x")
        self.chats_tree = self._tree(
            chats_card,
            ("dollars", "other", "n", "hl", "name"),
            {"dollars": 72, "other": 72, "n": 44, "hl": 48, "name": 280},
            height=7,
        )
        self.chats_tree.heading("dollars", text="$")
        self.chats_tree.heading("other", text="Other $")
        self.chats_tree.heading("n", text="n")
        self.chats_tree.heading("hl", text="cloud")
        self.chats_tree.heading("name", text="Chat / cloud agent")

        section_label(page, "By model", self.f)
        models_card = make_card(page)
        models_card.pack(fill="x")
        self.models_tree = self._tree(
            models_card,
            ("dollars", "pool", "in", "out", "model"),
            {"dollars": 72, "pool": 70, "in": 70, "out": 70, "model": 240},
            height=5,
        )
        self.models_tree.heading("dollars", text="$")
        self.models_tree.heading("pool", text="pool")
        self.models_tree.heading("in", text="in")
        self.models_tree.heading("out", text="out")
        self.models_tree.heading("model", text="model")

        section_label(page, "By day", self.f)
        days_card = make_card(page)
        days_card.pack(fill="x")
        self.days_tree = self._tree(
            days_card,
            ("date", "dollars", "n"),
            {"date": 120, "dollars": 80, "n": 70},
            height=3,
        )
        self.days_tree.heading("date", text="date")
        self.days_tree.heading("dollars", text="$")
        self.days_tree.heading("n", text="events")

        section_label(page, "Was it used properly?", self.f)
        why_card = make_card(page)
        why_card.pack(fill="x", pady=(0, 8))
        self.why = tk.Text(
            why_card,
            height=6,
            bg=CARD,
            fg=FG,
            wrap="word",
            font=self.f.body,
            relief="flat",
            highlightthickness=0,
            bd=0,
            padx=INSET,
            pady=10,
            insertbackground=FG,
        )
        self.why.pack(fill="both", expand=True)
        self.why.configure(state="disabled")

    def _build_chat_tab(self) -> None:
        scroll = VerticalScroll(self.tab_chat)
        scroll.pack(fill="both", expand=True)
        page = scroll.inner
        top = tk.Frame(page, bg=BG)
        top.pack(fill="x", pady=(0, 8))
        self.follow = tk.Label(top, text="Following Cursor selection", bg=BG, fg=FG2, anchor="w", font=self.f.body)
        self.follow.pack(side="left", fill="x", expand=True)
        make_action(top, "Follow Cursor tab", self.clear_pin, fonts=self.f).pack(side="right")

        card = make_card(page)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=INSET, pady=12)
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        self.chat_model = tk.Label(head, text="", bg=CARD, fg=FG2, font=self.f.caption, anchor="w")
        self.chat_model.pack(side="left", fill="x", expand=True)
        self.chat_pct = tk.Label(head, text="--%", bg=CARD, fg=FG, font=self.f.mono_lg)
        self.chat_pct.pack(side="right")
        self.meter_chat = ThinMeter(inner, bg=CARD, height=6)
        self.meter_chat.pack(fill="x", pady=(8, 10))
        self.chat_input = tk.Label(inner, text="Loading...", bg=CARD, fg=FG, font=self.f.mono, anchor="w")
        self.chat_input.pack(fill="x")
        self.chat_billed = tk.Label(inner, text="", bg=CARD, fg=FG2, font=self.f.mono_sm, anchor="w")
        self.chat_billed.pack(fill="x", pady=(4, 0))

        section_label(page, "Context breakdown", self.f)
        body_card = make_card(page)
        body_card.pack(fill="x")
        self.body = tk.Label(
            body_card,
            text="Loading...",
            justify="left",
            anchor="nw",
            font=self.f.mono_sm,
            bg=CARD,
            fg=FG,
            padx=INSET,
            pady=10,
        )
        self.body.pack(fill="x", anchor="w")

        section_label(page, "Click a chat if the tab switch lags", self.f)
        list_card = make_card(page)
        list_card.pack(fill="x")
        self.listbox = tk.Listbox(
            list_card,
            height=6,
            bg=CARD,
            fg=FG,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            activestyle="none",
            font=self.f.body,
            relief="flat",
            highlightthickness=0,
            bd=0,
        )
        self.listbox.pack(fill="x", padx=8, pady=8)
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)
        tk.Label(
            page,
            text="INPUT is this chat's live context window. OUTPUT billed is on This cycle.",
            font=self.f.micro,
            bg=BG,
            fg=FG3,
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(10, 8))

    def _build_resets_tab(self) -> None:
        scroll = VerticalScroll(self.tab_resets)
        scroll.pack(fill="both", expand=True)
        page = scroll.inner
        top = tk.Frame(page, bg=BG)
        top.pack(fill="x", pady=(0, 10))
        tk.Label(
            top,
            text="Provider windows -- not Cursor billing-cycle %.",
            bg=BG,
            fg=FG2,
            anchor="w",
            font=self.f.caption,
        ).pack(side="left", fill="x", expand=True)
        make_action(top, "Test buzz", self._test_buzz, fonts=self.f, primary=True).pack(side="right")

        self.reset_cards = {}
        for key, title in (("claude", "Claude"), ("codex", "Codex / ChatGPT")):
            card = ProviderCard(page, title, self.f, lambda k=key: self._mark_session(k))
            card.pack(fill="x", pady=(0, 10))
            self.reset_cards[key] = card

        alerts = make_card(page)
        alerts.pack(fill="x")
        box = tk.Frame(alerts, bg=CARD)
        box.pack(fill="x", padx=INSET, pady=10)
        tk.Label(box, text="Alerts", bg=CARD, fg=FG2, font=self.f.caption, anchor="w").pack(fill="x", pady=(0, 6))
        self.var_claude_session = tk.BooleanVar(value=True)
        self.var_claude_weekly = tk.BooleanVar(value=True)
        self.var_codex_session = tk.BooleanVar(value=True)
        self.var_codex_weekly = tk.BooleanVar(value=True)
        self.var_prewarn = tk.BooleanVar(value=True)
        self.var_sound = tk.BooleanVar(value=True)
        pairs = (
            (self.var_claude_session, "Claude session"),
            (self.var_claude_weekly, "Claude weekly"),
            (self.var_codex_session, "Codex session"),
            (self.var_codex_weekly, "Codex weekly"),
            (self.var_prewarn, "Pre-warn"),
            (self.var_sound, "Sound"),
        )
        grid = tk.Frame(box, bg=CARD)
        grid.pack(fill="x")
        for i, (var, text) in enumerate(pairs):
            tk.Checkbutton(
                grid,
                text=text,
                variable=var,
                command=self._save_reset_toggles,
                bg=CARD,
                fg=FG,
                selectcolor=BG,
                activebackground=CARD,
                activeforeground=FG,
                highlightthickness=0,
                font=self.f.caption,
                bd=0,
            ).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 16), pady=2)

        self.reset_status = tk.Label(page, text="", bg=BG, fg=FG3, anchor="w", font=self.f.micro)
        self.reset_status.pack(fill="x", pady=(10, 4))
        tk.Label(
            page,
            text=(
                f"Edit {config_path()} (America/New_York). Weekly times ship as PLACEHOLDERS -- "
                "paste real weekday+time from Settings -> Usage, then set weekly_placeholder to false. "
                "HUD reloads the JSON on save. No Anthropic/OpenAI scrape."
            ),
            bg=BG,
            fg=FG3,
            anchor="w",
            wraplength=560,
            justify="left",
            font=self.f.micro,
        ).pack(fill="x", pady=(0, 8))
        self._apply_resets()

    def _kv(self, parent: tk.Misc, label: str) -> tk.Label:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=CARD, fg=FG3, font=self.f.caption, anchor="w").pack(side="left")
        val = tk.Label(row, text="", bg=CARD, fg=FG, font=self.f.mono_sm, anchor="e")
        val.pack(side="right")
        return val

    def _tree(self, parent, columns, widths, height: int) -> SimpleTable:
        table = SimpleTable(parent, columns, widths, self.f)
        table.pack(fill="x")
        return table

    def _on_resize(self, event) -> None:
        if event.widget is not self.root:
            return
        wrap = max(event.width - 56, 280)
        if wrap == self._wrap:
            return
        self._wrap = wrap
        for widget in (self.header_note, self.banner_label, self.cycle_stats, self.body):
            try:
                widget.configure(wraplength=wrap)
            except tk.TclError:
                pass

    def clear_pin(self) -> None:
        self.pinned = None

    def on_pick(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.recent):
            self.pinned = self.recent[idx][0]

    def refresh_local(self) -> None:
        if not self._alive:
            return
        if self._pause_local:
            self._after(REFRESH_MS, self.refresh_local)
            return
        try:
            db = cursor_state_db()
            if not db.exists():
                raise FileNotFoundError("Cursor DB not found")
            con = connect(db)
            try:
                self.recent = recent_chats(con)
                cid = followed_chat_id(con, self.pinned)
                snap = snapshot_for(con, cid) if cid else {"error": "No active chat"}
            finally:
                con.close()
            mode = "PINNED" if self.pinned else "Cursor tab"
            name = snap.get("name") or snap.get("id") or "?"
            self.follow.configure(text=f"{mode}: {name}")
            self._paint_chat(snap)
            self.redraw_list(snap.get("id"))
        except Exception as exc:
            self._paint_chat({"error": f"Read failed:\n{exc}"})
        self._after(REFRESH_MS, self.refresh_local)

    def _paint_chat(self, snap: dict) -> None:
        if snap.get("error"):
            self.chat_model.configure(text="")
            self.chat_pct.configure(text="--", fg=FG3)
            self.meter_chat.set(0, TRACK)
            self.chat_input.configure(text=snap["error"], fg=SOON)
            self.chat_billed.configure(text="")
            self.body.configure(text=snap["error"], fg=SOON)
            return
        pct = float(snap.get("pct") or 0)
        used = snap.get("used") or 0
        limit = snap.get("limit") or 0
        self.chat_model.configure(text=snap.get("model") or "")
        self.chat_pct.configure(text=f"{pct:.0f}%", fg=pct_color(pct))
        self.meter_chat.set(pct)
        self.chat_input.configure(text=f"INPUT   {fmt_int(used)}  /  {fmt_int(limit)}", fg=FG)
        billed = billed_for_chat(self.report, snap.get("id"))
        if billed:
            self.chat_billed.configure(
                text=(
                    f"THIS CYCLE  {fmt_money(billed.get('cents') or 0)}  "
                    f"(Other {fmt_money(billed.get('other_cents') or 0)})  "
                    f"{billed.get('n') or 0} events"
                )
            )
        else:
            self.chat_billed.configure(text="THIS CYCLE  no billed events matched this chat id yet")
        self.body.configure(text=self.render_chat(snap), fg=FG)

    def _billing_tick(self) -> None:
        if not self._alive:
            return
        self._try_fetch(force=False)
        self._after(BILLING_POLL_MS, self._billing_tick)

    def _try_fetch(self, force: bool = False) -> None:
        now = time.time()
        cached = cache_path()
        if not force and self.report is None and cached.exists():
            try:
                self.report = json.loads(cached.read_text(encoding="utf-8"))
                self.apply_billing()
            except Exception:
                pass
        due = force or (now - self._last_fetch_attempt > 120)
        if due and not self._fetching:
            self._fetching = True
            self._last_fetch_attempt = now
            self.cycle_status.configure(text="Fetching cursor.com...")
            threading.Thread(target=self._fetch_thread, args=(force,), daemon=True).start()

    def _fetch_thread(self, force: bool) -> None:
        try:
            report = fetch_cycle(force=force)
            self._after(0, lambda r=report: self._on_billing_ok(r))
        except Exception as exc:
            self._after(0, lambda e=exc: self._on_billing_err(str(e)))

    def _on_billing_ok(self, report: dict) -> None:
        self.report = report
        self.billing_error = None
        self._fetching = False
        self.apply_billing()

    def _on_billing_err(self, err: str) -> None:
        self.billing_error = err
        self._fetching = False
        self.cycle_status.configure(text=f"Usage fetch failed: {err[:80]}")
        if self.report is None:
            self.header_plan.configure(text="Cycle usage unavailable", fg=SOON)
            self.header_status.configure(text="Sign in again in Cursor if this stays empty")

    def apply_billing(self) -> None:
        r = self.report or {}
        api = float(r.get("api_pct") or 0)
        auto = float(r.get("auto_pct") or 0)
        total = float(r.get("total_pct") or 0)
        plan = r.get("plan") or "Cursor"
        self.header_plan.configure(text=plan, fg=FG)
        self.header_status.configure(
            text=f"Day {r.get('elapsed_days') or 0:.1f} of ~30  ·  Overall {total:.0f}%"
        )
        self.header_api_pct.configure(text=f"{api:.0f}%", fg=pct_color(api))
        self.header_auto_pct.configure(text=f"{auto:.0f}%", fg=pct_color(auto))
        self.meter_api.set(api)
        self.meter_auto.set(auto)
        gb = r.get("grok_bot_pct")
        extra = f"Grok Bot weekly {gb:.0f}%." if gb is not None else ""
        left = r.get("api_days_left")
        left_s = f" Other-models slice lasts ~{left:.1f} more days at this rate." if left is not None else ""
        self.header_note.configure(text=f"{r.get('api_msg') or ''}  {extra}{left_s}".strip())

        age = time.time() - float(r.get("fetched_at") or time.time())
        self.cycle_status.configure(text=f"Snapshot {int(age)}s ago · {r.get('events_fetched') or 0} events")

        hl = r.get("headless") or {}
        inter = r.get("interactive") or {}
        self.spend_big.configure(text=fmt_money(r.get("total_spend_cents") or 0))
        inc = fmt_money(r.get("included_cents") or 0)
        bonus = fmt_money(r.get("bonus_cents") or 0)
        on_demand = "On" if r.get("on_demand_enabled") else "Off (hard stop when pools empty)"
        cloud = f"{fmt_money(hl.get('cents') or 0)}  ·  {hl.get('n') or 0}"
        interactive = f"{fmt_money(inter.get('cents') or 0)}  ·  {inter.get('n') or 0}"
        tokens = (
            f"in {fmt_int(r.get('agg_input'))}   out {fmt_int(r.get('agg_output'))}   "
            f"cacheRead {fmt_int(r.get('agg_cache_read'))}   cacheWrite {fmt_int(r.get('agg_cache_write'))}"
        )
        self.kv_included.configure(text=f"{inc}  +  bonus {bonus}")
        self.kv_ondemand.configure(text=on_demand)
        self.kv_cloud.configure(text=cloud)
        self.kv_interactive.configure(text=interactive)
        self.kv_tokens.configure(text=tokens)
        self.cycle_stats.configure(
            text="\n".join(
                [
                    f"Included {inc}  +  bonus {bonus}",
                    f"On-demand  {on_demand}",
                    f"Cloud {cloud}    Interactive {interactive}",
                    tokens,
                ]
            ),
            fg=FG2,
        )

        for tree in (self.chats_tree, self.models_tree, self.days_tree):
            tree.delete(*tree.get_children())

        ultra_chats = [c for c in r.get("conversations") or [] if not str(c.get("name", "")).startswith("Grok Bot")]
        for c in ultra_chats[:25]:
            self.chats_tree.insert(
                "",
                "end",
                values=(
                    fmt_money(c.get("cents") or 0),
                    fmt_money(c.get("other_cents") or 0),
                    c.get("n") or 0,
                    c.get("headless") or 0,
                    (c.get("name") or "")[:48],
                ),
            )

        for m in r.get("models") or []:
            pool = m.get("pool") or ""
            if m.get("tier") == 1:
                pool = "other"
            elif m.get("tier") == 2:
                pool = "cursor"
            self.models_tree.insert(
                "",
                "end",
                values=(
                    fmt_money(m.get("cents") or 0),
                    pool,
                    fmt_int(m.get("input")),
                    fmt_int(m.get("output")),
                    m.get("model") or "",
                ),
            )

        for d in r.get("days") or []:
            self.days_tree.insert("", "end", values=(d.get("date"), fmt_money(d.get("cents") or 0), d.get("n") or 0))

        self.why.configure(state="normal")
        self.why.delete("1.0", "end")
        findings = r.get("findings") or []
        self.why.insert("1.0", "\n\n".join(f"- {n}" for n in findings))
        self.why.configure(state="disabled")

    def render_chat(self, snap: dict) -> str:
        if snap.get("error"):
            return snap["error"]
        lines: list[str] = []
        cats = (snap.get("breakdown") or {}).get("categories") or []
        if cats:
            for cat in cats:
                label = (cat.get("label") or cat.get("id") or "?")[:22]
                tok = cat.get("estimatedTokens") or 0
                lines.append(f"{label:<22} {fmt_int(tok):>8}")
        tax = float(snap.get("tax_pct") or 0)
        if lines:
            lines.append("")
        lines.append(f"Context tax (rules/tools/skills/MCP/subagents): {tax:.0f}%")
        used = snap.get("used") or 0
        if used:
            grok_fast = used / 1_000_000.0
            lines.append(f"Next Grok Fast turn, cache-read alone ~ ${grok_fast:,.2f}")
            lines.append(f"Next Grok non-fast cache-read ~ ${grok_fast * 0.5:,.2f}")
        if used >= 200_000:
            lines.append("")
            lines.append("This chat is fat. A new chat is cheaper than another Opus/Sol turn here.")
        lines.append(f"Sub-agents in this chat: {snap.get('num_sub') or 0}")
        return "\n".join(lines)

    def redraw_list(self, current_id: str | None) -> None:
        labels = []
        for cid, name in self.recent:
            mark = " *" if cid == current_id else ""
            labels.append(f"{name[:48]}{mark}")
        old = list(self.listbox.get(0, "end"))
        if old == labels:
            return
        self.listbox.delete(0, "end")
        for label in labels:
            self.listbox.insert("end", label)

    def _reset_tick(self) -> None:
        if not self._alive:
            return
        try:
            self._apply_resets()
            events = consume_due_events()
            if events:
                self._on_reset_buzz(events)
        except Exception as exc:
            self.reset_status.configure(text=f"Reset clock error: {exc}", fg=NOW)
        self._after(RESET_TICK_MS, self._reset_tick)

    def _apply_resets(self) -> None:
        path = config_path()
        mtime = path.stat().st_mtime if path.exists() else 0.0
        cfg = load_config()
        if mtime != self._reset_cfg_mtime:
            self._reset_cfg_mtime = mtime
            self._syncing_toggles = True
            try:
                claude = (cfg.get("providers") or {}).get("claude") or {}
                codex = (cfg.get("providers") or {}).get("codex") or {}
                self.var_claude_session.set(bool(claude.get("session_enabled", True)))
                self.var_claude_weekly.set(bool(claude.get("weekly_enabled", True)))
                self.var_codex_session.set(bool(codex.get("session_enabled", True)))
                self.var_codex_weekly.set(bool(codex.get("weekly_enabled", True)))
                self.var_prewarn.set(bool(cfg.get("prewarn_enabled", True)))
                self.var_sound.set(bool(cfg.get("buzz_enabled", True)))
            finally:
                self._syncing_toggles = False
        now = now_et()
        grouped: dict[str, list[dict]] = {"claude": [], "codex": []}
        for row in snapshot_resets(now, cfg):
            grouped.setdefault(row["provider"], []).append(row)
        for key, card in self.reset_cards.items():
            card.apply(grouped.get(key) or [], now)
        pre = cfg.get("prewarn_minutes", 2)
        self.reset_status.configure(
            text=f"Pre-warn {pre} min · config {path} · reloads on save",
            fg=FG3,
        )

    def _save_reset_toggles(self) -> None:
        if self._syncing_toggles:
            return
        cfg = load_config()
        cfg.setdefault("providers", {}).setdefault("claude", {})
        cfg.setdefault("providers", {}).setdefault("codex", {})
        cfg["providers"]["claude"]["session_enabled"] = bool(self.var_claude_session.get())
        cfg["providers"]["claude"]["weekly_enabled"] = bool(self.var_claude_weekly.get())
        cfg["providers"]["codex"]["session_enabled"] = bool(self.var_codex_session.get())
        cfg["providers"]["codex"]["weekly_enabled"] = bool(self.var_codex_weekly.get())
        cfg["prewarn_enabled"] = bool(self.var_prewarn.get())
        cfg["buzz_enabled"] = bool(self.var_sound.get())
        save_config(cfg)
        self._reset_cfg_mtime = config_path().stat().st_mtime if config_path().exists() else 0.0
        self._apply_resets()

    def _mark_session(self, provider: str) -> None:
        mark_session_started(provider)
        self._reset_cfg_mtime = 0.0
        self._apply_resets()
        title = "Claude" if provider == "claude" else "Codex / ChatGPT"
        self._show_banner(f"{title} session anchored now")

    def _test_buzz(self) -> None:
        play_buzz()
        try:
            self.root.bell()
        except tk.TclError:
            pass
        self._show_banner("Test buzz")

    def _on_reset_buzz(self, events: list) -> None:
        play_buzz()
        try:
            self.root.bell()
        except tk.TclError:
            pass
        try:
            self.root.lift()
        except tk.TclError:
            pass
        self._show_banner(" · ".join(ev.get("message") or ev.get("label") or "reset" for ev in events))

    def _show_banner(self, message: str) -> None:
        self._banner_gen += 1
        gen = self._banner_gen
        self.banner_label.configure(text=message)
        if not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", before=self.chrome, padx=PAD, pady=(12, 0))
        self._flash_n = 0
        self._flash_step(gen)
        self._after(5000, lambda: self._hide_banner(gen))

    def _flash_step(self, gen: int) -> None:
        if not self._alive or gen != self._banner_gen:
            return
        self._flash_n += 1
        bg = FLASH[(self._flash_n - 1) % len(FLASH)]
        pill = BANNER_BG2 if self._flash_n % 2 else BANNER_BG
        self.root.configure(bg=bg)
        self.banner.configure(bg=bg)
        self.banner_pill.configure(bg=pill)
        self.banner_label.configure(bg=pill)
        if self._flash_n < 6:
            self._after(160, lambda: self._flash_step(gen))
        else:
            self.root.configure(bg=BG)
            self.banner.configure(bg=BG)
            self.banner_pill.configure(bg=BANNER_BG)
            self.banner_label.configure(bg=BANNER_BG)

    def _hide_banner(self, gen: int) -> None:
        if not self._alive or gen != self._banner_gen:
            return
        self.banner.pack_forget()
        self.root.configure(bg=BG)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if "--test-buzz" in sys.argv:
        sys.stdout.write(f"test buzz ({play_buzz()})\n")
        sys.exit(0)
    if not claim_single_instance():
        sys.exit(0)
    try:
        TokenHud().run()
    except tk.TclError as exc:
        sys.stderr.write(f"UI failed: {exc}\n")
        sys.exit(1)
