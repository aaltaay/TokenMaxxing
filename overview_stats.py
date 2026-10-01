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
MAX_BYTES = 256 * 1024 * 1024

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


def _intervals(times):
    out = []
    for t in sorted(times):
        if out and t - out[-1][1] <= IDLE_GAP:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def _claude_facts(path):
    kind = 'subagent' if 'subagents' in path.parts else 'session'
    times, prompts, seen = [], [], set()
    tokens = collections.Counter()
    tools = collections.Counter()
    last_reply = None
    with path.open(encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            t = _epoch(record.get('timestamp'))
            if t is not None:
                times.append(t)
            message = record.get('message') if isinstance(record.get('message'), dict) else {}
            if record.get('type') == 'assistant':
                last_reply = t or last_reply
                usage = message.get('usage')
                key = message.get('id') or record.get('requestId')
                if isinstance(usage, dict) and key not in seen:
                    seen.add(key)
                    tokens['input'] += usage.get('input_tokens') or 0
                    tokens['output'] += usage.get('output_tokens') or 0
                    tokens['cache_read'] += usage.get('cache_read_input_tokens') or 0
                    tokens['cache_write'] += usage.get('cache_creation_input_tokens') or 0
                for part in message.get('content') or []:
                    if isinstance(part, dict) and part.get('type') == 'tool_use':
                        tools[tool_group(part.get('name'))] += 1
            elif (record.get('type') == 'user' and kind == 'session' and t is not None
                  and not record.get('isMeta') and isinstance(message.get('content'), str)):
                text = message['content'].lstrip()
                # Slash commands, hook output and injected reminders are not you typing.
                if text and not text.startswith('<'):
                    think = t - last_reply if last_reply and 0 < t - last_reply <= THINK_CAP else 0
                    prompts.append((t, think))
    return {'kind': kind, 'intervals': _intervals(times), 'prompts': prompts,
            'tokens': dict(tokens), 'tools': dict(tools)}


def _codex_facts(path):
    kind, times, prompts = 'session', [], []
    tokens = {}
    tools = collections.Counter()
    last_reply = None
    with path.open(encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            t = _epoch(record.get('timestamp'))
            if t is not None:
                times.append(t)
            payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
            kind_of = record.get('type')
            ptype = payload.get('type')
            if kind_of == 'session_meta' and isinstance(payload.get('source'), dict) and 'subagent' in payload['source']:
                kind = 'subagent'
            elif kind_of == 'response_item' and ptype in ('function_call', 'custom_tool_call', 'local_shell_call'):
                tools[tool_group(payload.get('name') or ptype)] += 1
            elif kind_of == 'event_msg' and ptype == 'agent_message':
                last_reply = t or last_reply
            elif kind_of == 'event_msg' and ptype == 'user_message' and t is not None:
                think = t - last_reply if last_reply and 0 < t - last_reply <= THINK_CAP else 0
                prompts.append((t, think))
            elif kind_of == 'event_msg' and ptype == 'token_count':
                total = (payload.get('info') or {}).get('total_token_usage')
                if isinstance(total, dict):
                    tokens = total
    cached = tokens.get('cached_input_tokens') or 0
    return {'kind': kind, 'intervals': _intervals(times),
            # A subagent's prompts come from its parent agent, not from you.
            'prompts': prompts if kind == 'session' else [],
            'tokens': {'input': max(0, (tokens.get('input_tokens') or 0) - cached),
                       'cache_read': cached, 'output': tokens.get('output_tokens') or 0, 'cache_write': 0},
            'tools': dict(tools)}


def file_facts(provider, path):
    """One log's facts, reused until the file changes."""
    stat = path.stat()
    key = (provider, str(path))
    with _lock:
        hit = _facts.get(key)
    if hit and hit['stamp'] == (stat.st_size, stat.st_mtime_ns):
        return hit
    if stat.st_size > MAX_BYTES:
        return None
    facts = (_claude_facts if provider == 'claude' else _codex_facts)(path)
    try:
        facts['cents'] = sc.summarize_file(path, provider).get('cents') or 0
    except Exception:
        facts['cents'] = 0
    facts['stamp'] = (stat.st_size, stat.st_mtime_ns)
    with _lock:
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
    """`cursor_minutes` is {'interactive': [...], 'headless': [...]}, the
    minutes Cursor's dashboard logged requests in. Cursor records no chat
    boundaries, so its activity counts as one agent lane."""
    now = now or datetime.now().timestamp()
    agents = []
    per = {p: {'cents': 0, 'tokens': collections.Counter(), 'agent_seconds': 0.0,
               'sessions': 0, 'subagents': 0} for p in ('claude', 'codex')}
    cursor_minutes = cursor_minutes or {}
    cursor_interactive = [t for t in cursor_minutes.get('interactive') or [] if isinstance(t, (int, float)) and t >= since]
    cursor_all = cursor_interactive + [t for t in cursor_minutes.get('headless') or []
                                       if isinstance(t, (int, float)) and t >= since]
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
            if not facts:
                continue
            spans = [(max(s, since), e) for s, e in facts['intervals'] if e >= since]
            row = per[provider]
            row['cents'] += facts['cents']
            row['tokens'].update(facts['tokens'])
            tools.update(facts['tools'])
            prompts += [p for p in facts['prompts'] if p[0] >= since]
            if spans:
                row['sessions' if facts['kind'] == 'session' else 'subagents'] += 1
                row['agent_seconds'] += sum(e - s for s, e in spans)
                agents.append((provider, spans))

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

    heat = [[0.0] * 24 for _ in range(7)]
    today = datetime.fromtimestamp(now).date()
    days = {(today.toordinal() - i): {'cursor': 0.0, 'claude': 0.0, 'codex': 0.0} for i in range(7)}
    for provider, spans in agents:
        for s, e in spans:
            t = s
            while t < e:
                moment = datetime.fromtimestamp(t)
                step = min(e, t + 3600 - moment.minute * 60 - moment.second)
                heat[moment.weekday()][moment.hour] += (step - t) / 60
                day = days.get(moment.date().toordinal())
                if day is not None:
                    day[provider] += (step - t) / 3600
                t = step

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
    at_desk = {}
    for s, e in sittings:
        t, end = s, e + SITTING_PAD
        while t < end:
            moment = datetime.fromtimestamp(t)
            midnight = datetime(moment.year, moment.month, moment.day).timestamp() + 86400
            step = min(end, midnight)
            key = moment.strftime('%Y-%m-%d')
            at_desk[key] = at_desk.get(key, 0.0) + (step - t) / 3600
            t = step
    first_day = datetime.fromtimestamp(since).date().toordinal()
    desk_days = [{'date': datetime.fromordinal(o).strftime('%Y-%m-%d'),
                  'hours': round(at_desk.get(datetime.fromordinal(o).strftime('%Y-%m-%d'), 0.0), 2)}
                 for o in range(first_day, today.toordinal() + 1)]

    return {
        'since': since,
        'providers': {p: {'cents': round(row['cents'], 2), 'tokens': dict(row['tokens']),
                          'agent_hours': round(row['agent_seconds'] / 3600, 2),
                          'sessions': row['sessions'], 'subagents': row['subagents']}
                      for p, row in per.items()},
        'cursor_agent_hours': round(cursor_seconds / 3600, 2) if cursor_all else None,
        'wall_hours': round(wall / 3600, 2),
        'peak': {'agents': peak, 'at': peak_at},
        'prompts': len(prompts),
        'sittings': len(sittings),
        'human_hours': round(sum(e - s + SITTING_PAD for s, e in sittings) / 3600, 2),
        'think_hours': round(sum(think for _, think in prompts) / 3600, 2),
        'longest_sitting': {'start': longest[0], 'hours': round((longest[1] - longest[0] + SITTING_PAD) / 3600, 2)}
                           if longest else None,
        'desk_days': desk_days,
        'tool_calls': sum(tools.values()),
        'tools': tools.most_common(),
        'days': [{'date': datetime.fromordinal(o).strftime('%Y-%m-%d'), **{k: round(h, 2) for k, h in v.items()}}
                 for o, v in sorted(days.items())],
        'heat': [[round(m) for m in row] for row in heat],
    }
