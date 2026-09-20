"""Always-on-top Cursor token meter.

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
BG = "#1c1917"
FG = "#cecdc3"
DIM = "#878580"
ACCENT = "#3aa99f"
WARN = "#d14d41"
OK = "#7cc47a"
CARD = "#292524"
LOCK_PORT = 47821
_lock_sock = None


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


def bar(pct: float, width: int = 22) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "#" * filled + "." * (width - filled)


def pct_color(pct: float) -> str:
    if pct >= 75:
        return WARN
    if pct >= 50:
        return "#e6a23c"
    return OK


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

        self.root = tk.Tk()
        self.root.title("Cursor tokens")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)
        self.root.geometry("560x760+40+60")

        self._style()

        self.banner = tk.Frame(self.root, bg=WARN)
        self.banner_label = tk.Label(
            self.banner,
            text="",
            bg=WARN,
            fg="#fff7ed",
            font=("Segoe UI", 9, "bold"),
            wraplength=520,
            justify="left",
            anchor="w",
        )
        self.banner_label.pack(fill="x", padx=12, pady=6)

        self.header = tk.Frame(self.root, bg=BG)
        self.header.pack(fill="x", padx=12, pady=(10, 4))
        self.header_plan = tk.Label(self.header, text="Loading cycle...", bg=BG, fg=ACCENT, anchor="w", font=("Segoe UI", 10, "bold"))
        self.header_plan.pack(fill="x")
        self.header_api = tk.Label(self.header, text="", bg=BG, fg=FG, anchor="w", font=("Cascadia Mono", 9))
        self.header_api.pack(fill="x")
        self.header_auto = tk.Label(self.header, text="", bg=BG, fg=FG, anchor="w", font=("Cascadia Mono", 9))
        self.header_auto.pack(fill="x")
        self.header_note = tk.Label(self.header, text="", bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 8), wraplength=520, justify="left")
        self.header_note.pack(fill="x", pady=(2, 0))

        nb_wrap = tk.Frame(self.root, bg=BG)
        nb_wrap.pack(fill="both", expand=True, padx=8, pady=4)
        self.nb = ttk.Notebook(nb_wrap)
        self.nb.pack(fill="both", expand=True)

        self.tab_cycle = tk.Frame(self.nb, bg=BG)
        self.tab_chat = tk.Frame(self.nb, bg=BG)
        self.tab_resets = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_cycle, text="  This cycle  ")
        self.nb.add(self.tab_chat, text="  This chat  ")
        self.nb.add(self.tab_resets, text="  Resets  ")

        self._build_cycle_tab()
        self._build_chat_tab()
        self._build_resets_tab()
        self.refresh_local()
        self.root.after(200, self._billing_tick)
        self.root.after(400, self._reset_tick)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=FG, padding=(12, 6), font=("Segoe UI", 9))
        style.map("TNotebook.Tab", background=[("selected", "#44403c")], foreground=[("selected", FG)])
        style.configure(
            "Dark.Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=FG,
            rowheight=22,
            font=("Segoe UI", 9),
            borderwidth=0,
        )
        style.configure("Dark.Treeview.Heading", background="#44403c", foreground=FG, relief="flat", font=("Segoe UI", 8, "bold"))
        style.map("Dark.Treeview", background=[("selected", ACCENT)], foreground=[("selected", BG)])

    def _build_cycle_tab(self) -> None:
        btn_row = tk.Frame(self.tab_cycle, bg=BG)
        btn_row.pack(fill="x", padx=8, pady=(8, 4))
        tk.Button(
            btn_row,
            text="Refresh usage",
            command=lambda: self._try_fetch(force=True),
            bg="#44403c",
            fg=FG,
            relief="flat",
        ).pack(side="left")
        self.cycle_status = tk.Label(btn_row, text="", bg=BG, fg=DIM, anchor="w")
        self.cycle_status.pack(side="left", padx=10)

        self.cycle_stats = tk.Label(
            self.tab_cycle,
            text="Fetching billing cycle from cursor.com...",
            justify="left",
            anchor="nw",
            font=("Cascadia Mono", 9),
            bg=BG,
            fg=FG,
            wraplength=520,
        )
        self.cycle_stats.pack(fill="x", padx=10, pady=4)

        tk.Label(self.tab_cycle, text="Where the dollars went (this cycle)", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=10)
        self.chats_tree = self._tree(
            self.tab_cycle,
            ("dollars", "other", "n", "hl", "name"),
            {"dollars": 72, "other": 72, "n": 44, "hl": 40, "name": 280},
            height=9,
        )
        self.chats_tree.heading("dollars", text="$")
        self.chats_tree.heading("other", text="Other $")
        self.chats_tree.heading("n", text="n")
        self.chats_tree.heading("hl", text="cloud")
        self.chats_tree.heading("name", text="Chat / cloud agent")

        tk.Label(self.tab_cycle, text="By model (Cursor dashboard aggregates)", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        self.models_tree = self._tree(
            self.tab_cycle,
            ("dollars", "pool", "in", "out", "model"),
            {"dollars": 72, "pool": 70, "in": 70, "out": 70, "model": 240},
            height=6,
        )
        self.models_tree.heading("dollars", text="$")
        self.models_tree.heading("pool", text="pool")
        self.models_tree.heading("in", text="in")
        self.models_tree.heading("out", text="out")
        self.models_tree.heading("model", text="model")

        tk.Label(self.tab_cycle, text="By day", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        self.days_tree = self._tree(
            self.tab_cycle,
            ("date", "dollars", "n"),
            {"date": 110, "dollars": 80, "n": 60},
            height=4,
        )
        self.days_tree.heading("date", text="date")
        self.days_tree.heading("dollars", text="$")
        self.days_tree.heading("n", text="events")

        tk.Label(self.tab_cycle, text="Was it used properly?", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        self.why = tk.Text(
            self.tab_cycle,
            height=8,
            bg=CARD,
            fg=FG,
            wrap="word",
            font=("Segoe UI", 9),
            relief="flat",
            padx=8,
            pady=8,
        )
        self.why.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.why.configure(state="disabled")

    def _build_chat_tab(self) -> None:
        self.follow = tk.Label(self.tab_chat, text="Following Cursor selection", bg=BG, fg=ACCENT, anchor="w")
        self.follow.pack(fill="x", padx=12, pady=6)
        btns = tk.Frame(self.tab_chat, bg=BG)
        btns.pack(fill="x", padx=12)
        tk.Button(btns, text="Follow Cursor tab", command=self.clear_pin, bg="#44403c", fg=FG, relief="flat").pack(side="left")
        self.body = tk.Label(
            self.tab_chat,
            text="Loading...",
            justify="left",
            anchor="nw",
            font=("Cascadia Mono", 10),
            bg=BG,
            fg=FG,
            padx=12,
            pady=10,
        )
        self.body.pack(fill="x", anchor="w")
        tk.Label(self.tab_chat, text="Click a chat if the tab switch lags:", bg=BG, fg=DIM, anchor="w").pack(fill="x", padx=12)
        self.listbox = tk.Listbox(
            self.tab_chat,
            height=7,
            bg=CARD,
            fg=FG,
            selectbackground=ACCENT,
            activestyle="none",
            font=("Segoe UI", 9),
            relief="flat",
        )
        self.listbox.pack(fill="x", padx=12, pady=6)
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)
        tk.Label(
            self.tab_chat,
            text="INPUT is this chat's live context window. OUTPUT billed is on This cycle.",
            font=("Segoe UI", 8),
            bg=BG,
            fg=DIM,
            padx=12,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=8)

    def _build_resets_tab(self) -> None:
        tk.Label(
            self.tab_resets,
            text="Claude / Codex provider windows -- not Cursor Other Models or billing-cycle %.",
            bg=BG,
            fg=ACCENT,
            anchor="w",
            wraplength=520,
            justify="left",
            font=("Segoe UI", 9, "bold"),
        ).pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(
            self.tab_resets,
            text=(
                f"Edit {config_path()} (America/New_York). Weekly times ship as PLACEHOLDERS -- "
                "paste real weekday+time from Settings -> Usage, then set weekly_placeholder to false. "
                "HUD reloads the JSON on save. No Anthropic/OpenAI scrape."
            ),
            bg=BG,
            fg=DIM,
            anchor="w",
            wraplength=520,
            justify="left",
            font=("Segoe UI", 8),
        ).pack(fill="x", padx=12, pady=(0, 6))

        btns = tk.Frame(self.tab_resets, bg=BG)
        btns.pack(fill="x", padx=12, pady=(0, 6))
        tk.Button(
            btns,
            text="Mark Claude session now",
            command=lambda: self._mark_session("claude"),
            bg="#44403c",
            fg=FG,
            relief="flat",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btns,
            text="Mark Codex session now",
            command=lambda: self._mark_session("codex"),
            bg="#44403c",
            fg=FG,
            relief="flat",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btns,
            text="Test buzz",
            command=self._test_buzz,
            bg="#44403c",
            fg=FG,
            relief="flat",
        ).pack(side="left")

        self.reset_cards = {}
        for key, title in (("claude", "Claude"), ("codex", "Codex / ChatGPT")):
            card = tk.Frame(self.tab_resets, bg=CARD)
            card.pack(fill="x", padx=10, pady=5)
            head = tk.Label(card, text=title, bg=CARD, fg=ACCENT, anchor="w", font=("Segoe UI", 10, "bold"))
            head.pack(fill="x", padx=10, pady=(8, 0))
            body = tk.Label(
                card,
                text="Loading...",
                bg=CARD,
                fg=FG,
                anchor="nw",
                justify="left",
                font=("Cascadia Mono", 9),
                wraplength=500,
            )
            body.pack(fill="x", padx=10, pady=(2, 8))
            self.reset_cards[key] = body

        toggles = tk.Frame(self.tab_resets, bg=BG)
        toggles.pack(fill="x", padx=12, pady=(4, 2))
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
        for i, (var, text) in enumerate(pairs):
            tk.Checkbutton(
                toggles,
                text=text,
                variable=var,
                command=self._save_reset_toggles,
                bg=BG,
                fg=FG,
                selectcolor=CARD,
                activebackground=BG,
                activeforeground=FG,
                highlightthickness=0,
                font=("Segoe UI", 8),
            ).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 12), pady=1)

        self.reset_status = tk.Label(self.tab_resets, text="", bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 8))
        self.reset_status.pack(fill="x", padx=12, pady=(6, 8))
        self._apply_resets()

    def _tree(self, parent, columns, widths, height: int) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=height, style="Dark.Treeview")
        for col, w in widths.items():
            tree.column(col, width=w, anchor="w", stretch=(col == "name" or col == "model"))
        tree.pack(fill="x", padx=10, pady=2)
        return tree

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
            self.body.configure(text=self.render_chat(snap), fg=FG)
            self.redraw_list(snap.get("id"))
        except Exception as exc:
            self.body.configure(text=f"Read failed:\n{exc}", fg=WARN)
        self.root.after(REFRESH_MS, self.refresh_local)

    def _billing_tick(self) -> None:
        self._try_fetch(force=False)
        self.root.after(BILLING_POLL_MS, self._billing_tick)

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
            self.root.after(0, lambda r=report: self._on_billing_ok(r))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._on_billing_err(str(e)))

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
            self.header_plan.configure(text="Cycle usage unavailable", fg=WARN)

    def apply_billing(self) -> None:
        r = self.report or {}
        api = float(r.get("api_pct") or 0)
        auto = float(r.get("auto_pct") or 0)
        total = float(r.get("total_pct") or 0)
        plan = r.get("plan") or "Cursor"
        self.header_plan.configure(text=f"{plan}  ·  day {r.get('elapsed_days') or 0:.1f} of ~30  ·  overall {total:.0f}%", fg=ACCENT)
        self.header_api.configure(
            text=f"OTHER MODELS  {api:5.0f}%  {bar(api)}   <-- this is the 75%",
            fg=pct_color(api),
        )
        self.header_auto.configure(
            text=f"CURSOR MODELS {auto:5.0f}%  {bar(auto)}   Grok / Composer",
            fg=pct_color(auto),
        )
        gb = r.get("grok_bot_pct")
        extra = f"Grok Bot weekly {gb:.0f}%." if gb is not None else ""
        left = r.get("api_days_left")
        left_s = f" Other-models slice lasts ~{left:.1f} more days at this rate." if left is not None else ""
        self.header_note.configure(text=f"{r.get('api_msg') or ''}  {extra}{left_s}".strip())

        age = time.time() - float(r.get("fetched_at") or time.time())
        self.cycle_status.configure(text=f"Snapshot {int(age)}s ago · {r.get('events_fetched') or 0} events")

        hl = r.get("headless") or {}
        inter = r.get("interactive") or {}
        lines = [
            f"Spend {fmt_money(r.get('total_spend_cents') or 0)}  (included {fmt_money(r.get('included_cents') or 0)} + bonus {fmt_money(r.get('bonus_cents') or 0)})",
            f"On-demand: {'on' if r.get('on_demand_enabled') else 'off (hard stop when remaining pools empty)'}",
            f"Cloud/headless {fmt_money(hl.get('cents') or 0)} in {hl.get('n') or 0} events   |  Interactive {fmt_money(inter.get('cents') or 0)} in {inter.get('n') or 0}",
            f"Tokens  in {fmt_int(r.get('agg_input'))}  out {fmt_int(r.get('agg_output'))}  cacheRead {fmt_int(r.get('agg_cache_read'))}  cacheWrite {fmt_int(r.get('agg_cache_write'))}",
        ]
        self.cycle_stats.configure(text="\n".join(lines), fg=FG)

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
        used = snap.get("used") or 0
        limit = snap.get("limit") or 0
        pct = float(snap.get("pct") or 0)
        lines = [
            f"{snap['model']}   {pct:.0f}%  {bar(pct)}",
            "",
            f"INPUT   {fmt_int(used)}  /  {fmt_int(limit)}",
        ]
        billed = billed_for_chat(self.report, snap.get("id"))
        if billed:
            lines.append(
                f"THIS CYCLE  {fmt_money(billed.get('cents') or 0)}  "
                f"(Other {fmt_money(billed.get('other_cents') or 0)})  "
                f"{billed.get('n') or 0} events"
            )
        else:
            lines.append("THIS CYCLE  no billed events matched this chat id yet")
        cats = (snap.get("breakdown") or {}).get("categories") or []
        if cats:
            lines.append("")
            for cat in cats:
                label = (cat.get("label") or cat.get("id") or "?")[:22]
                tok = cat.get("estimatedTokens") or 0
                lines.append(f"  {label:<22} {fmt_int(tok):>8}")
        tax = float(snap.get("tax_pct") or 0)
        lines.append("")
        lines.append(f"Context tax (rules/tools/skills/MCP/subagents): {tax:.0f}%")
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
        try:
            self._apply_resets()
            events = consume_due_events()
            if events:
                self._on_reset_buzz(events)
        except Exception as exc:
            self.reset_status.configure(text=f"Reset clock error: {exc}", fg=WARN)
        self.root.after(RESET_TICK_MS, self._reset_tick)

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
        by_key: dict[str, list[str]] = {"claude": [], "codex": []}
        soonest: dict[str, float] = {"claude": 10**9, "codex": 10**9}
        for row in snapshot_resets(now, cfg):
            left = (row["next"] - now).total_seconds()
            soonest[row["provider"]] = min(soonest.get(row["provider"], 10**9), left)
            flags = []
            if not row.get("enabled"):
                flags.append("off")
            if row["kind"] == "session":
                flags.append("anchored" if row.get("anchored") else "rolling midnight ET")
            if row.get("placeholder"):
                flags.append("PLACEHOLDER")
            if row.get("error"):
                flags.append(row["error"])
            last = row.get("last_buzzed") or "--"
            by_key.setdefault(row["provider"], []).extend(
                [
                    f"{row['kind'].upper():<8} {fmt_countdown(left):>8}   next {fmt_et(row['next'])}",
                    f"         {' · '.join(flags) if flags else 'ready'}",
                    f"         last buzz {last}",
                    "",
                ]
            )
        for key, label in self.reset_cards.items():
            text = "\n".join(by_key.get(key) or ["--"]).rstrip()
            left = soonest.get(key, 10**9)
            if left <= 120:
                color = WARN
            elif left <= 900:
                color = "#e6a23c"
            else:
                color = FG
            label.configure(text=text, fg=color)
        pre = cfg.get("prewarn_minutes", 2)
        self.reset_status.configure(
            text=f"Pre-warn {pre} min · config {path} · reloads on save",
            fg=DIM,
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
            self.banner.pack(fill="x", before=self.header)
        self._flash_n = 0
        self._flash_step(gen)
        self.root.after(5000, lambda: self._hide_banner(gen))

    def _flash_step(self, gen: int) -> None:
        if gen != self._banner_gen:
            return
        self._flash_n += 1
        self.root.configure(bg=WARN if self._flash_n % 2 else BG)
        self.banner.configure(bg=WARN if self._flash_n % 2 else "#9a3412")
        if self._flash_n < 6:
            self.root.after(140, lambda: self._flash_step(gen))
        else:
            self.root.configure(bg=BG)
            self.banner.configure(bg=WARN)

    def _hide_banner(self, gen: int) -> None:
        if gen != self._banner_gen:
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
