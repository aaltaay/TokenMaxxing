"""How much of the weekly quota one 5-hour session uses: measured, never assumed.

Providers report the 5-hour window and the weekly window as separate
percentages, and neither says how they relate. Both count the same usage,
so between two readings inside the same pair of windows the weekly window
moves by a steady share of what the 5-hour window moves. Summing those
moves across many readings gives that share. The sums telescope, so the
rounding of each reading cancels instead of piling up, and only readings
taken inside unchanged windows count, so a reset never reads as usage.
"""
import json
import math
import threading
from cursor_usage import hud_dir

SESSION_MINUTES = 300
WEEK_MINUTES = 10080
# Percentage points of 5-hour movement, and of weekly movement, needed
# before a share is reported. Below that, one point of rounding is too
# large a part of the answer.
MIN_SESSION = 25.0
MIN_WEEKLY = 3.0
# Plenty of 5-hour movement with no weekly movement means the two windows
# do not count the same usage (a model-scoped weekly cap, say).
UNRELATED_SESSION = 60.0
# Weekly cycles kept per pair. The share uses all of them, so a change in
# plan limits or in model mix ages out within a few weeks.
KEEP_CYCLES = 3
# Reset times can jitter by a fraction of a second between readings.
SAME_RESET = 180

_lock = threading.Lock()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same(a, b):
    return _number(a) and _number(b) and abs(a - b) <= SAME_RESET


def _bucket(window_id):
    text = str(window_id)
    return text.split(':', 1)[0] if ':' in text else ''


def pairs(windows):
    """(5-hour, weekly) windows that count the same usage: same quota bucket."""
    sessions = [w for w in windows if w.get('window_minutes') == SESSION_MINUTES]
    weeks = [w for w in windows if w.get('window_minutes') == WEEK_MINUTES]
    return [(s, w) for s in sessions for w in weeks if _bucket(s.get('id')) == _bucket(w.get('id'))]


def observe(state, provider_id, windows, at):
    """Fold one fresh reading into the ledger."""
    entry = state.setdefault(provider_id, {})
    if _number(entry.get('at')) and _number(at) and at <= entry['at']:
        return  # a held reading, already counted
    last = entry.get('last') if isinstance(entry.get('last'), dict) else {}
    ledger = entry.setdefault('pairs', {})
    for session, week in pairs(windows):
        before_s, before_w = last.get(session['id']), last.get(week['id'])
        if not before_s or not before_w:
            continue
        if not (_same(before_s[1], session.get('resets_at')) and _same(before_w[1], week.get('resets_at'))):
            continue
        moved_s = session['used_percent'] - before_s[0]
        moved_w = week['used_percent'] - before_w[0]
        if moved_s < 0 or moved_w < 0 or moved_s + moved_w == 0:
            continue
        cycles = ledger.setdefault(f"{session['id']}|{week['id']}", [])
        cycle = next((c for c in cycles if _same(c.get('week'), week.get('resets_at'))), None)
        if cycle is None:
            cycle = {'week': week.get('resets_at'), 'session': 0.0, 'weekly': 0.0}
            cycles.append(cycle)
            cycles.sort(key=lambda c: c['week'])
            del cycles[:-KEEP_CYCLES]
        cycle['session'] += moved_s
        cycle['weekly'] += moved_w
    entry['last'] = {w['id']: [w['used_percent'], w.get('resets_at')] for w in windows
                     if _number(w.get('used_percent'))}
    if _number(at):
        entry['at'] = at


def estimates(state, provider_id, windows):
    """What the ledger can say about each pair right now."""
    ledger = (state.get(provider_id) or {}).get('pairs') or {}
    result = []
    for session, week in pairs(windows):
        cycles = ledger.get(f"{session['id']}|{week['id']}") or []
        moved_s = sum(c['session'] for c in cycles)
        moved_w = sum(c['weekly'] for c in cycles)
        if moved_s >= MIN_SESSION and moved_w >= MIN_WEEKLY:
            status = 'measured'
        elif moved_s >= UNRELATED_SESSION and moved_w < 1:
            status = 'unrelated'
        else:
            status = 'measuring'
        result.append({'session': session['id'], 'weekly': week['id'], 'status': status,
                       'share': moved_w / moved_s if status == 'measured' else None,
                       'session_points': round(moved_s, 2), 'weekly_points': round(moved_w, 2),
                       'cycles': len(cycles), 'needed_session_points': MIN_SESSION,
                       'needed_weekly_points': MIN_WEEKLY})
    return result


def _path():
    return hud_dir() / 'session-budget.json'


def attach(snapshot):
    """Record each fresh provider reading and add what it says about sessions."""
    with _lock:
        path = _path()
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        before = json.dumps(state, sort_keys=True)
        for provider in snapshot.get('providers', []):
            if provider.get('status') != 'ok' or provider.get('warning'):
                continue
            try:
                observe(state, provider['id'], provider.get('windows') or [], provider.get('fetched_at'))
            except (KeyError, TypeError, ValueError):
                continue
        for provider in snapshot.get('providers', []):
            try:
                provider['budget'] = estimates(state, provider['id'], provider.get('windows') or [])
            except (KeyError, TypeError, ValueError):
                provider['budget'] = []
        if json.dumps(state, sort_keys=True) != before:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(state), encoding='utf-8')
                temporary.replace(path)
            except OSError:
                pass
    return snapshot
