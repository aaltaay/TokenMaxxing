"""Claude / Codex session and weekly reset clocks (stdlib only).

These meters are independent of Cursor's Other-Models billing-cycle %.
Schedule is config-driven: no scraping of Anthropic or OpenAI sites.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

FIRE_GRACE_S = 120
CONFIG_NAME = "reset-schedule.json"
STATE_NAME = "reset-buzz-state.json"
PROVIDER_IDS = ("claude", "codex")
ET_NAME = "America/New_York"

_WEEKDAYS = {
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

_ET_STD = timezone(timedelta(hours=-5), "EST")
_ET_DST = timezone(timedelta(hours=-4), "EDT")

DEFAULT_CONFIG: dict[str, Any] = {
    "_comment": (
        "Claude and Codex/ChatGPT provider resets -- NOT Cursor Other-Models cycle %. "
        "Times use America/New_York. Weekly weekday + time are placeholders: paste the "
        "real values from Settings -> Usage. session_anchor is ISO-8601 (or click "
        "Mark session started now in the HUD)."
    ),
    "pre_warn_minutes": 2,
    "pre_warn_enabled": True,
    "timezone": ET_NAME,
    "providers": {
        "claude": {
            "label": "Claude",
            "enabled": True,
            "session_hours": 5,
            "session_enabled": True,
            "session_mode": "rolling",
            "session_anchor": None,
            "weekly_enabled": True,
            "weekly_weekday": "thursday",
            "weekly_time": "00:00",
            "weekly_note": "PLACEHOLDER. Paste the real weekday + time from Settings -> Usage.",
        },
        "codex": {
            "label": "Codex/ChatGPT",
            "enabled": True,
            "session_hours": 5,
            "session_enabled": True,
            "session_mode": "rolling",
            "session_anchor": None,
            "weekly_enabled": True,
            "weekly_weekday": "thursday",
            "weekly_time": "11:00",
            "weekly_note": "PLACEHOLDER. Paste the real weekday + time from Settings -> Usage.",
        },
    },
}


@dataclass(frozen=True)
class BuzzEvent:
    event_id: str
    provider: str
    kind: str
    phase: str
    fire_at: datetime
    title: str
    body: str


@dataclass(frozen=True)
class MeterView:
    provider: str
    kind: str
    label: str
    enabled: bool
    next_at: datetime | None
    remaining_s: float | None
    note: str
    last_buzz: str | None
    last_phase: str | None
    needs_anchor: bool
    session_hours: float
    weekly_placeholder: bool


def hud_dir() -> Path:
    return Path.home() / ".cursor" / "token-hud"


def config_path() -> Path:
    return hud_dir() / CONFIG_NAME


def state_path() -> Path:
    return hud_dir() / STATE_NAME


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    first = datetime(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return 1 + delta + 7 * (n - 1)


def _et_from_utc_fallback(utc: datetime) -> timezone:
    year = utc.year
    start = datetime(year, 3, _nth_weekday(year, 3, 6, 2), 7, 0, tzinfo=timezone.utc)
    end = datetime(year, 11, _nth_weekday(year, 11, 6, 1), 6, 0, tzinfo=timezone.utc)
    return _ET_DST if start <= utc < end else _ET_STD


def et_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(ET_NAME)
        except Exception:
            pass
    return _ET_STD


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = et_tz()
    if ZoneInfo is not None and isinstance(tz, ZoneInfo):
        return dt.astimezone(tz)
    utc = dt.astimezone(timezone.utc)
    return utc.astimezone(_et_from_utc_fallback(utc))


def localize_et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    tz = et_tz()
    naive = datetime(year, month, day, hour, minute)
    if ZoneInfo is not None and isinstance(tz, ZoneInfo):
        return naive.replace(tzinfo=tz)
    as_est = naive.replace(tzinfo=_ET_STD)
    return naive.replace(tzinfo=_et_from_utc_fallback(as_est.astimezone(timezone.utc)))


def parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo else dt.replace(tzinfo=et_tz())
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz())
    return dt


def parse_hhmm(value: Any) -> tuple[int, int]:
    text = str(value or "00:00").strip().upper()
    am = text.endswith("AM")
    pm = text.endswith("PM")
    if am or pm:
        text = text[:-2].strip()
    parts = text.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return 0, 0
    if pm and hour < 12:
        hour += 12
    if am and hour == 12:
        hour = 0
    return hour % 24, max(0, min(59, minute))


def parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        return value % 7
    return _WEEKDAYS.get(str(value or "thursday").strip().lower(), 3)


def fmt_et(dt: datetime | None) -> str:
    if dt is None:
        return "--"
    local = to_et(dt)
    text = local.strftime("%a %b %d %I:%M %p ET")
    return text.replace(" 0", " ")


def fmt_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "mark session"
    secs = max(0, int(seconds))
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    minutes, secs = divmod(secs, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def event_id(provider: str, kind: str, fire_at: datetime, phase: str) -> str:
    stamp = to_et(fire_at).strftime("%Y-%m-%dT%H:%M")
    return f"{provider}:{kind}:{phase}:{stamp}"


def _deep_merge(default: dict, override: Any) -> dict:
    if not isinstance(override, dict):
        return dict(default)
    out = dict(default)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return default_config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_config()
    merged = _deep_merge(default_config(), raw)
    return merged


def ensure_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        cfg = default_config()
        _write_json(path, cfg)
        return cfg
    return load_config()


def save_config(cfg: dict[str, Any]) -> None:
    _write_json(config_path(), cfg)


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"version": 1, "fired_ids": {}, "last_buzz": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "fired_ids": {}, "last_buzz": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "fired_ids": {}, "last_buzz": {}}
    raw.setdefault("version", 1)
    raw.setdefault("fired_ids", {})
    raw.setdefault("last_buzz", {})
    return raw


def save_state(state: dict[str, Any]) -> None:
    _write_json(state_path(), state)


def _prune_fired(state: dict[str, Any], now: datetime, keep_days: int = 21) -> None:
    cutoff = now - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for eid, when in (state.get("fired_ids") or {}).items():
        dt = parse_iso(when)
        if dt is None or dt >= cutoff:
            kept[str(eid)] = str(when)
    state["fired_ids"] = kept


def first_weekly_on_or_after(weekday: Any, hhmm: Any, after: datetime) -> datetime:
    wd = parse_weekday(weekday)
    hour, minute = parse_hhmm(hhmm)
    after_et = to_et(after)
    start_date = after_et.date()
    for add in range(0, 15):
        day = start_date + timedelta(days=add)
        if day.weekday() != wd:
            continue
        local = localize_et(day.year, day.month, day.day, hour, minute)
        if local >= after:
            return local
    day = start_date + timedelta(days=7)
    return localize_et(day.year, day.month, day.day, hour, minute)


def next_weekly_fire(weekday: Any, hhmm: Any, now: datetime, lookback_s: float = FIRE_GRACE_S) -> datetime:
    return first_weekly_on_or_after(weekday, hhmm, now - timedelta(seconds=lookback_s))


def next_session_fire(
    anchor: datetime | None,
    hours: float,
    now: datetime,
    lookback_s: float = FIRE_GRACE_S,
    mode: str = "rolling",
) -> datetime | None:
    if anchor is None or hours <= 0:
        return None
    period_s = float(hours) * 3600.0
    first = anchor + timedelta(seconds=period_s)
    threshold = now - timedelta(seconds=lookback_s)
    if mode != "rolling":
        return first if first >= threshold else None
    if first >= threshold:
        return first
    n = math.ceil((threshold - first).total_seconds() / period_s - 1e-9)
    n = max(0, int(n))
    return first + timedelta(seconds=n * period_s)


def _provider_cfg(cfg: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = cfg.get("providers") or {}
    raw = providers.get(provider) or {}
    return raw if isinstance(raw, dict) else {}


def _meter_enabled(pcfg: dict[str, Any], kind: str) -> bool:
    if not pcfg.get("enabled", True):
        return False
    key = "session_enabled" if kind == "session" else "weekly_enabled"
    return bool(pcfg.get(key, True))


def _last_buzz_text(state: dict[str, Any], provider: str, kind: str) -> tuple[str | None, str | None]:
    row = (state.get("last_buzz") or {}).get(f"{provider}:{kind}") or {}
    if not row:
        return None, None
    when = parse_iso(row.get("at"))
    phase = str(row.get("phase") or "")
    return (fmt_et(when) if when else str(row.get("at") or "")), phase or None


def meter_views(cfg: dict[str, Any], state: dict[str, Any], now: datetime | None = None) -> list[MeterView]:
    now = now or now_utc()
    views: list[MeterView] = []
    for provider in PROVIDER_IDS:
        pcfg = _provider_cfg(cfg, provider)
        label = str(pcfg.get("label") or provider)
        hours = float(pcfg.get("session_hours") or 0)
        mode = str(pcfg.get("session_mode") or "rolling")
        anchor = parse_iso(pcfg.get("session_anchor"))
        weekly_note = str(pcfg.get("weekly_note") or "")
        placeholder = "PLACEHOLDER" in weekly_note.upper()

        session_at = next_session_fire(anchor, hours, now, mode=mode)
        session_remain = (session_at - now).total_seconds() if session_at else None
        last_s, phase_s = _last_buzz_text(state, provider, "session")
        views.append(
            MeterView(
                provider=provider,
                kind="session",
                label=label,
                enabled=_meter_enabled(pcfg, "session"),
                next_at=session_at,
                remaining_s=session_remain,
                note=f"{hours:g}h {'rolling' if mode == 'rolling' else 'manual'} session",
                last_buzz=last_s,
                last_phase=phase_s,
                needs_anchor=anchor is None,
                session_hours=hours,
                weekly_placeholder=False,
            )
        )

        weekly_at = next_weekly_fire(pcfg.get("weekly_weekday"), pcfg.get("weekly_time"), now)
        weekly_remain = (weekly_at - now).total_seconds()
        last_w, phase_w = _last_buzz_text(state, provider, "weekly")
        views.append(
            MeterView(
                provider=provider,
                kind="weekly",
                label=label,
                enabled=_meter_enabled(pcfg, "weekly"),
                next_at=weekly_at,
                remaining_s=weekly_remain,
                note=weekly_note,
                last_buzz=last_w,
                last_phase=phase_w,
                needs_anchor=False,
                session_hours=hours,
                weekly_placeholder=placeholder,
            )
        )
    return views


def _event_copy(view: MeterView, phase: str) -> tuple[str, str]:
    kind_l = "session" if view.kind == "session" else "weekly"
    when = fmt_et(view.next_at)
    if phase == "prewarn":
        title = f"{view.label} {kind_l} reset soon"
        body = f"Fires {when}. New {kind_l} window is about to open."
    else:
        title = f"{view.label} {kind_l} reset"
        body = f"New {kind_l} window is open ({when})."
    return title, body


def due_events(
    cfg: dict[str, Any],
    state: dict[str, Any],
    now: datetime | None = None,
) -> list[BuzzEvent]:
    now = now or now_utc()
    fired = state.get("fired_ids") or {}
    pre_on = bool(cfg.get("pre_warn_enabled", True))
    try:
        pre_m = float(cfg.get("pre_warn_minutes") or 0)
    except (TypeError, ValueError):
        pre_m = 0.0
    out: list[BuzzEvent] = []
    for view in meter_views(cfg, state, now):
        if not view.enabled or view.next_at is None:
            continue
        remain = (view.next_at - now).total_seconds()
        fire_eid = event_id(view.provider, view.kind, view.next_at, "fire")
        if fire_eid not in fired and -FIRE_GRACE_S <= remain <= 0.5:
            title, body = _event_copy(view, "fire")
            out.append(
                BuzzEvent(fire_eid, view.provider, view.kind, "fire", view.next_at, title, body)
            )
        if pre_on and pre_m > 0 and remain > 0:
            pre_eid = event_id(view.provider, view.kind, view.next_at, "prewarn")
            window = pre_m * 60.0
            if pre_eid not in fired and remain <= window + 0.5:
                title, body = _event_copy(view, "prewarn")
                out.append(
                    BuzzEvent(pre_eid, view.provider, view.kind, "prewarn", view.next_at, title, body)
                )
    return out


def record_fired(state: dict[str, Any], event: BuzzEvent, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    state.setdefault("fired_ids", {})[event.event_id] = now.isoformat(timespec="seconds")
    state.setdefault("last_buzz", {})[f"{event.provider}:{event.kind}"] = {
        "at": now.isoformat(timespec="seconds"),
        "event_id": event.event_id,
        "phase": event.phase,
    }
    _prune_fired(state, now)
    save_state(state)
    return state


def mark_session_now(cfg: dict[str, Any], provider: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    providers = cfg.setdefault("providers", {})
    pcfg = providers.setdefault(provider, {})
    pcfg["session_anchor"] = to_et(now).isoformat(timespec="seconds")
    save_config(cfg)
    return cfg


def set_meter_enabled(cfg: dict[str, Any], provider: str, kind: str, enabled: bool) -> dict[str, Any]:
    providers = cfg.setdefault("providers", {})
    pcfg = providers.setdefault(provider, {})
    key = "session_enabled" if kind == "session" else "weekly_enabled"
    pcfg[key] = bool(enabled)
    save_config(cfg)
    return cfg


def set_pre_warn_enabled(cfg: dict[str, Any], enabled: bool) -> dict[str, Any]:
    cfg["pre_warn_enabled"] = bool(enabled)
    save_config(cfg)
    return cfg


def play_buzz(phase: str = "fire") -> None:
    """Windows: Beep + system sound. Elsewhere: terminal bell. Never blocks the caller."""

    def _run() -> None:
        if sys.platform == "win32":
            try:
                import winsound

                if phase == "prewarn":
                    winsound.Beep(880, 160)
                    time.sleep(0.05)
                    winsound.Beep(880, 160)
                else:
                    winsound.Beep(980, 180)
                    time.sleep(0.05)
                    winsound.Beep(1318, 280)
                    time.sleep(0.05)
                    winsound.Beep(980, 180)
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
                return
            except Exception:
                pass
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def test_event(phase: str = "fire") -> BuzzEvent:
    now = now_utc()
    title = "TEST BUZZ" if phase == "fire" else "TEST PRE-WARN"
    body = "Sound + banner check. Not a real Claude or Codex reset."
    return BuzzEvent("test:manual", "test", "session", phase, now, title, body)


if __name__ == "__main__":
    if "--buzz-only" in sys.argv:
        play_buzz("fire")
        time.sleep(0.9)
        sys.exit(0)
    cfg = ensure_config()
    state = load_state()
    print(f"config {config_path()}")
    print(f"state  {state_path()}")
    print(f"pre-warn {cfg.get('pre_warn_minutes')} min  enabled={cfg.get('pre_warn_enabled')}")
    for view in meter_views(cfg, state):
        nxt = fmt_et(view.next_at)
        left = fmt_countdown(view.remaining_s)
        on = "on" if view.enabled else "off"
        extra = " (needs mark)" if view.needs_anchor else ""
        print(f"{view.label:14} {view.kind:8} {on:3}  {left:>14}  next {nxt}{extra}")
