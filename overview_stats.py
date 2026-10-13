"""Range totals for the Overview: API-price value, tokens, how long agents
ran, how long you were at the keyboard, what the agents did, Cursor's value
and the time AI apps had the focus. Every figure is a fact the ledger holds
(see ledger.py); nothing is guessed, and nothing here reads a log."""
from __future__ import annotations

import bisect
import collections
import time
from datetime import datetime

import ledger
# The reading rules, and the pricing and log folders they use, where callers
# and tests have always found them.
from ledger import IDLE_GAP, THINK_CAP, TOOL_GROUPS, _intervals, _typed, tool_group  # noqa: F401
import session_cost as sc  # noqa: F401
import session_usage as su  # noqa: F401

# Prompts closer together than this are one sitting at the keyboard.
SITTING_GAP = 30 * 60
# Credit for reading the reply that ended a sitting.
SITTING_PAD = 5 * 60
PROVIDERS = ('claude', 'codex')
DAY_PROVIDERS = ('cursor', 'claude', 'codex')
# Cursor events after its last fetch are taken as known for this long, since
# the dashboard is fetched again every few minutes while the HUD runs.
CURSOR_FRESH = 30 * 60


def _hourly(s, e):
    """A stretch of time cut at each hour boundary, as (local moment, hours) pieces."""
    t = s
    while t < e:
        moment = datetime.fromtimestamp(t)
        step = min(e, t + 3600 - moment.minute * 60 - moment.second - moment.microsecond / 1e6)
        yield moment, (step - t) / 3600
        t = step


class _Days:
    """The calendar days from `since` to `now`, local time, and where each begins."""

    def __init__(self, since, now):
        self.first = first = datetime.fromtimestamp(since).date().toordinal()
        self.today = datetime.fromtimestamp(now).date().toordinal()
        self.ordinals = list(range(first, self.today + 1))
        self.starts = [datetime.fromordinal(o).timestamp() for o in self.ordinals] + \
                      [datetime.fromordinal(self.today + 1).timestamp()]
        self.dates = [datetime.fromordinal(o).strftime('%Y-%m-%d') for o in self.ordinals]

    def index(self, t):
        """The day a moment falls on, or None outside the range."""
        i = bisect.bisect_right(self.starts, t) - 1
        return i if 0 <= i < len(self.ordinals) else None

    def split(self, s, e):
        """The part of a stretch inside the range, cut at each midnight, as
        (day index, seconds) pieces."""
        s = max(s, self.starts[0])
        i = self.index(s)
        while i is not None and i < len(self.ordinals) and s < e:
            stop = min(e, self.starts[i + 1])
            yield i, stop - s
            s = stop
            i += 1


def progress():
    """How far reading every log has got: the backfill, in bytes, with the
    names the first Overview's reading screen used."""
    state = ledger.default().backfill()
    return {**state, 'read': state['read_bytes'], 'total': state['total_bytes']}


def _cursor(facts, since, now, days):
    """Cursor's value over the range from the dashboard events the ledger
    holds, and per day. A range or day counts as complete only when fetches
    that saw every event cover all of it; otherwise its value is unknown."""
    if facts is None or not (facts['events'] or facts['coverage']):
        return None, [None] * len(days.ordinals)
    merged = ledger._merge(facts['coverage'])
    if merged:
        merged[-1][1] += CURSOR_FRESH

    def covered(lo, hi):
        return any(a <= lo and b >= hi for a, b in merged)

    def total(values):
        values = list(values)
        return None if any(v is None for v in values) else sum(values)

    events = facts['events']
    per_day = collections.defaultdict(list)
    for event in events:
        i = days.index(event[0])
        if i is not None:
            per_day[i].append(event[1])
    day_cents = []
    for i, start in enumerate(days.starts[:-1]):
        lo = max(start, since)
        day_cents.append(total(per_day[i]) if covered(lo, min(days.starts[i + 1], now)) else None)
    cents = total(e[1] for e in events)
    block = {'cents': round(cents, 2) if cents is not None else None,
             'tokens': {key: total(e[2 + n] for e in events)
                        for n, key in enumerate(('input', 'output', 'cache_read', 'cache_write'))},
             'requests': len(events), 'complete': covered(since, now)}
    return block, [round(c, 2) if c is not None else None for c in day_cents]


def _focus(spans, first_seen, since, now, days):
    """Time an AI app had the focus, per app and per day."""
    clipped = [(app, max(s, since), min(e, now)) for app, s, e in spans]
    clipped = [(app, s, e) for app, s, e in clipped if e > s]
    apps = collections.Counter()
    day_apps = [collections.Counter() for _ in days.ordinals]
    for app, s, e in clipped:
        apps[app] += e - s
        for i, seconds in days.split(s, e):
            day_apps[i][app] += seconds
    union = ledger._merge([(s, e) for _, s, e in clipped])
    day_total = [0.0] * len(days.ordinals)
    for s, e in union:
        for i, seconds in days.split(s, e):
            day_total[i] += seconds
    return {
        'hours': round(sum(e - s for s, e in union) / 3600, 2),
        'apps': [{'id': app, 'label': ledger.FOCUS_APPS.get(app, app), 'hours': round(seconds / 3600, 2)}
                 for app, seconds in apps.most_common()],
        'days': [{'date': date, 'hours': round(day_total[i] / 3600, 2),
                  'apps': {app: round(seconds / 3600, 2) for app, seconds in day_apps[i].most_common()}}
                 for i, date in enumerate(days.dates)],
        'tracked_since': first_seen,
        'idle_cutoff_s': ledger.FOCUS_IDLE_CUTOFF,
    }


def overview(since, now=None, cursor_minutes=None, cursor_account=None, led=None):
    """The Overview from `since` on. `cursor_account` is the Cursor sign-in
    whose dashboard events and activity minutes count; `cursor_minutes`, if
    given, stands in for the minutes ({'interactive': [...], 'headless': [...],
    'subagent': [...]}). Cursor records no chat boundaries, so its activity
    counts as one agent lane, and only its foreground requests in your editor
    count as you at the keyboard."""
    now = now or time.time()
    led = led or ledger.default()
    # Whatever the logs in use gained in the last seconds, if that is quick.
    led.catch_up()
    facts = led.overview_facts(since, cursor_account)
    days = _Days(since, now)

    per = {p: {'cents': 0.0, 'unpriced': 0, 'tokens': collections.Counter(), 'agent_seconds': 0.0,
               'sessions': set(), 'subagents': set()} for p in PROVIDERS}
    day_cents = {p: [0.0] * len(days.ordinals) for p in PROVIDERS}
    for provider, at, n, unpriced, cents, inp, out, cache_read, cache_write in facts['requests']:
        row = per.get(provider)
        if row is None:
            continue
        row['cents'] += cents
        row['unpriced'] += unpriced
        row['tokens'].update({'input': inp, 'output': out, 'cache_read': cache_read, 'cache_write': cache_write})
        i = days.index(at)
        if i is not None:
            day_cents[provider][i] += cents

    if cursor_minutes is None and facts['cursor'] is not None:
        cursor_minutes = {}
        for kind, t in facts['cursor']['minutes']:
            cursor_minutes.setdefault(kind, []).append(t)
    cursor_minutes = cursor_minutes or {}

    def minutes(kind):
        return [t for t in cursor_minutes.get(kind) or [] if isinstance(t, (int, float)) and t >= since]
    cursor_interactive = minutes('interactive')
    cursor_all = cursor_interactive + minutes('headless') + minutes('subagent')
    agents = []
    cursor_seconds = 0.0
    if cursor_all:
        spans = [(s, e + 60) for s, e in _intervals(cursor_all)]
        cursor_seconds = sum(e - s for s, e in spans)
        agents.append(('cursor', spans))

    # Only what happened in the range counts, even in a log begun before it.
    by_log = collections.defaultdict(list)
    for log, provider, kind, s, e in facts['spans']:
        by_log[(log, provider, kind)].append((max(s, since), e))
    for (log, provider, kind), spans in by_log.items():
        row = per.get(provider)
        if row is None:
            continue
        row['sessions' if kind == 'session' else 'subagents'].add(log)
        row['agent_seconds'] += sum(e - s for s, e in spans)
        agents.append((provider, spans))
    tools = facts['tools']
    prompts = facts['prompts']

    # Real time with any agent running, and the most running at once.
    edges = sorted([(s, 1) for _, spans in agents for s, _ in spans] +
                   [(max(e, s + 1), -1) for _, spans in agents for s, e in spans])
    live = peak = 0
    wall = 0.0
    peak_at = last = None
    for t, step in edges:
        if live and last is not None:
            wall += t - last
        live += step
        last = t
        if live > peak:
            peak, peak_at = live, t

    # Agent hours per day of the range, and hour by hour for today.
    today = datetime.fromtimestamp(now).date()
    day_hours = {p: [0.0] * len(days.ordinals) for p in DAY_PROVIDERS}
    hours = [{'cursor': 0.0, 'claude': 0.0, 'codex': 0.0, 'you': 0.0} for _ in range(24)]
    heat = [[0.0] * 24 for _ in range(7)]
    first_day = days.first
    for provider, spans in agents:
        lane = day_hours[provider]
        for s, e in spans:
            for moment, length in _hourly(s, e):
                heat[moment.weekday()][moment.hour] += length * 60
                ordinal = moment.toordinal()
                if first_day <= ordinal <= days.today:
                    lane[ordinal - first_day] += length
                if ordinal == days.today:
                    hours[moment.hour][provider] += length

    # You are at the keyboard when you prompt Claude or Codex, and while
    # Cursor's foreground agent is taking requests in your editor.
    presence = sorted([t for t, _ in prompts] + cursor_interactive)
    sittings = []
    for t in presence:
        if sittings and t - sittings[-1][1] <= SITTING_GAP:
            sittings[-1][1] = t
        else:
            sittings.append([t, t])
    longest = max(sittings, key=lambda s: s[1] - s[0], default=None)

    # Your time per calendar day, a sitting past midnight split at midnight.
    at_desk = collections.Counter()
    for s, e in sittings:
        for moment, length in _hourly(s, e + SITTING_PAD):
            at_desk[moment.toordinal()] += length
            if moment.date() == today:
                hours[moment.hour]['you'] += length
    desk_days = [{'date': date, 'hours': round(at_desk[o], 2)} for o, date in zip(days.ordinals, days.dates)]

    cursor, cursor_day_cents = _cursor(facts['cursor'], since, now, days)
    day_cents['cursor'] = cursor_day_cents

    return {
        'since': since,
        'providers': {p: {'cents': round(row['cents'], 2), 'tokens': dict(row['tokens']),
                          'agent_hours': round(row['agent_seconds'] / 3600, 2),
                          'sessions': len(row['sessions']), 'subagents': len(row['subagents']),
                          'unpriced_requests': row['unpriced']}
                      for p, row in per.items()},
        'cursor_agent_hours': round(cursor_seconds / 3600, 2) if cursor_all else None,
        'wall_hours': round(wall / 3600, 2),
        'peak': {'agents': peak, 'at': peak_at},
        'prompts': len(prompts),
        'sittings': len(sittings),
        'first_seen': sittings[0][0] if sittings else None,
        'human_hours': round(sum(e - s + SITTING_PAD for s, e in sittings) / 3600, 2),
        'think_hours': round(sum(think for _, think in prompts) / 3600, 2),
        'longest_sitting': {'start': longest[0], 'hours': round((longest[1] - longest[0] + SITTING_PAD) / 3600, 2)}
                           if longest else None,
        'desk_days': desk_days,
        'tool_calls': sum(tools.values()),
        'tools': tools.most_common(),
        'days': [{'date': date, **{p: round(day_hours[p][i], 2) for p in DAY_PROVIDERS},
                  'cents': {p: (round(day_cents[p][i], 2) if day_cents[p][i] is not None else None)
                            for p in DAY_PROVIDERS}}
                 for i, date in enumerate(days.dates)],
        'hours': [{'hour': h, **{k: round(v, 3) for k, v in row.items()}} for h, row in enumerate(hours)],
        'heat': [[round(m) for m in row] for row in heat],
        'cursor': cursor,
        'focus': _focus(facts['focus'], facts['focus_since'], since, now, days),
        'backfill': led.backfill(),
        'db': {'path': str(led.path), 'bytes': led.size()},
    }
