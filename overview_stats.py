"""Cycle-wide totals from local Claude Code and Codex logs: API-price value,
tokens, how long agents ran, how long you were at the keyboard, and what the
agents did. Every figure is read from the logs; nothing is guessed.

What each log has said so far is kept on disk too, in `overview-facts.json`
in the HUD folder, so a fresh engine reads only what the logs gained since the
last one looked instead of every log in the range."""
from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
from datetime import datetime

import session_cost as sc
import session_usage as su
from cursor_usage import hud_dir

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
# The saved facts' layout. Changes to how logs are read need no bump: the
# saved file names the readers and prices it was made with (see _readers).
CACHE_VERSION = 1
# Save what was read once reading it again would take this many seconds,
SAVE_AFTER_READING = 2
# during one long read every this many seconds of it, so quitting midway
# keeps most of it,
CHECKPOINT_EVERY = 30
# and otherwise at most this often while the logs keep growing.
SAVE_EVERY = 300

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
# Held while the facts are written out, so saves land in the order they were taken.
_writing = threading.Lock()
# The file on disk: taken up yet, logs read since the last save and the
# seconds that took, when that save was, and the readers' fingerprint.
_disk = {'loaded': False, 'dirty': False, 'unsaved': 0.0, 'saved_at': 0.0, 'readers': None}
# How far the reads under way have got, in bytes, for the "reading" screen.
_progress = {'read': 0, 'total': 0, 'running': 0}


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


def _id_digest(key):
    """An id as 8 bytes, so a month of logs' ids stays small on disk. Two ids
    in one log sharing a digest is a chance of about one in a billion."""
    return hashlib.blake2b(str(key).encode('utf-8', 'surrogatepass'), digest_size=8).digest()


def _claude_reader(path, facts):
    owner = _owner(path)
    seen = facts.setdefault('seen', set())

    def once(key):
        """One request is logged once per content block, and a rewound session
        logs earlier records again: count each id once. No id, no repeat."""
        if key is None:
            return True
        key = _id_digest(key)
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
            _progress['read'] += len(line)
            if isinstance(record, dict):
                t = reader(record)
                if t is not None:
                    times.append(t)
    facts['intervals'] = _intervals(times, facts['intervals'])


def file_facts(provider, path, stat=None):
    """One log's facts, kept and extended as the file grows."""
    stat = stat or path.stat()
    key = (provider, str(path))
    stamp = (stat.st_size, stat.st_mtime_ns)
    with _lock:
        if not _disk['loaded']:
            _load()
        facts = _facts.get(key)
        if facts and facts['stamp'] == stamp:
            return facts
        if not facts or stat.st_size < facts['offset']:
            facts = {'kind': 'subagent' if provider == 'claude' and 'subagents' in path.parts else 'session',
                     'offset': 0, 'intervals': [], 'prompts': [], 'requests': [], 'tools': []}
        began = time.monotonic()
        _read(path, facts, (_claude_reader if provider == 'claude' else _codex_reader)(path, facts))
        facts['stamp'] = stamp
        _facts[key] = facts
        _disk['dirty'] = True
        _disk['unsaved'] += time.monotonic() - began
        return facts


def _logs(provider, since):
    """The logs written to since `since`, each with what stat said of it."""
    root = su.session_root(provider)
    if not root.exists():
        return []
    out = []
    for path in root.rglob('*.jsonl'):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= since:
            out.append((path, stat))
    return out


# ── the facts on disk ─────────────────────────────────────────────────────

def cache_path():
    return hud_dir() / 'overview-facts.json'


def _readers():
    """A fingerprint of how logs are read: the code of file_facts and of every
    function in this module and session_cost it reaches, and the constants
    (IDLE_GAP, TOOL_GROUPS, the price tables...) they use. Saved facts count
    only under the same fingerprint, so a new price or reading rule reads
    every log afresh instead of keeping numbers the new code would not give."""
    ours = {__name__, sc.__name__}
    # Python's own parsing (JSON, ISO dates) can change between minor versions.
    digest = hashlib.sha256(repr(sys.version_info[:2]).encode())
    walked = set()

    def canon(value):
        if isinstance(value, dict):
            return '{%s}' % ','.join(f'{canon(k)}:{canon(v)}' for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return '[%s]' % ','.join(canon(v) for v in value)
        if isinstance(value, (set, frozenset)):
            return '{%s}' % ','.join(sorted(canon(v) for v in value))
        if isinstance(value, types.FunctionType):
            walk(value)
            return value.__qualname__
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            return repr(value)
        # Locks, caches and other live state say nothing about how logs are read.
        return type(value).__name__

    def code(block, scope):
        digest.update(block.co_code)
        digest.update(canon([c for c in block.co_consts if not isinstance(c, types.CodeType)]).encode())
        for name in block.co_names:
            value = scope.get(name)
            if isinstance(value, types.ModuleType):
                if value.__name__ in ours:
                    for attr in block.co_names:
                        member = getattr(value, attr, None)
                        if attr.isupper() or isinstance(member, types.FunctionType):
                            digest.update(f'{attr}={canon(member)}'.encode())
            elif isinstance(value, types.FunctionType) or name.isupper():
                digest.update(f'{name}={canon(value)}'.encode())
        for inner in block.co_consts:
            if isinstance(inner, types.CodeType):
                code(inner, scope)

    def walk(function):
        if function in walked or function.__module__ not in ours:
            return
        walked.add(function)
        digest.update(canon(function.__defaults__).encode())
        code(function.__code__, function.__globals__)

    walk(file_facts)
    return digest.hexdigest()


def _fingerprint():
    if _disk['readers'] is None:
        _disk['readers'] = _readers()
    return _disk['readers']


def _number(value, kinds=(int, float)):
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ValueError(f'not a number: {value!r}')
    return value


def _optional(value):
    return None if value is None else _number(value)


def _text(value):
    if not isinstance(value, str):
        raise ValueError(f'not text: {value!r}')
    return value


def _saveable(facts):
    out = {key: value for key, value in facts.items() if key != 'seen'}
    if 'seen' in facts:
        out['seen'] = base64.b64encode(b''.join(facts['seen'])).decode('ascii')
    return out


def _revive(saved):
    """Saved facts back in the shapes the readers keep them in, checked field
    by field. Anything off raises, and that log is read afresh."""
    stamp = tuple(_number(n, int) for n in saved['stamp'])
    offset = _number(saved['offset'], int)
    if saved['kind'] not in ('session', 'subagent') or len(stamp) != 2 or offset < 0:
        raise ValueError('malformed log facts')
    facts = {**saved, 'stamp': stamp, 'offset': offset,
             'intervals': [[_number(s), _number(e)] for s, e in saved['intervals']],
             'prompts': [(_number(t), _number(think)) for t, think in saved['prompts']],
             'requests': [(_number(t), _optional(cents), _number(a), _number(b), _number(c), _number(d))
                          for t, cents, a, b, c, d in saved['requests']],
             'tools': [(_number(t), _text(group)) for t, group in saved['tools']]}
    for key in ('last_t', 'last_reply'):
        if key in saved:
            facts[key] = _optional(saved[key])
    if saved.get('previous') is not None:
        facts['previous'] = tuple(_number(n) for n in saved['previous'])
    if 'seen' in saved:
        raw = base64.b64decode(_text(saved['seen']), validate=True)
        if len(raw) % 8:
            raise ValueError('malformed seen ids')
        facts['seen'] = {raw[i:i + 8] for i in range(0, len(raw), 8)}
    return facts


def _load():
    """Take up what the last engine saved; called once, under _lock. A file
    that is unreadable, of another layout or made by other readers or prices
    is passed over, and every log is read afresh."""
    _disk.update(loaded=True, saved_at=time.monotonic())
    # A save cut short by the engine being killed leaves its temporary file.
    for stray in cache_path().parent.glob('.overview-facts-*.tmp'):
        try:
            if time.time() - stray.stat().st_mtime > 3600:
                stray.unlink()
        except OSError:
            pass
    try:
        saved = json.loads(cache_path().read_bytes())
    except (OSError, ValueError):
        return
    if (not isinstance(saved, dict) or saved.get('version') != CACHE_VERSION
            or saved.get('readers') != _fingerprint() or not isinstance(saved.get('logs'), list)):
        return
    for entry in saved['logs']:
        try:
            provider, path, facts = entry
            if provider in ('claude', 'codex') and isinstance(path, str):
                _facts.setdefault((provider, path), _revive(facts))
        except (TypeError, ValueError, KeyError, AttributeError, binascii.Error):
            continue


def _save():
    """Write the facts where the next engine will look. It is a cache: a write
    that fails is tried again at a later save, never an error."""
    with _writing:
        with _lock:
            if not _disk['dirty']:
                return
            blob = json.dumps({'version': CACHE_VERSION, 'readers': _fingerprint(),
                               'logs': [[provider, path, _saveable(facts)] for (provider, path), facts in _facts.items()]},
                              separators=(',', ':'))
            _disk.update(dirty=False, unsaved=0.0, saved_at=time.monotonic())
        path = cache_path()
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.overview-facts-', suffix='.tmp')
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                stream.write(blob)
            os.replace(temporary, path)
        except OSError:
            _disk['dirty'] = True
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _save_due(reread):
    """Whether to save: what is unsaved would take `reread` seconds to read
    again, or the last save is SAVE_EVERY old."""
    return _disk['dirty'] and (_disk['unsaved'] >= reread or time.monotonic() - _disk['saved_at'] >= SAVE_EVERY)


def progress():
    """How far the overview reads under way have got, or None between reads."""
    if not _progress['running']:
        return None
    return {'read': _progress['read'], 'total': max(_progress['read'], _progress['total'])}


def _read_logs(since):
    """(provider, facts) for every log written to since `since`, read as far
    as each has grown, saving along the way during a long first read."""
    logs = [(provider, path, stat) for provider in ('claude', 'codex') for path, stat in _logs(provider, since)]
    with _lock:
        if not _disk['loaded']:
            _load()
        unread = 0
        for provider, path, stat in logs:
            facts = _facts.get((provider, str(path)))
            if not facts or facts['stamp'] != (stat.st_size, stat.st_mtime_ns):
                unread += stat.st_size - (facts['offset'] if facts and stat.st_size >= facts['offset'] else 0)
        if not _progress['running']:
            _progress.update(read=0, total=0)
        _progress['running'] += 1
        _progress['total'] = max(_progress['total'], _progress['read'] + unread)
    out = []
    try:
        for provider, path, stat in logs:
            try:
                out.append((provider, file_facts(provider, path, stat)))
            except OSError:
                continue
            if _save_due(CHECKPOINT_EVERY):
                _save()
    finally:
        with _lock:
            _progress['running'] -= 1
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
    for provider, facts in _read_logs(since):
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
        old = [key for key, facts in _facts.items() if facts['stamp'][1] / 1e9 < now - FORGET_AFTER]
        for key in old:
            del _facts[key]
        _disk['dirty'] = _disk['dirty'] or bool(old)
    # Saved beside the reply, not ahead of it. Not a daemon (as the bridge's
    # request threads are), so an engine told to quit lets the save land first.
    if _save_due(SAVE_AFTER_READING):
        threading.Thread(target=_save, name='overview-save', daemon=False).start()

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
