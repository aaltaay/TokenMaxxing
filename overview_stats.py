"""Cycle-wide totals from local Claude Code and Codex logs: API-price value,
tokens, how long agents ran, how long you were at the keyboard, and what the
agents did. Every figure is read from the logs; nothing is guessed."""
from __future__ import annotations

import collections
import json
import threading
from datetime import datetime

import session_cost as sc
import session_usage as su

# A log that goes quiet for longer than this was idle, not working.
IDLE_GAP = 300
# Prompts closer together than this are one sitting at the keyboard.
SITTING_GAP = 30 * 60
# Credit for reading the reply that ended a sitting.
SITTING_PAD = 5 * 60
# The longest pause between a reply and your next prompt counted as reading it.
THINK_CAP = 15 * 60
# Longer than any range the Overview shows (30 days, or a billing cycle).
FORGET_AFTER = 40 * 86400

TOOL_GROUPS = (
    ('Shell', ('bash', 'powershell', 'exec', 'shell', 'local_shell_call', 'exec_command', 'write_stdin')),
    ('Edit', ('edit', 'multiedit', 'notebookedit', 'apply_patch')),
    ('Read', ('read',)),
    ('Write', ('write',)),
    ('Search', ('grep', 'glob', 'websearch', 'toolsearch')),
    ('Browser', ('browser', 'computer', 'navigate', 'claude-in-chrome', 'claude_browser')),
    ('Web fetch', ('webfetch',)),
    ('Subagents', ('agent', 'task', 'workflow', 'sendmessage')),
)

_facts: dict = {}
# Held while a log is read, so two refreshes never read the same one at once.
_lock = threading.Lock()


def _epoch(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def tool_group(name):
    low = str(name or '').lower()
    for group, keys in TOOL_GROUPS:
        if low in keys or any(key in low for key in keys if len(key) > 5):
            return group
    return 'Other'


def _typed(record):
    """Whether a user record is a prompt you sent. Newer Claude Code logs say
    who sent each one; task notifications, scheduled tasks and sessions other
    sessions started are not you. Older logs are judged by content: a prompt
    with a screenshot attached arrives as a list of parts, and so do tool
    results, which are the agent's own work."""
    if record.get('isMeta') or record.get('isCompactSummary'):
        return False
    origin = record.get('origin')
    if isinstance(origin, dict) and origin.get('kind'):
        return origin['kind'] == 'human'
    if record.get('turnOrigin') not in (None, 'human'):
        return False
    message = record.get('message') if isinstance(record.get('message'), dict) else {}
    content = message.get('content')
    if isinstance(content, str):
        parts = [{'type': 'text', 'text': content}]
    elif isinstance(content, list):
        parts = [part for part in content if isinstance(part, dict)]
    else:
        return False
    if any(part.get('type') == 'tool_result' for part in parts):
        return False
    # Slash commands, hook output, injected reminders and the marker an
    # interrupted turn leaves are not you typing.
    return any(part.get('type') in ('image', 'document')
               or (part.get('type') == 'text' and str(part.get('text') or '').strip()
                   and not str(part.get('text')).lstrip().startswith(('<', '[Request interrupted')))
               for part in parts)


def _intervals(times, spans=()):
    """Busy stretches: moments and earlier stretches less than IDLE_GAP apart merge."""
    out = []
    for s, e in sorted([tuple(span) for span in spans] + [(t, t) for t in times]):
        if out and s - out[-1][1] <= IDLE_GAP:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _hourly(s, e):
    """A stretch of time cut at each hour boundary, as (local moment, hours) pieces."""
    t = s
    while t < e:
        moment = datetime.fromtimestamp(t)
        step = min(e, t + 3600 - moment.minute * 60 - moment.second - moment.microsecond / 1e6)
        yield moment, (step - t) / 3600
        t = step


def _owner(path):
    """The session a Claude log belongs to. A forked session's log opens with a
    copy of its parent's records, and its subagent logs are copied whole; those
    records keep the parent's sessionId and are counted in the parent."""
    parts = path.parts
    return parts[parts.index('subagents') - 1] if 'subagents' in parts else path.stem


def _claude_reader(path, facts):
    owner = _owner(path)
    seen = facts.setdefault('seen', set())

    def once(key):
        """One request is logged once per content block, and a rewound session
        logs earlier records again: count each id once. No id, no repeat."""
        if key is None:
            return True
        if key in seen:
            return False
        seen.add(key)
        return True

    def read(record):
        if record.get('sessionId') not in (None, owner):
            return None
        t = _epoch(record.get('timestamp'))
        if t is not None:
            facts['last_t'] = t
        at = t if t is not None else facts.get('last_t')
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        if record.get('type') == 'assistant':
            facts['last_reply'] = t or facts.get('last_reply')
            usage = message.get('usage')
            if isinstance(usage, dict) and at is not None and once(message.get('id') or record.get('requestId')):
                model = message.get('model')
                # Unknown models stay None so the Overview can say they count as $0.
                cents = 0.0 if model == '<synthetic>' else sc.claude_request_cents(model, usage)
                facts['requests'].append((at, cents, usage.get('input_tokens') or 0,
                                          usage.get('output_tokens') or 0, usage.get('cache_read_input_tokens') or 0,
                                          usage.get('cache_creation_input_tokens') or 0))
            for part in message.get('content') or []:
                if isinstance(part, dict) and part.get('type') == 'tool_use' and at is not None and once(part.get('id')):
                    facts['tools'].append((at, tool_group(part.get('name'))))
        elif (record.get('type') == 'user' and facts['kind'] == 'session' and t is not None
              and _typed(record) and once(record.get('uuid'))):
            last = facts.get('last_reply')
            think = t - last if last and 0 < t - last <= THINK_CAP else 0
            facts['prompts'].append((t, think))
        return t
    return read


def _codex_reader(path, facts):
    def read(record):
        t = _epoch(record.get('timestamp'))
        if t is not None:
            facts['last_t'] = t
        at = t if t is not None else facts.get('last_t')
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        kind_of = record.get('type')
        ptype = payload.get('type')
        if kind_of == 'session_meta' and isinstance(payload.get('source'), dict) and 'subagent' in payload['source']:
            facts['kind'] = 'subagent'
            # A subagent's prompts come from its parent agent, not from you.
            facts['prompts'].clear()
        elif kind_of == 'turn_context':
            facts['model'] = payload.get('model')
        elif kind_of == 'response_item' and ptype in ('function_call', 'custom_tool_call', 'local_shell_call') and at is not None:
            facts['tools'].append((at, tool_group(payload.get('name') or ptype)))
        elif kind_of == 'event_msg' and ptype == 'agent_message':
            facts['last_reply'] = t or facts.get('last_reply')
        elif kind_of == 'event_msg' and ptype == 'user_message' and t is not None and facts['kind'] == 'session':
            last = facts.get('last_reply')
            facts['prompts'].append((t, t - last if last and 0 < t - last <= THINK_CAP else 0))
        elif kind_of == 'event_msg' and ptype == 'token_count' and at is not None:
            info = payload.get('info') if isinstance(payload.get('info'), dict) else {}
            total = sc.counts(info.get('total_token_usage'))
            previous = facts.get('previous') or (0, 0, 0)
            # Repeated quota updates carry the same cumulative counters.
            if total is None or total == previous:
                return t
            delta = tuple(a - b for a, b in zip(total, previous))
            if min(delta) < 0:
                delta = total
            last = info.get('last_token_usage')
            cents = sc.request_cents(facts.get('model'), last) if delta == sc.counts(last) else None
            facts['previous'] = total
            facts['requests'].append((at, cents, max(0, delta[0] - delta[1]), delta[2], delta[1], 0))
        return t
    return read


def _read(path, facts, reader):
    """Read what the log gained since the last look. Logs only grow, so a big
    live one costs only its new lines; a line still being written waits."""
    times = []
    with path.open('rb') as stream:
        stream.seek(facts['offset'])
        for line in stream:
            try:
                record = json.loads(line.decode('utf-8', 'replace'))
            except ValueError:
                if not line.endswith(b'\n'):
                    break
                record = None
            facts['offset'] += len(line)
            if isinstance(record, dict):
                t = reader(record)
                if t is not None:
                    times.append(t)
    facts['intervals'] = _intervals(times, facts['intervals'])


def file_facts(provider, path):
    """One log's facts, kept and extended as the file grows."""
    stat = path.stat()
    key = (provider, str(path))
    stamp = (stat.st_size, stat.st_mtime_ns)
    with _lock:
        facts = _facts.get(key)
        if facts and facts['stamp'] == stamp:
            return facts
        if not facts or stat.st_size < facts['offset']:
            facts = {'kind': 'subagent' if provider == 'claude' and 'subagents' in path.parts else 'session',
                     'offset': 0, 'intervals': [], 'prompts': [], 'requests': [], 'tools': []}
        _read(path, facts, (_claude_reader if provider == 'claude' else _codex_reader)(path, facts))
        facts['stamp'] = stamp
        _facts[key] = facts
        return facts


def _logs(provider, since):
    root = su.session_root(provider)
    if not root.exists():
        return []
    out = []
    for path in root.rglob('*.jsonl'):
        try:
            if path.stat().st_mtime >= since:
                out.append(path)
        except OSError:
            continue
    return out


def overview(since, now=None, cursor_minutes=None):
    """`cursor_minutes` is {'interactive': [...], 'headless': [...], 'subagent': [...]},
    the minutes Cursor's dashboard logged requests in. Cursor records no chat
    boundaries, so its activity counts as one agent lane, and only its
    foreground requests in your editor count as you at the keyboard."""
    now = now or datetime.now().timestamp()
    agents = []
    per = {p: {'cents': 0, 'tokens': collections.Counter(), 'agent_seconds': 0.0,
               'sessions': 0, 'subagents': 0, 'unpriced': 0} for p in ('claude', 'codex')}
    cursor_minutes = cursor_minutes or {}

    def minutes(kind):
        return [t for t in cursor_minutes.get(kind) or [] if isinstance(t, (int, float)) and t >= since]
    cursor_interactive = minutes('interactive')
    cursor_all = cursor_interactive + minutes('headless') + minutes('subagent')
    cursor_seconds = 0.0
    if cursor_all:
        spans = [(s, e + 60) for s, e in _intervals(cursor_all)]
        cursor_seconds = sum(e - s for s, e in spans)
        agents.append(('cursor', spans))
    tools = collections.Counter()
    prompts = []
    for provider in ('claude', 'codex'):
        for path in _logs(provider, since):
            try:
                facts = file_facts(provider, path)
            except OSError:
                continue
            # Only what happened in the range counts, even in a log begun before it.
            spans = [(max(s, since), e) for s, e in facts['intervals'] if e >= since]
            row = per[provider]
            for t, cents, inp, out, cache_read, cache_write in facts['requests']:
                if t >= since:
                    row['cents'] += cents or 0
                    row['unpriced'] += cents is None
                    row['tokens'].update({'input': inp, 'output': out, 'cache_read': cache_read, 'cache_write': cache_write})
            tools.update(group for t, group in facts['tools'] if t >= since)
            prompts += [p for p in facts['prompts'] if p[0] >= since]
            if spans:
                row['sessions' if facts['kind'] == 'session' else 'subagents'] += 1
                row['agent_seconds'] += sum(e - s for s, e in spans)
                agents.append((provider, spans))
    # Forget logs too old for any range, so switching ranges never rereads one.
    with _lock:
        for key in [key for key, facts in _facts.items() if facts['stamp'][1] / 1e9 < now - FORGET_AFTER]:
            del _facts[key]

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
    first_day = datetime.fromtimestamp(since).date().toordinal()
    days = {o: {'cursor': 0.0, 'claude': 0.0, 'codex': 0.0} for o in range(first_day, today.toordinal() + 1)}
    hours = [{'cursor': 0.0, 'claude': 0.0, 'codex': 0.0, 'you': 0.0} for _ in range(24)]
    heat = [[0.0] * 24 for _ in range(7)]
    for provider, spans in agents:
        for s, e in spans:
            for moment, length in _hourly(s, e):
                heat[moment.weekday()][moment.hour] += length * 60
                day = days.get(moment.date().toordinal())
                if day is not None:
                    day[provider] += length
                if moment.date() == today:
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
            at_desk[moment.date().toordinal()] += length
            if moment.date() == today:
                hours[moment.hour]['you'] += length
    desk_days = [{'date': datetime.fromordinal(o).strftime('%Y-%m-%d'), 'hours': round(at_desk[o], 2)} for o in days]

    return {
        'since': since,
        'providers': {p: {'cents': round(row['cents'], 2), 'tokens': dict(row['tokens']),
                          'agent_hours': round(row['agent_seconds'] / 3600, 2),
                          'sessions': row['sessions'], 'subagents': row['subagents'],
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
        'days': [{'date': datetime.fromordinal(o).strftime('%Y-%m-%d'), **{k: round(h, 2) for k, h in v.items()}}
                 for o, v in sorted(days.items())],
        'hours': [{'hour': h, **{k: round(v, 3) for k, v in row.items()}} for h, row in enumerate(hours)],
        'heat': [[round(m) for m in row] for row in heat],
    }
