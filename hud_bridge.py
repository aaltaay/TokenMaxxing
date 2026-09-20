"""NDJSON bridge between the Electron UI and the Python data engine (stdlib only).

The UI never touches ``state.vscdb`` or the dashboard itself. It spawns this
module once and speaks newline-delimited JSON over stdin/stdout::

    -> {"id": 1, "cmd": "local"}
    <- {"id": 1, "ok": true, "data": {...}}

Every request is served on its own thread, so a 90-second cycle fetch never
blocks the one-second reset tick. stdout carries protocol frames only --
anything diagnostic goes to stderr.
"""
from __future__ import annotations

import json
import math
import time
import sys
import threading
import traceback
from datetime import datetime

import cursor_usage as cu
import reset_schedule as rs
import provider_usage as pu
import reset_alerts
import session_usage

PROTOCOL_VERSION = 1

# Countdown thresholds, shared with the renderer so one clock reads the same
# everywhere. Ported from the Tkinter HUD's status_kind().
NOW_S = 120
SOON_S = 900


def status_kind(left: float) -> str:
    """Urgency bucket for a countdown, in seconds remaining."""
    if left <= NOW_S:
        return "now"
    if left <= SOON_S:
        return "soon"
    return "ok"


def pct_kind(pct: float) -> str:
    """State of a consumed-percentage meter.

    ``maxxed`` is deliberately its own state rather than the top of a warning
    ramp: hitting the ceiling is the thing this tool exists to show, and the UI
    renders it as an achievement in the provider's own colour, not an alarm.
    """
    if pct >= 100:
        return "maxxed"
    if pct >= 75:
        return "now"
    if pct >= 50:
        return "soon"
    return "ok"


# ── commands ──────────────────────────────────────────────────────────────

def cmd_sessions(provider: str = 'codex', pinned: str | None = None,
                 follow: str = 'latest', active_title: str | None = None) -> dict:
    result = session_usage.get_sessions(provider, pinned, follow, active_title)
    if provider == 'cursor' and result.get('chat'):
        result['chat']['cat_rows'] = _chat_categories(result['chat'])
    return result


def cmd_local(pinned: str | None = None, chat_limit: int = 12) -> dict:
    """Everything readable from the local Cursor DB: context window + chat list."""
    db = cu.cursor_state_db()
    if not db.exists():
        return {"available": False, "reason": "Cursor DB not found", "chats": [], "chat": None}
    con = cu.connect(db)
    try:
        chats = [{"id": cid, "name": name} for cid, name in cu.recent_chats(con, chat_limit)]
        cid = cu.followed_chat_id(con, pinned)
        chat = cu.snapshot_for(con, cid) if cid else None
    finally:
        con.close()
    if chat and not chat.get("error"):
        chat["cat_rows"] = _chat_categories(chat)
    return {"available": True, "pinned": pinned, "chats": chats, "chat": chat}


# Cursor's category ids, written the way a person would.
CATEGORY_LABELS = {
    "system_prompt": "System prompt",
    "tools": "Tools",
    "rules": "Rules",
    "skills": "Skills",
    "mcp": "MCP",
    "subagents": "Subagents",
    "conversation": "Conversation",
}


def category_label(key: str) -> str:
    """Human label for a context category, falling back to a tidied id."""
    return CATEGORY_LABELS.get(key) or str(key).replace("_", " ").capitalize()


def _chat_categories(chat: dict) -> list[dict]:
    """Context breakdown as ordered rows, overhead first, with shares."""
    cats = chat.get("cats") or {}
    used = chat.get("used") or 0
    order = list(cu.OVERHEAD_IDS) + [k for k in cats if k not in cu.OVERHEAD_IDS]
    rows = []
    for key in order:
        tokens = cats.get(key)
        if not tokens:
            continue
        rows.append(
            {
                "id": key,
                "label": category_label(key),
                "tokens": tokens,
                "share": (100.0 * tokens / used) if used else None,
                "overhead": key in cu.OVERHEAD_IDS,
            }
        )
    return rows


def cmd_cycle(force: bool = False) -> dict:
    """Billing-cycle report from the dashboard (cached for CACHE_TTL_S)."""
    report = cu.fetch_cycle(force=force)
    return _decorate_cycle(report)


def cmd_cached_cycle() -> dict | None:
    """Paint only a fresh snapshot validated against the current Cursor sign-in."""
    report = cu.read_cached_cycle()
    return _decorate_cycle(report) if report is not None else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _decorate_cycle(report: dict) -> dict:
    """Keep unavailable metrics null; derive only arithmetic from known inputs."""
    out = dict(report)
    elapsed = _number(out.get("elapsed_days"))
    remain = _number(out.get("remain_days"))
    spend = _number(out.get("total_spend_cents"))
    out["burn_cents_per_day"] = spend / elapsed if spend is not None and elapsed is not None and elapsed > 0.15 else None
    out["cycle_days"] = elapsed + remain if elapsed is not None and remain is not None else None
    out["spark"] = []
    out["burn_delta_pct"] = None
    out["api_days_left"] = None
    out["meters"] = _cycle_meters(out)
    out["attention"] = _attention(out)
    return out


def _cycle_meters(report: dict) -> list[dict]:
    meters = []
    for key, ident, label in (("api_pct", "other", "Other models"),
                              ("auto_pct", "auto", "Cursor models"),
                              ("grok_bot_pct", "grokbot", "Grok Bot")):
        value = _number(report.get(key))
        meters.append({
            "id": f"cursor.{ident}", "provider": "cursor", "label": label,
            "hint": "Reported by Cursor dashboard" if value is not None else "Not returned by Cursor",
            "pct": value, "kind": "pool", "status": pct_kind(value) if value is not None else "off",
            "reset": report.get("grok_bot_reset") if ident == "grokbot" else report.get("cycle_end"),
        })
    return meters


# The one action the app can actually take on the user's behalf; everything
# else an alert could "do" belongs to Cursor, so it stays advice, not a button.
DASHBOARD_LINK = {"label": "Open Cursor dashboard", "url": "https://cursor.com/dashboard"}


def _attention(report: dict) -> list[dict]:
    """Describe reported values without inferring model access or future cost."""
    items = []
    for key, ident, label in (("api_pct", "other", "Other models"), ("auto_pct", "cursor", "Cursor models")):
        value = _number(report.get(key))
        if value is None or value < 90:
            continue
        detail = "Cursor dashboard reports this share of the pool used."
        demand = report.get("on_demand_enabled")
        if isinstance(demand, bool):
            detail += f" On-demand is reported as {'enabled' if demand else 'disabled'}."
        items.append({"id": f"{ident}-pool-high", "severity": "now", "tone": "warn", "provider": "cursor",
                      "title": f"{label}: {value:.0f}% used", "detail": detail, "link": DASHBOARD_LINK})
    return items


def cmd_providers(force: bool = False) -> dict:
    return pu.get_provider_usage(force=force)


def cmd_resets(now: str | None = None) -> dict:
    """Only expose timestamps returned by a provider, never manual schedule math."""
    snapshot = pu.peek_provider_usage() or {"providers": []}
    moment = rs.parse_iso(now).timestamp() if now else time.time()
    rows = []
    for provider in snapshot.get("providers", []):
        if provider.get("status") != "ok":
            continue
        for window in provider.get("windows", []):
            reset = _number(window.get("resets_at"))
            if reset is None:
                continue
            rows.append({"provider": provider["id"], "label": window["label"], "source": provider.get("source"),
                         "resets_at": reset, "seconds_left": reset - moment,
                         "countdown": rs.fmt_countdown(reset - moment) if reset > moment else "Awaiting provider update"})
    return {"rows": rows, "providers": snapshot.get("providers", []), "config_path": str(rs.config_path())}


def cmd_due() -> dict:
    """Warn before real boundaries and confirm transitions from fresh responses."""
    return {"events": reset_alerts.consume(pu.peek_provider_usage() or {"providers": []}, rs.load_config(), time.time())}


def cmd_alert_settings() -> dict:
    cfg = rs.load_config()
    return {key: cfg.get(key, default) for key, default in
            (("buzz_enabled", True), ("prewarn_enabled", True), ("prewarn_minutes", 15), ("sms_to", ""))}


def cmd_buzz() -> dict:
    return {"kind": rs.play_buzz()}


def cmd_set_config(patch: dict) -> dict:
    cfg = rs.load_config()
    cfg.update(patch or {})
    rs.save_config(cfg)
    return cmd_resets()


def cmd_paths() -> dict:
    return {
        "state_db": str(cu.cursor_state_db()),
        "state_db_exists": cu.cursor_state_db().exists(),
        "hud_dir": str(cu.hud_dir()),
        "config": str(rs.config_path()),
        "cache": str(cu.cache_path()),
    }


def cmd_hello() -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "paths": cmd_paths(),
    }


COMMANDS = {
    "sessions": cmd_sessions,
    "hello": cmd_hello,
    "local": cmd_local,
    "cycle": cmd_cycle,
    "cached_cycle": cmd_cached_cycle,
    "resets": cmd_resets,
    "providers": cmd_providers,
    "due": cmd_due,
    "buzz": cmd_buzz,
    "set_config": cmd_set_config,
    "alert_settings": cmd_alert_settings,
    "paths": cmd_paths,
}


# ── transport ─────────────────────────────────────────────────────────────

class Bridge:
    """Serves NDJSON requests, one worker thread each, one writer lock."""

    def __init__(self, out=None, commands: dict | None = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.commands = commands if commands is not None else COMMANDS
        self._write_lock = threading.Lock()

    def emit(self, frame: dict) -> None:
        line = json.dumps(frame, default=_json_default)
        with self._write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def dispatch(self, frame: dict) -> None:
        rid = frame.get("id")
        name = frame.get("cmd")
        fn = self.commands.get(name)
        if fn is None:
            self.emit({"id": rid, "ok": False, "error": {"kind": "unknown_command", "message": str(name)}})
            return
        try:
            data = fn(**(frame.get("args") or {}))
            self.emit({"id": rid, "ok": True, "data": data})
        except Exception as exc:  # surfaced in the UI, never fatal to the process
            traceback.print_exc(file=sys.stderr)
            self.emit(
                {
                    "id": rid,
                    "ok": False,
                    "error": {"kind": type(exc).__name__, "message": str(exc) or type(exc).__name__},
                }
            )

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            self.emit({"id": None, "ok": False, "error": {"kind": "bad_frame", "message": str(exc)}})
            return
        threading.Thread(target=self.dispatch, args=(frame,), daemon=True).start()

    def serve(self, stream=None) -> int:
        stream = stream if stream is not None else sys.stdin
        self.emit({"id": None, "ok": True, "event": "ready", "data": cmd_hello()})
        for line in stream:
            self.handle_line(line)
        return 0


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--once" in args:
        # One-shot mode for scripting and smoke tests: emit a full snapshot.
        idx = args.index("--once")
        name = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("-") else "hello"
        fn = COMMANDS.get(name)
        if fn is None:
            sys.stderr.write(f"unknown command: {name}\n")
            return 2
        json.dump(fn(), sys.stdout, default=_json_default, indent=2)
        sys.stdout.write("\n")
        return 0
    return Bridge().serve()


if __name__ == "__main__":
    raise SystemExit(main())
