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
import sys
import threading
import traceback
from datetime import datetime

import cursor_usage as cu
import reset_schedule as rs

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
                "share": (100.0 * tokens / used) if used else 0.0,
                "overhead": key in cu.OVERHEAD_IDS,
            }
        )
    return rows


def cmd_cycle(force: bool = False) -> dict:
    """Billing-cycle report from the dashboard (cached for CACHE_TTL_S)."""
    report = cu.fetch_cycle(force=force)
    return _decorate_cycle(report)


def cmd_cached_cycle() -> dict | None:
    """The cycle report already on disk, if any -- paints the UI before the fetch lands."""
    path = cu.cache_path()
    if not path.exists():
        return None
    try:
        return _decorate_cycle(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _decorate_cycle(report: dict) -> dict:
    """Add the derived numbers the UI would otherwise recompute per frame."""
    out = dict(report)
    elapsed = float(out.get("elapsed_days") or 0.0)
    spend = float(out.get("total_spend_cents") or 0.0)
    out["burn_cents_per_day"] = (spend / elapsed) if elapsed > 0.15 else 0.0
    out["cycle_days"] = elapsed + float(out.get("remain_days") or 0.0)
    out["spark"] = [
        {"date": d.get("date"), "cents": float(d.get("cents") or 0.0)}
        for d in (out.get("days") or [])
    ]
    out["burn_delta_pct"] = _burn_delta(out["spark"])
    out["meters"] = _cycle_meters(out)
    out["attention"] = _attention(out)
    return out


def _burn_delta(spark: list[dict]) -> float | None:
    """Percent change between the last 7 days of spend and the 7 before them."""
    if len(spark) < 4:
        return None
    recent = spark[-7:]
    prior = spark[-14:-7]
    if not prior:
        return None
    a = sum(d["cents"] for d in recent) / len(recent)
    b = sum(d["cents"] for d in prior) / len(prior)
    if b <= 0:
        return None
    return 100.0 * (a - b) / b


def _cycle_meters(report: dict) -> list[dict]:
    """Cursor's pools as ring bands.

    Only consumed-percentage meters belong here. Provider reset clocks measure
    elapsed time, not consumption, and the README is emphatic that the two are
    different meters -- they stay out of the dial.
    """
    meters = [
        {
            "id": "cursor.other",
            "provider": "cursor",
            "label": "Other models",
            "hint": "Claude / GPT / Gemini pool",
            "pct": float(report.get("api_pct") or 0.0),
            "kind": "pool",
        },
        {
            "id": "cursor.auto",
            "provider": "cursor",
            "label": "Cursor models",
            "hint": "Grok / Composer",
            "pct": float(report.get("auto_pct") or 0.0),
            "kind": "pool",
        },
    ]
    grok = report.get("grok_bot_pct")
    if grok is not None:
        meters.append(
            {
                "id": "cursor.grokbot",
                "provider": "cursor",
                "label": "Grok Bot",
                "hint": "separate weekly meter",
                "pct": float(grok),
                "kind": "pool",
                "reset": report.get("grok_bot_reset"),
            }
        )
    for m in meters:
        m["status"] = pct_kind(m["pct"])
    return meters


# The one action the app can actually take on the user's behalf; everything
# else an alert could "do" belongs to Cursor, so it stays advice, not a button.
DASHBOARD_LINK = {"label": "Open Cursor dashboard", "url": "https://cursor.com/dashboard"}


def _attention(report: dict) -> list[dict]:
    """What needs the user, most urgent first. Empty list means all clear."""
    items = []
    api = float(report.get("api_pct") or 0.0)
    auto = float(report.get("auto_pct") or 0.0)
    left = report.get("api_days_left")
    on_demand = bool(report.get("on_demand_enabled"))

    if api >= 100:
        items.append(
            {
                "id": "other-pool-empty",
                "severity": "maxxed",
                "tone": "celebrate",
                "provider": "cursor",
                "title": "Other models: fully maxxed",
                "detail": "Every included token in the Claude / GPT / Gemini pool is spent. "
                + (
                    "On-demand is off, so named models stop here -- Grok and Composer "
                    "keep running on the Cursor-models pool."
                    if not on_demand
                    else "On-demand is on, so further named turns bill as overage."
                ),
                "link": DASHBOARD_LINK,
            }
        )
    elif api >= 90:
        items.append(
            {
                "id": "other-pool-low",
                "severity": "now",
                "tone": "warn",
                "provider": "cursor",
                "title": f"Other models at {api:.0f}% -- nearly maxxed",
                "detail": f"{_runway_phrase(left)} Grok and Composer draw on a separate pool.",
                "link": DASHBOARD_LINK,
            }
        )
    if auto >= 100:
        items.append(
            {
                "id": "cursor-pool-maxxed",
                "severity": "maxxed",
                "tone": "celebrate",
                "provider": "cursor",
                "title": "Cursor models: fully maxxed",
                "detail": "The Grok / Composer pool is spent for this cycle too.",
                "link": DASHBOARD_LINK,
            }
        )
    elif auto >= 90:
        items.append(
            {
                "id": "cursor-pool-low",
                "severity": "now",
                "tone": "warn",
                "provider": "cursor",
                "title": f"Cursor models at {auto:.0f}%",
                "detail": "The Grok / Composer pool is nearly gone too.",
                "link": DASHBOARD_LINK,
            }
        )
    if left is not None and left < 3 and api < 100:
        items.append(
            {
                "id": "runway-short",
                "severity": "now",
                "tone": "warn",
                "provider": "cursor",
                "title": f"About {left:.1f} days of runway left",
                "detail": "At the current other-models burn rate.",
                "link": None,
            }
        )
    # Celebrations lead: reaching the ceiling is the headline, and the warnings
    # that got you there are the footnote.
    order = {"maxxed": 0, "now": 1, "soon": 2, "ok": 3}
    items.sort(key=lambda i: order.get(i["severity"], 4))
    return items


def _runway_phrase(days_left) -> str:
    if days_left is None:
        return "Not enough of this cycle has elapsed to project a burn rate."
    if days_left < 1:
        return "Under a day of runway at the current burn rate."
    return f"About {days_left:.1f} days of runway at the current burn rate."


def cmd_resets(now: str | None = None) -> dict:
    """Provider session / weekly clocks with countdowns, ready to render."""
    moment = rs.now_et(rs.parse_iso(now) if now else None)
    cfg = rs.load_config()
    rows = []
    for row in rs.snapshot_resets(moment, cfg):
        left = (row["next"] - moment).total_seconds()
        span = (row["next"] - row["prev"]).total_seconds()
        rows.append(
            {
                "provider": row["provider"],
                "kind": row["kind"],
                "label": row["label"],
                "short": row["short"],
                "enabled": row["enabled"],
                "placeholder": row["placeholder"],
                "anchored": row["anchored"],
                "error": row["error"],
                "last_buzzed": row["last_buzzed"],
                "next_iso": row["next"].isoformat(),
                "next_et": rs.fmt_et(row["next"]),
                "seconds_left": left,
                "countdown": rs.fmt_countdown(left),
                # How far through the current window we are -- a time meter,
                # deliberately not a consumption percentage.
                "elapsed_pct": max(0.0, min(100.0, 100.0 * (1.0 - left / span))) if span else 0.0,
                "status": "off" if not row["enabled"] else status_kind(left),
                "can_buzz": rs.can_buzz(row, cfg),
            }
        )
    return {
        "now_et": rs.fmt_et(moment),
        "rows": rows,
        "config_path": str(rs.config_path()),
        "buzz_enabled": bool(cfg.get("buzz_enabled", True)),
        "prewarn_enabled": bool(cfg.get("prewarn_enabled", True)),
        "prewarn_minutes": cfg.get("prewarn_minutes", 2),
    }


def cmd_due() -> dict:
    """Reset events that have come due since the last check, marking them buzzed."""
    events = rs.consume_due_events()
    return {
        "events": [
            {
                "id": e.get("id"),
                "provider": e.get("provider"),
                "kind": e.get("kind"),
                "phase": e.get("phase"),
                "message": e.get("message"),
            }
            for e in events
        ]
    }


def cmd_mark_session(provider: str) -> dict:
    rs.mark_session_started(provider)
    return cmd_resets()


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
    "hello": cmd_hello,
    "local": cmd_local,
    "cycle": cmd_cycle,
    "cached_cycle": cmd_cached_cycle,
    "resets": cmd_resets,
    "due": cmd_due,
    "mark_session": cmd_mark_session,
    "buzz": cmd_buzz,
    "set_config": cmd_set_config,
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
