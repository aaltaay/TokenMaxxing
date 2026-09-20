"""Claude / Codex provider reset clocks (stdlib only).

Independent of Cursor billing-cycle %. No live Anthropic/OpenAI scrape.
Config: ~/.cursor/token-hud/reset-schedule.json
Last-fired ids: ~/.cursor/token-hud/reset-buzzed.json
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from cursor_usage import hud_dir

CONFIG_NAME = "reset-schedule.json"
BUZZED_NAME = "reset-buzzed.json"
GRACE_S = 90.0
PROVIDERS = ("claude", "codex")
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "notes": (
        "PLACEHOLDER weekly weekday+time. Paste the real values from Claude / ChatGPT "
        "Settings -> Usage. Times are America/New_York. Session windows are 5 hours from "
        "session_anchor (Mark session started now), or a rolling guess from midnight ET "
        "if unset. Session buzz needs an anchor. Weekly buzz needs weekly_placeholder=false. "
        "These clocks are not Cursor Other Models / billing-cycle %."
    ),
    "timezone": "America/New_York",
    "prewarn_minutes": 15,
    "prewarn_enabled": True,
    "buzz_enabled": True,
    "providers": {
        "claude": {
            "label": "Claude",
            "session_enabled": True,
            "weekly_enabled": True,
            "session_hours": 5,
            "session_anchor": None,
            "weekly_weekday": "thursday",
            "weekly_time": "09:00",
            "weekly_placeholder": True,
        },
        "codex": {
            "label": "Codex / ChatGPT",
            "session_enabled": True,
            "weekly_enabled": True,
            "session_hours": 5,
            "session_anchor": None,
            "weekly_weekday": "thursday",
            "weekly_time": "09:00",
            "weekly_placeholder": True,
        },
    },
}


class _Eastern(tzinfo):
    """US Eastern (2007+ DST) when zoneinfo has no America/New_York data."""

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-5) + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1) if _wall_is_edt(dt.replace(tzinfo=None)) else timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc requires this timezone")
        utc_naive = dt.replace(tzinfo=None)
        # The spring transition occurs at 02:00 EST (07:00 UTC), and
        # the fall transition at 02:00 EDT (06:00 UTC). Comparing UTC
        # boundaries avoids inventing 02:00 during the repeated fall hour.
        year = utc_naive.year
        start = datetime(year, 3, _nth_weekday(year, 3, 6, 2), 7)
        end = datetime(year, 11, _nth_weekday(year, 11, 6, 1), 6)
        offset = -4 if start <= utc_naive < end else -5
        fold = int(end <= utc_naive < end + timedelta(hours=1))
        return (utc_naive + timedelta(hours=offset)).replace(tzinfo=self, fold=fold)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    first = datetime(year, month, 1)
    return 1 + (weekday - first.weekday()) % 7 + 7 * (n - 1)


def _wall_is_edt(naive: datetime) -> bool:
    y = naive.year
    start = datetime(y, 3, _nth_weekday(y, 3, 6, 2), 2, 0, 0)
    end = datetime(y, 11, _nth_weekday(y, 11, 6, 1), 2, 0, 0)
    hour = timedelta(hours=1)
    if start <= naive < start + hour:
        # For the nonexistent spring hour, fold reverses the offset choice.
        return bool(naive.fold)
    if end - hour <= naive < end:
        return not naive.fold
    return start + hour <= naive < end - hour


def et_tz() -> datetime.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:
        return _Eastern()


def now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(et_tz())
    if now.tzinfo is None:
        return now.replace(tzinfo=et_tz())
    return now.astimezone(et_tz())


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=et_tz())
    return dt


def parse_weekday(value) -> int:
    if isinstance(value, bool):
        raise ValueError("weekday cannot be bool")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"weekday out of range: {value}")
    key = str(value).strip().lower()
    if key.isdigit():
        return parse_weekday(int(key))
    if key in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[key]
    raise ValueError(f"unknown weekday: {value}")


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"time needs HH:MM: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {value}")
    return hour, minute


def fire_id(dt: datetime) -> str:
    return now_et(dt).replace(microsecond=0).isoformat()


def fmt_et(dt: datetime) -> str:
    return now_et(dt).strftime("%Y-%m-%d %H:%M ET")


def fmt_countdown(seconds: float) -> str:
    if seconds <= 0:
        return "now"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {s:02d}s"
    return f"{s}s"


def config_path() -> Path:
    return hud_dir() / CONFIG_NAME


def buzzed_path() -> Path:
    return hud_dir() / BUZZED_NAME


def _merge(default: dict, user: Any) -> dict:
    if not isinstance(user, dict):
        return deepcopy(default)
    out = deepcopy(default)
    for key, val in user.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        cfg = deepcopy(DEFAULT_CONFIG)
        _write_json(path, cfg)
        return cfg
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        bad = path.with_suffix(".bad.json")
        try:
            path.replace(bad)
        except OSError:
            pass
        cfg = deepcopy(DEFAULT_CONFIG)
        _write_json(path, cfg)
        return cfg
    return _merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict) -> None:
    _write_json(config_path(), cfg)


def load_buzzed() -> dict:
    path = buzzed_path()
    if not path.exists():
        return {"events": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": {}}
    events = data.get("events")
    if not isinstance(events, dict):
        return {"events": {}}
    return {"events": events}


def already_buzzed(event_id: str) -> bool:
    return event_id in load_buzzed()["events"]


def record_buzz(event_id: str, meta: dict | None = None) -> None:
    data = load_buzzed()
    row = dict(meta or {})
    row.setdefault("at", datetime.now(timezone.utc).isoformat())
    data["events"][event_id] = row
    items = sorted(data["events"].items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True)
    data["events"] = dict(items[:80])
    _write_json(buzzed_path(), data)


def last_buzzed_for(provider: str, kind: str) -> str | None:
    prefix = f"{provider}:{kind}:"
    best_at = None
    best_fire = None
    for eid, meta in load_buzzed()["events"].items():
        if not eid.startswith(prefix) or ":prewarn:" in eid:
            continue
        at = str(meta.get("at") or "")
        if best_at is None or at > best_at:
            best_at = at
            best_fire = eid.rsplit(":", 1)[-1] if ":" in eid else at
    if best_at is None:
        return None
    try:
        return fmt_et(parse_iso(best_fire or best_at))
    except Exception:
        return best_at[:19]


def session_hours(prov: dict) -> float:
    try:
        hours = float(prov.get("session_hours") or 5)
    except (TypeError, ValueError):
        hours = 5.0
    return hours if hours > 0 else 5.0


def session_bounds(now: datetime, hours: float, anchor: str | None) -> tuple[datetime, datetime, bool]:
    now = now_et(now)
    period = timedelta(hours=hours)
    if anchor:
        start = now_et(parse_iso(anchor))
        if now < start:
            return start - period, start, True
        elapsed = (now - start).total_seconds()
        n = int(elapsed // period.total_seconds())
        window_start = start + n * period
        return window_start, window_start + period, True
    local = now
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (local - midnight).total_seconds()
    period_s = period.total_seconds()
    n = int(elapsed // period_s)
    window_start = midnight + timedelta(seconds=n * period_s)
    return window_start, window_start + timedelta(seconds=period_s), False


def weekly_bounds(now: datetime, weekday, time_str: str) -> tuple[datetime, datetime]:
    now = now_et(now)
    wd = parse_weekday(weekday)
    hour, minute = parse_hhmm(time_str)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (wd - now.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate - timedelta(days=7), candidate


def provider_cfg(cfg: dict, key: str) -> dict:
    block = (cfg.get("providers") or {}).get(key) or {}
    return _merge(DEFAULT_CONFIG["providers"][key], block) if key in DEFAULT_CONFIG["providers"] else block


def snapshot_resets(now: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    cfg = cfg if cfg is not None else load_config()
    now = now_et(now)
    rows: list[dict] = []
    for key in PROVIDERS:
        prov = provider_cfg(cfg, key)
        label = str(prov.get("label") or key)
        hours = session_hours(prov)
        anchor = prov.get("session_anchor") or None
        if anchor is not None:
            anchor = str(anchor).strip() or None
        try:
            start, nxt, anchored = session_bounds(now, hours, anchor)
            session_err = None
        except Exception as exc:
            start, nxt, anchored = now, now + timedelta(hours=hours), bool(anchor)
            session_err = str(exc)
        rows.append(
            {
                "provider": key,
                "kind": "session",
                "label": f"{label} session",
                "short": label,
                "enabled": bool(prov.get("session_enabled", True)),
                "placeholder": False,
                "anchored": anchored,
                "prev": start,
                "next": nxt,
                "hours": hours,
                "error": session_err,
                "last_buzzed": last_buzzed_for(key, "session"),
            }
        )
        placeholder = bool(prov.get("weekly_placeholder", True))
        try:
            prev_w, next_w = weekly_bounds(now, prov.get("weekly_weekday"), prov.get("weekly_time") or "09:00")
            weekly_err = None
        except Exception as exc:
            prev_w, next_w = now - timedelta(days=7), now + timedelta(days=7)
            weekly_err = str(exc)
        rows.append(
            {
                "provider": key,
                "kind": "weekly",
                "label": f"{label} weekly",
                "short": label,
                "enabled": bool(prov.get("weekly_enabled", True)),
                "placeholder": placeholder,
                "anchored": True,
                "prev": prev_w,
                "next": next_w,
                "hours": 24 * 7,
                "error": weekly_err,
                "last_buzzed": last_buzzed_for(key, "weekly"),
            }
        )
    return rows


def can_buzz(row: dict, cfg: dict) -> bool:
    if not cfg.get("buzz_enabled", True):
        return False
    if not row.get("enabled"):
        return False
    if row.get("error"):
        return False
    if row["kind"] == "session" and not row.get("anchored"):
        return False
    if row["kind"] == "weekly" and row.get("placeholder"):
        return False
    return True


def due_events(now: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    cfg = cfg if cfg is not None else load_config()
    now = now_et(now)
    try:
        prewarn_m = float(cfg.get("prewarn_minutes") if cfg.get("prewarn_minutes") is not None else 2)
    except (TypeError, ValueError):
        prewarn_m = 2.0
    prewarn_s = max(0.0, prewarn_m * 60.0)
    out: list[dict] = []
    for row in snapshot_resets(now, cfg):
        if not can_buzz(row, cfg):
            continue
        prev = row["prev"]
        nxt = row["next"]
        if row["kind"] == "session":
            # User mark is window start, not a reset. Only n>=1 boundaries buzz.
            fire = prev
            if row.get("anchored"):
                prov = provider_cfg(cfg, row["provider"])
                anchor_raw = prov.get("session_anchor")
                try:
                    anchor_dt = now_et(parse_iso(str(anchor_raw))) if anchor_raw else None
                except Exception:
                    anchor_dt = None
                if anchor_dt is not None and abs((fire - anchor_dt).total_seconds()) < 1:
                    fire = None
            if fire is not None and 0 <= (now - fire).total_seconds() < GRACE_S:
                eid = f"{row['provider']}:{row['kind']}:{fire_id(fire)}"
                out.append(_event(row, eid, "reset", fire, f"{row['label']} reset"))
        else:
            fire = prev
            if 0 <= (now - fire).total_seconds() < GRACE_S:
                eid = f"{row['provider']}:{row['kind']}:{fire_id(fire)}"
                out.append(_event(row, eid, "reset", fire, f"{row['label']} reset"))
        if cfg.get("prewarn_enabled", True) and prewarn_s > 0:
            left = (nxt - now).total_seconds()
            if 0 < left <= prewarn_s:
                eid = f"{row['provider']}:{row['kind']}:prewarn:{fire_id(nxt)}"
                out.append(_event(row, eid, "prewarn", nxt, f"{row['label']} reset in {fmt_countdown(left)}"))
    return out


def _event(row: dict, eid: str, phase: str, fire: datetime, message: str) -> dict:
    return {
        "id": eid,
        "provider": row["provider"],
        "kind": row["kind"],
        "phase": phase,
        "label": row["label"],
        "message": message,
        "fire": fire,
    }


def consume_due_events(now: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    fresh = []
    for ev in due_events(now, cfg):
        if already_buzzed(ev["id"]):
            continue
        record_buzz(
            ev["id"],
            {
                "provider": ev["provider"],
                "kind": ev["kind"],
                "phase": ev["phase"],
                "message": ev["message"],
                "fire": fire_id(ev["fire"]),
            },
        )
        fresh.append(ev)
    return fresh


def mark_session_started(provider: str, when: datetime | None = None) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    cfg = load_config()
    stamp = now_et(when).replace(microsecond=0).isoformat()
    cfg.setdefault("providers", {}).setdefault(provider, {})
    cfg["providers"][provider]["session_anchor"] = stamp
    save_config(cfg)
    return cfg


def play_buzz() -> str:
    if sys.platform == "win32":
        try:
            import winsound

            try:
                winsound.Beep(880, 160)
                winsound.Beep(1320, 240)
                return "winsound.Beep"
            except RuntimeError:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                return "winsound.MessageBeep"
        except Exception:
            pass
    sys.stdout.write("\a")
    sys.stdout.flush()
    return "bell"


def render_row(row: dict, now: datetime | None = None) -> str:
    now = now_et(now)
    left = (row["next"] - now).total_seconds()
    bits = [
        f"{row['label']:<22} {fmt_countdown(left):>8}   next {fmt_et(row['next'])}",
    ]
    flags = []
    if not row.get("enabled"):
        flags.append("disabled")
    if row["kind"] == "session":
        flags.append("anchored" if row.get("anchored") else "rolling midnight ET (mark session to sync + buzz)")
    if row.get("placeholder"):
        flags.append("PLACEHOLDER weekly time -- set weekly_placeholder=false after pasting Usage time")
    if row.get("error"):
        flags.append(f"error {row['error']}")
    last = row.get("last_buzzed") or "--"
    flags.append(f"last buzz {last}")
    bits.append("  " + " · ".join(flags))
    return "\n".join(bits)


def render_status(now: datetime | None = None, cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    now = now_et(now)
    lines = [
        f"config {config_path()}",
        f"now    {fmt_et(now)}",
        "not Cursor billing-cycle %",
        "",
    ]
    for row in snapshot_resets(now, cfg):
        lines.append(render_row(row, now))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--test-buzz" in args:
        kind = play_buzz()
        sys.stdout.write(f"test buzz ({kind})\n")
        return 0
    if "--mark-session" in args:
        idx = args.index("--mark-session")
        if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
            sys.stderr.write("usage: reset_schedule.py --mark-session claude|codex\n")
            return 2
        mark_session_started(args[idx + 1])
        sys.stdout.write(render_status())
        return 0
    load_config()
    sys.stdout.write(render_status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
