"""Persistent alerts derived only from observed provider windows."""
import json
import math
import threading
from cursor_usage import hud_dir

_lock = threading.Lock()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evaluate(snapshot, cfg, state, now):
    events = []
    tracked = state.setdefault("windows", {})
    sent = state.setdefault("sent", {})
    for provider in snapshot.get("providers", []):
        fetched = provider.get("fetched_at")
        if provider.get("status") != "ok" or not number(fetched) or not 0 <= now - fetched <= 120:
            continue
        for window in provider.get("windows", []):
            reset = window.get("resets_at")
            if not number(reset) or reset <= now or not number(window.get("used_percent")):
                continue
            key = f"{provider['id']}:{window['id']}"
            previous = tracked.get(key)
            kind = "weekly" if window.get("window_minutes") == 10080 else "session"
            enabled = cfg.get("buzz_enabled", True) and cfg.get("providers", {}).get(provider['id'], {}).get(f"{kind}_enabled", True)
            def emit(phase, boundary, message):
                eid = f"{key}:{phase}:{boundary}"
                if enabled and eid not in sent:
                    events.append(dict(id=eid, provider=provider['id'], kind=kind, phase=phase, message=message))
                    sent[eid] = now
            label = f"{provider['label']} {window['label']}"
            # Passing a timer alone is not proof. A fresh response must move
            # this same provider window into the next period after its boundary.
            if isinstance(previous, dict) and number(previous.get('reset')) and previous['reset'] <= fetched and previous['reset'] < reset:
                emit('reset', previous['reset'], f"{label} has reset. The provider reports a new usage window.")
            minutes = cfg.get('prewarn_minutes', 15)
            if cfg.get('prewarn_enabled', True) and number(minutes) and 0 < reset - now <= minutes * 60:
                left = max(1, math.ceil((reset - now) / 60))
                emit('prewarn', reset, f"{label}: provider reports reset in {left} minute{'s' if left != 1 else ''}.")
            tracked[key] = {'reset': reset, 'fetched_at': fetched}
    state['sent'] = dict(sorted(sent.items(), key=lambda item: item[1], reverse=True)[:200])
    return events


def consume(snapshot, cfg, now):
    with _lock:
        path = hud_dir() / 'verified-reset-alerts.json'
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state.get('windows'), dict) or not isinstance(state.get('sent'), dict):
                state = {}
        except (OSError, ValueError, AttributeError):
            state = {}
        before = json.dumps(state, sort_keys=True)
        events = evaluate(snapshot, cfg, state, now)
        if before != json.dumps(state, sort_keys=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state), encoding='utf-8')
            temporary.replace(path)
        return events
