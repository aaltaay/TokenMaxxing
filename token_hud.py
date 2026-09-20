"""Always-on-top Token HUD.

Tab 1: this billing cycle (the 75% question).
Tab 2: the selected chat's context window, plus billed $ for that chat.
Tab 3: Claude / Codex provider reset countdowns and desktop buzz.

Chrome is the OpenDesign Apple (light) HTML shell. Data paths stay in
cursor_usage.py / reset_schedule.py.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
from opendesign_apple import DANGER, FG, META, NOW, OK, SOON, css_root
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
WIN_W = 680
WIN_H = 900
WIN_MIN_W = 600
WIN_MIN_H = 720
HUD_DIR = Path(__file__).resolve().parent / "hud"
_lock_sock = None

DEMO_REPORT = {
    "plan": "Ultra",
    "elapsed_days": 12.4,
    "total_pct": 61,
    "api_pct": 75,
    "auto_pct": 28,
    "api_msg": "Other Models is the 75% bar.",
    "grok_bot_pct": 12,
    "api_days_left": 4.2,
    "events_fetched": 184,
    "total_spend_cents": 18422,
    "included_cents": 10000,
    "bonus_cents": 8422,
    "on_demand_enabled": False,
    "headless": {"cents": 9010, "n": 8},
    "interactive": {"cents": 9412, "n": 176},
    "agg_input": 2100000,
    "agg_output": 140000,
    "agg_cache_read": 6400000,
    "agg_cache_write": 210000,
    "conversations": [
        {
            "id": "demo-chat",
            "name": "Unrestricted AI suggestion",
            "cents": 4520,
            "other_cents": 3100,
            "n": 14,
            "headless": 0,
        },
        {"name": "Cloud invoice parser", "cents": 9010, "other_cents": 8800, "n": 8, "headless": 8},
        {"name": "Timezone and sale stuck", "cents": 860, "other_cents": 120, "n": 6, "headless": 0},
    ],
    "models": [
        {"model": "claude-4.6-sonnet", "cents": 6200, "tier": 1, "input": 420000, "output": 38000},
        {"model": "grok-4.6", "cents": 4100, "tier": 2, "input": 980000, "output": 62000},
        {"model": "composer-2", "cents": 900, "tier": 2, "input": 210000, "output": 14000},
    ],
    "days": [
        {"date": "2026-09-20", "cents": 18422, "n": 28},
        {"date": "2026-09-19", "cents": 2100, "n": 18},
    ],
    "findings": [
        "Other Models is the scarce pool.",
        "Cloud / headless events are a large slice of Other $.",
    ],
}

DEMO_CHAT = {
    "id": "demo-chat",
    "name": "Unrestricted AI suggestion",
    "model": "grok-4.6",
    "pct": 80,
    "used": 204140,
    "limit": 256000,
    "tax_pct": 31,
    "num_sub": 0,
    "breakdown": {
        "categories": [
            {"label": "System prompt", "estimatedTokens": 1067},
            {"id": "conversation", "estimatedTokens": 171664},
        ]
    },
}

DEMO_RECENT = (
    ("demo-chat", "Unrestricted AI suggestion"),
    ("demo-cloud", "Cloud invoice parser"),
    ("demo-tz", "Timezone and sale stuck"),
)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/apple.css": ("apple.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def hud_port() -> int:
    raw = os.environ.get("TOKEN_HUD_PORT")
    if raw:
        return int(raw)
    return LOCK_PORT


def claim_single_instance() -> bool:
    global _lock_sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", hud_port()))
        sock.listen(1)
        _lock_sock = sock
        return True
    except OSError:
        sock.close()
        return False


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
    return {"now": NOW, "soon": SOON, "ok": META, "off": META}.get(kind, META)


class Attr:
    """Tk-like text holder so existing HUD tests stay headless."""

    def __init__(self, text: str = "", **kw) -> None:
        self._d: dict = {"text": text, "fg": None}
        self._d.update(kw)
        self._mapped = True

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        self._d.update(kw)
        return None

    def config(self, cnf=None, **kw):
        return self.configure(cnf, **kw)

    def cget(self, key):
        return self._d.get(key, "")

    def pack(self, **_kw) -> None:
        self._mapped = True

    def pack_forget(self) -> None:
        self._mapped = False

    def winfo_ismapped(self) -> bool:
        return self._mapped


class Banner(Attr):
    def __init__(self) -> None:
        super().__init__("")
        self._mapped = False


class TextHold:
    def __init__(self) -> None:
        self._text = ""
        self._state = "normal"

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        if "state" in kw:
            self._state = kw["state"]
        if "text" in kw:
            self._text = kw["text"]
        return None

    def delete(self, *_a) -> None:
        self._text = ""

    def insert(self, _idx, text: str) -> None:
        self._text += text

    def get(self, _a=None, _b=None) -> str:
        return self._text


class Flag:
    def __init__(self, value: bool = True) -> None:
        self._v = bool(value)

    def get(self) -> bool:
        return self._v

    def set(self, value) -> None:
        self._v = bool(value)


class ThinMeter:
    def __init__(self) -> None:
        self._pct = 0.0
        self._fill = OK

    def set(self, pct: float, fill: str | None = None) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self._fill = fill or pct_color(self._pct)


class StatusPill:
    _LABELS = {"ok": "OK", "soon": "Soon", "now": "Now", "off": "Off"}

    def __init__(self) -> None:
        self._kind = "ok"
        self._text = "OK"

    def set(self, kind: str) -> None:
        kind = kind if kind in self._LABELS else "ok"
        self._kind = kind
        self._text = self._LABELS[kind]

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "kind":
            return self._kind
        return ""


class ProviderCard:
    """Claude / Codex card. cget('text') stays a test-friendly summary."""

    def __init__(self, title: str) -> None:
        self.title = title
        self._summary = ""
        self.soonest = 10**9
        self.pill = StatusPill()
        self.session: dict = {}
        self.weekly: dict = {}

    def cget(self, key):
        if key == "text":
            return self._summary
        return ""

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        if "text" in kw:
            self._summary = kw.pop("text")
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
            target = self.session if row["kind"] == "session" else self.weekly
            target.clear()
            target.update(self._clock_state(row, left, flags, last))
        self._summary = "\n".join(lines).rstrip() or "--"
        self.soonest = soonest
        self.pill.set(status_kind(soonest) if enabled_any else "off")

    def _clock_state(self, row: dict, left: float, flags: list[str], last: str) -> dict:
        kind = status_kind(left)
        color = kind_color(kind)
        hours = float(row.get("hours") or 5)
        window_s = max(hours * 3600.0, 1.0)
        remain = max(0.0, min(100.0, 100.0 * left / window_s))
        extra = " · ".join(flags) if flags else "ready"
        return {
            "left": fmt_countdown(left),
            "next": f"next {fmt_et(row['next'])}",
            "meta": f"{extra}  ·  last buzz {last}",
            "remain_pct": remain,
            "color": color,
        }


class Pane:
    def __init__(self) -> None:
        self._mapped = False

    def winfo_ismapped(self) -> bool:
        return self._mapped


class SegmentedNotebook:
    def __init__(self) -> None:
        self._items: list[dict] = []
        self._current: int | None = None

    def add(self, frame: Pane, text: str = "") -> None:
        self._items.append({"frame": frame, "text": text})
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
            item["frame"]._mapped = i == idx

    def _index(self, tab_id) -> int:
        if isinstance(tab_id, Pane):
            for i, item in enumerate(self._items):
                if item["frame"] is tab_id:
                    return i
            raise KeyError(tab_id)
        return int(tab_id)


class SimpleTable:
    def __init__(self, columns: tuple[str, ...]) -> None:
        self.columns = columns
        self._head_text = {c: c for c in columns}
        self._rows: list[tuple] = []

    def heading(self, col, text=None, **_kw) -> None:
        if text is not None:
            self._head_text[col] = text

    def column(self, *_a, **_kw) -> None:
        return None

    def get_children(self) -> tuple[str, ...]:
        return tuple(str(i) for i in range(len(self._rows)))

    def delete(self, *items) -> None:
        if not items:
            return
        drop = {str(x) for x in items}
        self._rows = [row for i, row in enumerate(self._rows) if str(i) not in drop]

    def insert(self, _parent, _index, values=()) -> str:
        self._rows.append(tuple(values))
        return str(len(self._rows) - 1)


class ListBox:
    def __init__(self) -> None:
        self._items: list[str] = []
        self._sel: tuple[int, ...] = ()

    def get(self, _start=0, _end=None):
        return tuple(self._items)

    def delete(self, _start=0, _end=None) -> None:
        self._items = []

    def insert(self, _index, label: str) -> None:
        self._items.append(label)

    def curselection(self):
        return self._sel

    def bind(self, *_a, **_kw) -> None:
        return None


class DummyRoot:
    def __init__(self, hud: "TokenHud") -> None:
        self._hud = hud
        self._title = "Token HUD"

    def title(self, value: str | None = None):
        if value is not None:
            self._title = value
        return self._title

    def destroy(self) -> None:
        self._hud._destroy()

    def update(self) -> None:
        return None

    def update_idletasks(self) -> None:
        return None

    def minsize(self):
        return (WIN_MIN_W, WIN_MIN_H)

    def configure(self, **_kw) -> None:
        return None

    def after(self, ms: int, fn) -> None:
        self._hud._after(ms, fn)

    def after_cancel(self, _aid) -> None:
        return None

    def bell(self) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()

    def lift(self) -> None:
        return None

    def protocol(self, *_a, **_kw) -> None:
        return None

    def bind(self, *_a, **_kw) -> None:
        return None

    def attributes(self, *_a, **_kw) -> None:
        return None


class TokenHud:
    def __init__(self, *, demo: bool = False) -> None:
        self.demo = demo
        self.pinned: str | None = None
        self.recent: list[tuple[str, str]] = []
        self.report: dict | None = None
        self.billing_error: str | None = None
        self._fetching = False
        self._last_fetch_attempt = 0.0
        self._reset_cfg_mtime = 0.0
        self._syncing_toggles = False
        self._banner_gen = 0
        self._alive = True
        self._pause_local = False
        self._timers: list[threading.Timer] = []
        self._lock = threading.RLock()
        self._current_chat_id: str | None = None
        self._findings: list[str] = []

        self.root = DummyRoot(self)
        self.banner = Banner()
        self.banner_label = Attr("")
        self.header_plan = Attr("Loading cycle...")
        self.header_status = Attr("")
        self.header_note = Attr("")
        self.header_api = Attr("Other Models")
        self.header_auto = Attr("Cursor Models")
        self.header_api_pct = Attr("--%")
        self.header_auto_pct = Attr("--%")
        self.meter_api = ThinMeter()
        self.meter_auto = ThinMeter()
        self.meter_chat = ThinMeter()
        self.cycle_status = Attr("")
        self.spend_big = Attr("$--")
        self.kv_included = Attr("")
        self.kv_ondemand = Attr("")
        self.kv_cloud = Attr("")
        self.kv_interactive = Attr("")
        self.kv_tokens = Attr("")
        self.cycle_stats = Attr("Fetching billing cycle from cursor.com...")
        self.chats_tree = SimpleTable(("dollars", "other", "n", "hl", "name"))
        self.models_tree = SimpleTable(("dollars", "pool", "in", "out", "model"))
        self.days_tree = SimpleTable(("date", "dollars", "n"))
        self.why = TextHold()
        self.follow = Attr("Following Cursor selection")
        self.chat_model = Attr("")
        self.chat_pct = Attr("--%")
        self.chat_input = Attr("Loading...")
        self.chat_billed = Attr("")
        self.body = Attr("Loading...")
        self.listbox = ListBox()
        self.reset_status = Attr("")
        self.reset_help = Attr("")

        self.nb = SegmentedNotebook()
        self.tab_cycle = Pane()
        self.tab_chat = Pane()
        self.tab_resets = Pane()
        self.nb.add(self.tab_cycle, text="This cycle")
        self.nb.add(self.tab_chat, text="This chat")
        self.nb.add(self.tab_resets, text="Resets")

        self.reset_cards = {
            "claude": ProviderCard("Claude"),
            "codex": ProviderCard("Codex / ChatGPT"),
        }
        self.var_claude_session = Flag(True)
        self.var_claude_weekly = Flag(True)
        self.var_codex_session = Flag(True)
        self.var_codex_weekly = Flag(True)
        self.var_prewarn = Flag(True)
        self.var_sound = Flag(True)

        self.chats_tree.heading("dollars", text="$")
        self.chats_tree.heading("other", text="Other $")
        self.chats_tree.heading("n", text="n")
        self.chats_tree.heading("hl", text="cloud")
        self.chats_tree.heading("name", text="Chat / cloud agent")
        self.models_tree.heading("dollars", text="$")
        self.models_tree.heading("pool", text="pool")
        self.models_tree.heading("in", text="in")
        self.models_tree.heading("out", text="out")
        self.models_tree.heading("model", text="model")
        self.days_tree.heading("date", text="date")
        self.days_tree.heading("dollars", text="$")
        self.days_tree.heading("n", text="events")

        self._apply_resets()
        if self.demo:
            report = dict(DEMO_REPORT)
            report["fetched_at"] = time.time()
            self._on_billing_ok(report)
            self.recent = list(DEMO_RECENT)
            self.follow.configure(text=f"DEMO: {DEMO_CHAT['name']}")
            self._paint_chat(DEMO_CHAT)
            self.redraw_list(DEMO_CHAT.get("id"))
        self.refresh_local()
        self._after(200, self._billing_tick)
        self._after(400, self._reset_tick)

    def _destroy(self) -> None:
        self._alive = False
        for timer in list(self._timers):
            try:
                timer.cancel()
            except Exception:
                pass
        self._timers.clear()

    def _after(self, ms: int, fn) -> None:
        if not self._alive:
            return

        def run() -> None:
            if not self._alive:
                return
            try:
                fn()
            except Exception:
                if not self._alive:
                    return

        timer = threading.Timer(max(ms, 0) / 1000.0, run)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def clear_pin(self) -> None:
        self.pinned = None

    def on_pick(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.recent):
            self.pinned = self.recent[idx][0]

    def pin_chat(self, chat_id: str | None) -> None:
        if not chat_id:
            return
        if any(cid == chat_id for cid, _name in self.recent):
            self.pinned = chat_id

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
                with self._lock:
                    self.recent = recent_chats(con)
                    cid = followed_chat_id(con, self.pinned)
                    snap = snapshot_for(con, cid) if cid else {"error": "No active chat"}
            finally:
                con.close()
            mode = "PINNED" if self.pinned else "Cursor tab"
            name = snap.get("name") or snap.get("id") or "?"
            with self._lock:
                self.follow.configure(text=f"{mode}: {name}")
                self._paint_chat(snap)
                self.redraw_list(snap.get("id"))
        except Exception as exc:
            with self._lock:
                if self.demo:
                    self.recent = list(DEMO_RECENT)
                    self.follow.configure(text=f"DEMO: {DEMO_CHAT['name']}")
                    self._paint_chat(DEMO_CHAT)
                    self.redraw_list(DEMO_CHAT.get("id"))
                else:
                    self._paint_chat({"error": f"Read failed:\n{exc}"})
        self._after(REFRESH_MS, self.refresh_local)

    def _paint_chat(self, snap: dict) -> None:
        if snap.get("error"):
            self.chat_model.configure(text="")
            self.chat_pct.configure(text="--", fg=META)
            self.meter_chat.set(0, META)
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
        if self.demo:
            report = dict(DEMO_REPORT)
            report["fetched_at"] = time.time()
            self._on_billing_ok(report)
            return
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
        with self._lock:
            self.report = report
            self.billing_error = None
            self._fetching = False
            self.apply_billing()

    def _on_billing_err(self, err: str) -> None:
        with self._lock:
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
            )
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

        findings = r.get("findings") or []
        self._findings = list(findings)
        self.why.configure(state="normal")
        self.why.delete("1.0", "end")
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
        self._current_chat_id = current_id
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
            with self._lock:
                self._apply_resets()
                events = consume_due_events()
            if events:
                self._on_reset_buzz(events)
        except Exception as exc:
            self.reset_status.configure(text=f"Reset clock error: {exc}", fg=DANGER)
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
            fg=META,
        )
        self.reset_help.configure(
            text=(
                f"Edit {path} (America/New_York). Weekly times ship as PLACEHOLDERS -- "
                "paste real weekday+time from Settings -> Usage, then set weekly_placeholder to false. "
                "HUD reloads the JSON on save. No Anthropic/OpenAI scrape."
            )
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

    def _set_toggle(self, key: str, value: bool) -> None:
        flags = {
            "claude_session": self.var_claude_session,
            "claude_weekly": self.var_claude_weekly,
            "codex_session": self.var_codex_session,
            "codex_weekly": self.var_codex_weekly,
            "prewarn": self.var_prewarn,
            "sound": self.var_sound,
        }
        flag = flags.get(key)
        if flag is None:
            return
        flag.set(value)
        self._save_reset_toggles()

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
        except Exception:
            pass
        self._show_banner("Test buzz")

    def _on_reset_buzz(self, events: list) -> None:
        play_buzz()
        try:
            self.root.bell()
        except Exception:
            pass
        try:
            self.root.lift()
        except Exception:
            pass
        self._show_banner(" · ".join(ev.get("message") or ev.get("label") or "reset" for ev in events))

    def _show_banner(self, message: str) -> None:
        self._banner_gen += 1
        gen = self._banner_gen
        self.banner_label.configure(text=message)
        self.banner.pack()
        self._after(5000, lambda: self._hide_banner(gen))

    def _hide_banner(self, gen: int) -> None:
        if not self._alive or gen != self._banner_gen:
            return
        self.banner.pack_forget()

    def dispatch(self, payload: dict) -> None:
        op = payload.get("op")
        with self._lock:
            if op == "refresh":
                self._try_fetch(force=True)
            elif op == "follow":
                self.clear_pin()
            elif op == "pin":
                self.pin_chat(str(payload.get("chat_id") or ""))
            elif op == "mark_session":
                self._mark_session(str(payload.get("provider") or ""))
            elif op == "test_buzz":
                self._test_buzz()
            elif op == "toggle":
                self._set_toggle(str(payload.get("key") or ""), bool(payload.get("value")))

    def state(self) -> dict:
        with self._lock:
            chats = [
                dict(zip(("dollars", "other", "n", "hl", "name"), row))
                for row in self.chats_tree._rows
            ]
            models = [
                dict(zip(("dollars", "pool", "in", "out", "model"), row))
                for row in self.models_tree._rows
            ]
            days = [dict(zip(("date", "dollars", "n"), row)) for row in self.days_tree._rows]
            recent = [
                {
                    "id": cid,
                    "name": name[:48],
                    "current": cid == self._current_chat_id,
                }
                for cid, name in self.recent
            ]
            return {
                "banner": {
                    "visible": self.banner.winfo_ismapped(),
                    "text": self.banner_label.cget("text"),
                },
                "header": {
                    "plan": self.header_plan.cget("text"),
                    "status": self.header_status.cget("text"),
                    "api_pct": self.meter_api._pct,
                    "api_pct_text": self.header_api_pct.cget("text"),
                    "api_color": self.header_api_pct.cget("fg") or pct_color(self.meter_api._pct),
                    "auto_pct": self.meter_auto._pct,
                    "auto_pct_text": self.header_auto_pct.cget("text"),
                    "auto_color": self.header_auto_pct.cget("fg") or pct_color(self.meter_auto._pct),
                    "note": self.header_note.cget("text"),
                },
                "cycle": {
                    "status": self.cycle_status.cget("text"),
                    "spend": self.spend_big.cget("text"),
                    "included": self.kv_included.cget("text"),
                    "ondemand": self.kv_ondemand.cget("text"),
                    "cloud": self.kv_cloud.cget("text"),
                    "interactive": self.kv_interactive.cget("text"),
                    "tokens": self.kv_tokens.cget("text"),
                    "chats": chats,
                    "models": models,
                    "days": days,
                    "findings": list(self._findings),
                },
                "chat": {
                    "follow": self.follow.cget("text"),
                    "model": self.chat_model.cget("text"),
                    "pct": self.meter_chat._pct,
                    "pct_text": self.chat_pct.cget("text"),
                    "pct_color": self.chat_pct.cget("fg") or pct_color(self.meter_chat._pct),
                    "input": self.chat_input.cget("text"),
                    "billed": self.chat_billed.cget("text"),
                    "body": self.body.cget("text"),
                    "recent": recent,
                },
                "resets": {
                    "claude": self._reset_state("claude"),
                    "codex": self._reset_state("codex"),
                    "status": self.reset_status.cget("text"),
                    "help": self.reset_help.cget("text"),
                    "toggles": {
                        "claude_session": self.var_claude_session.get(),
                        "claude_weekly": self.var_claude_weekly.get(),
                        "codex_session": self.var_codex_session.get(),
                        "codex_weekly": self.var_codex_weekly.get(),
                        "prewarn": self.var_prewarn.get(),
                        "sound": self.var_sound.get(),
                    },
                },
            }

    def _reset_state(self, key: str) -> dict:
        card = self.reset_cards[key]
        return {
            "title": card.title,
            "summary": card.cget("text"),
            "pill": card.pill.cget("text"),
            "pill_kind": card.pill.cget("kind"),
            "session": dict(card.session),
            "weekly": dict(card.weekly),
        }

    def run(self) -> None:
        try:
            while self._alive:
                time.sleep(0.25)
        except KeyboardInterrupt:
            self._destroy()


class HudHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a) -> None:
        return

    def _hud(self) -> TokenHud:
        return self.server.hud  # type: ignore[attr-defined]

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            body = json.dumps(self._hud().state()).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/tokens.css":
            self._send(200, "text/css; charset=utf-8", css_root().encode("utf-8"))
            return
        if path == "/favicon.ico":
            self._send(204, "text/plain", b"")
            return
        spec = STATIC.get(path)
        if not spec:
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        name, ctype = spec
        target = (HUD_DIR / name).resolve()
        if not str(target).startswith(str(HUD_DIR.resolve())) or not target.is_file():
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        self._send(200, ctype, target.read_bytes())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/action":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, "application/json; charset=utf-8", b'{"error":"bad json"}')
            return
        self._hud().dispatch(payload)
        body = json.dumps(self._hud().state()).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)


class HudServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, hud: TokenHud) -> None:
        self.hud = hud
        super().__init__(addr, HudHandler)


def serve_hud(hud: TokenHud, host: str = "127.0.0.1", port: int | None = None) -> HudServer:
    server = HudServer((host, hud_port() if port is None else port), hud)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def find_browser() -> list[str] | None:
    override = os.environ.get("TOKEN_HUD_BROWSER")
    if override:
        return [override]
    candidates: list[Path] = []
    if sys.platform == "win32":
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates.extend(
            [
                pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                pf / "Google" / "Chrome" / "Application" / "chrome.exe",
                local / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
        )
        names = ("msedge", "chrome")
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")
    for path in candidates:
        if path and path.exists():
            return [str(path)]
    for name in names:
        hit = shutil.which(name)
        if hit:
            return [hit]
    return None


def launch_app_window(url: str) -> subprocess.Popen | None:
    cmd = find_browser()
    if not cmd:
        return None
    profile = Path(tempfile.gettempdir()) / "token-hud-chrome-profile"
    args = cmd + [
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={WIN_W},{WIN_H}",
        "--window-position=40,60",
        "--class=TokenHUD",
    ]
    try:
        return subprocess.Popen(args)
    except OSError:
        return None


def pin_topmost_windows(title: str = "Token HUD") -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title.lower() in buf.value.lower():
            hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    for hwnd in hits:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    return bool(hits)


def pin_topmost_linux(title: str = "Token HUD") -> bool:
    commands = []
    if shutil.which("wmctrl"):
        commands.append(["wmctrl", "-r", title, "-b", "add,above"])
    if shutil.which("xdotool"):
        commands.append(["xdotool", "search", "--name", title, "windowstayontop", "1"])
    ok = False
    for cmd in commands:
        try:
            subprocess.run(cmd, check=False, timeout=2, capture_output=True)
            ok = True
        except Exception:
            pass
    return ok


def pin_topmost_loop(proc: subprocess.Popen, title: str = "Token HUD") -> None:
    deadline = time.time() + 20
    while proc.poll() is None:
        if sys.platform == "win32":
            pin_topmost_windows(title)
        else:
            pin_topmost_linux(title)
        time.sleep(1.2 if time.time() < deadline else 8)


def try_pywebview(url: str) -> bool:
    try:
        import webview
    except ImportError:
        return False
    webview.create_window(
        "Token HUD",
        url,
        width=WIN_W,
        height=WIN_H,
        x=40,
        y=60,
        on_top=True,
        text_select=True,
    )
    webview.start()
    return True


def open_hud_window(url: str) -> None:
    if try_pywebview(url):
        return
    proc = launch_app_window(url)
    if proc is not None:
        pin = threading.Thread(target=pin_topmost_loop, args=(proc,), daemon=True)
        pin.start()
        proc.wait()
        return
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass
    sys.stderr.write(
        f"Token HUD is serving {url}\n"
        "Install Edge/Chrome, or `pip install pywebview`, for an app window.\n"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return


def run_hud(*, demo: bool = False, no_window: bool = False) -> int:
    hud = TokenHud(demo=demo)
    try:
        server = serve_hud(hud)
    except OSError:
        hud._destroy()
        return 0
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    try:
        if no_window:
            sys.stdout.write(url + "\n")
            sys.stdout.flush()
            hud.run()
        else:
            open_hud_window(url)
    finally:
        hud._destroy()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    if "--test-buzz" in sys.argv:
        sys.stdout.write(f"test buzz ({play_buzz()})\n")
        sys.exit(0)
    sys.exit(
        run_hud(
            demo="--demo" in sys.argv,
            no_window="--no-window" in sys.argv,
        )
    )
