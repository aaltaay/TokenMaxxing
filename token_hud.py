"""Always-on-top Cursor token meter.

Tab 1: this billing cycle (the 75% question).
Tab 2: the selected chat's context window, plus billed $ for that chat.
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

REFRESH_MS = 800
BILLING_POLL_MS = 4000
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

        self.root = tk.Tk()
        self.root.title("Cursor tokens")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)
        self.root.geometry("560x760+40+60")

        self._style()

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
        self.nb.add(self.tab_cycle, text="  This cycle  ")
        self.nb.add(self.tab_chat, text="  This chat  ")

        self._build_cycle_tab()
        self._build_chat_tab()
        self.refresh_local()
        self.root.after(200, self._billing_tick)

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

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if not claim_single_instance():
        sys.exit(0)
    try:
        TokenHud().run()
    except tk.TclError as exc:
        sys.stderr.write(f"UI failed: {exc}\n")
        sys.exit(1)
