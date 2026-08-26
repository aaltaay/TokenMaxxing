"""Always-on-top Cursor token meter.

Follows the chat selected in Cursor (cursor/glass.selectedAgent),
not whichever chat last got a write.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tkinter as tk
from pathlib import Path

def cursor_state_db() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


DB_PATH = cursor_state_db()
REFRESH_MS = 800
BG = "#1c1917"
FG = "#cecdc3"
DIM = "#878580"
ACCENT = "#3aa99f"
WARN = "#d14d41"
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


def fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "--"


def bar(pct: float, width: int = 18) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=2)
    con.execute("PRAGMA query_only=ON")
    return con


def item_text(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
    if not row or row[0] is None:
        return None
    val = row[0]
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    text = str(val).strip().strip('"').strip("'")
    return text or None


def recent_chats(con: sqlite3.Connection, limit: int = 8) -> list[tuple[str, str]]:
    rows = con.execute(
        """
        SELECT composerId, COALESCE(json_extract(value, '$.name'), '(untitled)')
        FROM composerHeaders
        WHERE COALESCE(isArchived, 0) = 0
          AND COALESCE(isSubagent, 0) = 0
          AND COALESCE(json_extract(value, '$.isDraft'), 0) = 0
        ORDER BY recency DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(r[0], str(r[1])) for r in rows]


def followed_chat_id(con: sqlite3.Connection, pinned: str | None) -> str | None:
    if pinned:
        return pinned
    for key in ("cursor/glass.selectedAgent", "cursor/glass.lastRealAgent"):
        cid = item_text(con, key)
        if cid:
            return cid
    row = con.execute(
        """
        SELECT composerId
        FROM composerHeaders
        WHERE COALESCE(isArchived, 0) = 0
          AND COALESCE(isSubagent, 0) = 0
          AND COALESCE(json_extract(value, '$.isDraft'), 0) = 0
        ORDER BY recency DESC
        LIMIT 1
        """
    ).fetchone()
    return row[0] if row else None


def snapshot_for(con: sqlite3.Connection, cid: str) -> dict:
    row = con.execute(
        """
        SELECT
          json_extract(value, '$.name'),
          json_extract(value, '$.contextTokensUsed'),
          json_extract(value, '$.contextTokenLimit'),
          json_extract(value, '$.contextUsagePercent'),
          json_extract(value, '$.promptTokenBreakdown'),
          json_extract(value, '$.modelConfig.modelName'),
          json_extract(value, '$.numSubComposers')
        FROM cursorDiskKV
        WHERE key = ?
        """,
        (f"composerData:{cid}",),
    ).fetchone()
    if not row:
        return {"error": "Chat blob missing", "id": cid}
    breakdown = {}
    if row[4]:
        try:
            breakdown = json.loads(row[4])
        except json.JSONDecodeError:
            breakdown = {}
    return {
        "id": cid,
        "name": row[0] or "(untitled)",
        "used": row[1],
        "limit": row[2],
        "pct": row[3],
        "breakdown": breakdown,
        "model": row[5] or "?",
        "num_sub": row[6] or 0,
    }


class TokenHud:
    def __init__(self) -> None:
        self.pinned: str | None = None
        self.recent: list[tuple[str, str]] = []
        self.root = tk.Tk()
        self.root.title("Cursor tokens")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(True, True)
        self.root.geometry("420x520+40+80")

        self.follow = tk.Label(self.root, text="Following Cursor selection", bg=BG, fg=ACCENT, anchor="w")
        self.follow.pack(fill="x", padx=12, pady=6)

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=12)
        tk.Button(
            btns,
            text="Follow Cursor tab",
            command=self.clear_pin,
            bg="#44403c",
            fg=FG,
            relief="flat",
        ).pack(side="left")

        self.body = tk.Label(
            self.root,
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

        tk.Label(self.root, text="Click a chat if the tab switch lags:", bg=BG, fg=DIM, anchor="w").pack(
            fill="x", padx=12
        )
        self.listbox = tk.Listbox(
            self.root,
            height=6,
            bg="#292524",
            fg=FG,
            selectbackground=ACCENT,
            activestyle="none",
            font=("Segoe UI", 9),
            relief="flat",
        )
        self.listbox.pack(fill="x", padx=12, pady=6)
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)

        tk.Label(
            self.root,
            text="Close anytime. Desktop / Start Menu: Cursor tokens. Starts with Cursor.",
            font=("Segoe UI", 8),
            bg=BG,
            fg=DIM,
            padx=12,
        ).pack(anchor="w", pady=8)
        self.refresh()

    def clear_pin(self) -> None:
        self.pinned = None

    def on_pick(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.recent):
            self.pinned = self.recent[idx][0]

    def refresh(self) -> None:
        try:
            if not DB_PATH.exists():
                raise FileNotFoundError("Cursor DB not found")
            con = connect()
            try:
                self.recent = recent_chats(con)
                cid = followed_chat_id(con, self.pinned)
                snap = snapshot_for(con, cid) if cid else {"error": "No active chat"}
            finally:
                con.close()
            mode = "PINNED" if self.pinned else "Cursor tab"
            name = snap.get("name") or snap.get("id") or "?"
            self.follow.configure(text=f"{mode}: {name}")
            self.body.configure(text=self.render(snap), fg=FG)
            self.redraw_list(snap.get("id"))
        except Exception as exc:
            self.body.configure(text=f"Read failed:\n{exc}", fg=WARN)
        self.root.after(REFRESH_MS, self.refresh)

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

    def render(self, snap: dict) -> str:
        if snap.get("error"):
            return snap["error"]
        used = snap.get("used") or 0
        limit = snap.get("limit") or 0
        pct = float(snap.get("pct") or 0)
        lines = [
            f"{snap['model']}   {pct:.0f}%  {bar(pct)}",
            "",
            f"INPUT   {fmt(used)}  /  {fmt(limit)}",
        ]
        cats = (snap.get("breakdown") or {}).get("categories") or []
        if cats:
            lines.append("")
            for cat in cats:
                label = (cat.get("label") or cat.get("id") or "?")[:22]
                tok = cat.get("estimatedTokens") or 0
                lines.append(f"  {label:<22} {fmt(tok):>8}")
        lines.append("")
        lines.append(f"Sub-agents in this chat: {snap.get('num_sub') or 0}")
        lines.append("OUTPUT billed: not stored locally")
        return "\n".join(lines)

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
