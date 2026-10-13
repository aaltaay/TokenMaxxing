"""The ledger: one SQLite file in the HUD folder holding what the agent logs,
Cursor's dashboard and the focus tracker have said, so history is read once
and never changes.

It stores facts, not caches. Each Claude Code and Codex log is read once, from
where the last read stopped, and what it said lands here: every request,
priced the moment it is first read (a later price table never reprices it),
the prompts you typed, the tool calls, and the stretches the agent was busy.
Cursor's dashboard events are kept once each under a stable key, with the
stretches of time a fetch is known to have covered, and so are the spans an
AI app had the focus. A new layout is a migration (PRAGMA user_version),
never a silent reread of every log.

Reading happens off the request path: a background thread backfills every
log newest first, on worker processes when there is much to read, then every
INGEST_EVERY seconds reads what the logs gained. The Overview only queries."""
from __future__ import annotations

import collections
import concurrent.futures
import concurrent.futures.process
import hashlib
import json
import math
import multiprocessing
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import cursor_usage as cu
import session_cost as sc
import session_usage as su

DB_NAME = 'tokenmaxxing.db'
# A log that goes quiet for longer than this was idle, not working.
IDLE_GAP = 300
# The longest pause between a reply and your next prompt counted as reading it.
THINK_CAP = 15 * 60
# Requests and tool calls are also summed per quarter hour, which every
# timezone's midnight falls on, so a range's totals never walk every request.
SLOT = 900
# How often the background thread looks for what the logs gained.
INGEST_EVERY = 20
# Below this much unread, logs are read on the ingest thread itself; above it
# (a first run is gigabytes) on worker processes, at most this many.
POOL_AFTER = 64 * 1024 * 1024
MAX_WORKERS = 8
# Logs written to this recently are read again just before an Overview
# answers, as long as that takes no more than CATCH_UP_SECONDS.
HOT_FOR = 15 * 60
HOT_MAX = 32
CATCH_UP_SECONDS = 0.05
CATCH_UP_BYTES = 4 * 1024 * 1024
# Claude records longer than this are skimmed, not parsed whole (see _skim);
# a tool result's message begins within its first SKIM_HEAD bytes.
SKIM_OVER = 2048
SKIM_HEAD = 1024

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

# The apps the focus tracker reports, by the id it sends.
FOCUS_APPS = {'claude': 'Claude', 'chatgpt': 'ChatGPT', 'codex': 'Codex', 'cursor': 'Cursor',
              'vscode': 'VS Code', 'windsurf': 'Windsurf', 'terminal': 'Terminal (CLI agents)'}
# Spans of one app closer than this are one span; the tracker resends the
# span still growing, so storing a span twice changes nothing.
FOCUS_MERGE_GAP = 15
FOCUS_MAX_SPAN = 6 * 3600
FOCUS_FUTURE_SLACK = 60
# What the tracker counts as gone from the keyboard, echoed to the Overview.
FOCUS_IDLE_CUTOFF = 300

CURSOR_KINDS = ('interactive', 'headless', 'subagent')

# The layout, one migration per version. A database at version n runs
# migrations n+1 onward, each in its own transaction.
SCHEMA = (
    # 1: the first ledger.
    (
        'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID',
        # One row per log: how far it has been read, and the little reader
        # state that decides how its next lines count.
        'CREATE TABLE logs (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, path TEXT NOT NULL,'
        ' kind TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, offset INTEGER NOT NULL,'
        ' state TEXT NOT NULL, UNIQUE (provider, path))',
        # The ids a Claude log's requests, tool calls and prompts were counted
        # under (8-byte digests run together), each read adding its own row.
        'CREATE TABLE seen (log INTEGER NOT NULL, ids BLOB NOT NULL)',
        'CREATE INDEX seen_log ON seen (log)',
        'CREATE TABLE requests (log INTEGER NOT NULL, provider TEXT NOT NULL, t REAL NOT NULL, cents REAL,'
        ' input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,'
        ' cache_write INTEGER NOT NULL, model TEXT)',
        'CREATE INDEX requests_t ON requests (t)',
        'CREATE TABLE prompts (log INTEGER NOT NULL, t REAL NOT NULL, think REAL NOT NULL)',
        'CREATE INDEX prompts_t ON prompts (t)',
        'CREATE TABLE tools (log INTEGER NOT NULL, t REAL NOT NULL, grp TEXT NOT NULL)',
        'CREATE INDEX tools_t ON tools (t)',
        'CREATE TABLE busy (log INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL)',
        'CREATE INDEX busy_log ON busy (log)',
        'CREATE INDEX busy_end ON busy (end)',
        'CREATE TABLE request_slots (slot INTEGER NOT NULL, provider TEXT NOT NULL, n INTEGER NOT NULL,'
        ' unpriced INTEGER NOT NULL, cents REAL NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,'
        ' cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, PRIMARY KEY (slot, provider)) WITHOUT ROWID',
        'CREATE TABLE tool_slots (slot INTEGER NOT NULL, grp TEXT NOT NULL, n INTEGER NOT NULL,'
        ' PRIMARY KEY (slot, grp)) WITHOUT ROWID',
        'CREATE TABLE cursor_events (key TEXT PRIMARY KEY, account TEXT NOT NULL, t REAL NOT NULL, cents REAL,'
        ' input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, kind TEXT NOT NULL,'
        ' model TEXT) WITHOUT ROWID',
        'CREATE INDEX cursor_events_t ON cursor_events (account, t)',
        'CREATE TABLE cursor_coverage (account TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL)',
        'CREATE TABLE cursor_minutes (account TEXT NOT NULL, kind TEXT NOT NULL, t INTEGER NOT NULL,'
        ' PRIMARY KEY (account, kind, t)) WITHOUT ROWID',
        'CREATE TABLE focus (app TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL)',
        'CREATE INDEX focus_app ON focus (app, start)',
        'CREATE INDEX focus_end ON focus (end)',
    ),
)

UPSERT_REQUEST_SLOT = (
    'INSERT INTO request_slots VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (slot, provider) DO UPDATE SET'
    ' n = n + excluded.n, unpriced = unpriced + excluded.unpriced, cents = cents + excluded.cents,'
    ' input = input + excluded.input, output = output + excluded.output,'
    ' cache_read = cache_read + excluded.cache_read, cache_write = cache_write + excluded.cache_write')
UPSERT_TOOL_SLOT = 'INSERT INTO tool_slots VALUES (?,?,?) ON CONFLICT (slot, grp) DO UPDATE SET n = n + excluded.n'

# Reader state carried from one read of a log to the next.
STATE_KEYS = ('last_t', 'last_reply', 'previous', 'model')


class LedgerError(RuntimeError):
    pass


def db_path() -> Path:
    return cu.hud_dir() / DB_NAME


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _count(value):
    """A token count as logged; anything that is not a number counts as none."""
    return value if _finite(value) else 0


def slot_of(t) -> int:
    return int(t // SLOT)


# ── how a log is read ─────────────────────────────────────────────────────
# The reading rules the Overview has always had, unchanged: what counts as a
# request, a typed prompt, a tool call and busy time.

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
    seen = facts['seen']
    fresh = facts['fresh']

    def once(key):
        """One request is logged once per content block, and a rewound session
        logs earlier records again: count each id once. No id, no repeat."""
        if key is None:
            return True
        key = _id_digest(key)
        if key in seen:
            return False
        seen.add(key)
        fresh.append(key)
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
                                          usage.get('cache_creation_input_tokens') or 0,
                                          model if isinstance(model, str) else None))
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
            # A subagent's prompts come from its parent agent, not from you,
            # including any an earlier read of this log stored.
            facts['prompts'].clear()
            facts['cleared'] = True
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
            model = facts.get('model')
            facts['requests'].append((at, cents, max(0, delta[0] - delta[1]), delta[2], delta[1], 0,
                                      model if isinstance(model, str) else None))
        return t
    return read


# Big Claude records are almost all tool output: the tool results a user
# record carries and the attachments Claude Code logs. Reading needs only
# their time and session from them, so they are skimmed: the small keys
# around the bulk are parsed, and the parse itself proves they are the
# record's own top-level keys (a cut inside a nested value leaves brackets
# that do not balance, and the parse fails). Anything that does not skim
# cleanly is parsed whole, so the rules above see the same records either way:
# a line whose opening key comes again (a record cut short with the next one
# written straight after it, which a whole parse rejects) among them.
_TOOL_RESULT_FIRST = re.compile(rb'\{"role":"user","content":\[\{"tool_use_id":"[^"\\]*","type":"tool_result"')


def _object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _skim(line):
    """The session and time of a big record that holds nothing else the
    readers count, or None to parse it whole."""
    if not line.startswith(b'{"') or not line.endswith(b'\n'):
        return None
    opening = line.find(b'":', 2)
    if opening < 0 or line.find(line[:opening + 2], 1) >= 0:
        return None
    cut = line.find(b'"message":', 0, SKIM_HEAD)
    if cut > 0 and _TOOL_RESULT_FIRST.match(line, cut + 10):
        # A tool result goes back to the agent: never a prompt you typed, as
        # long as its first part is the result and nothing says a human sent it.
        if b'"human"' in line:
            return None
        head = _object(line[:cut] + b'"":0}')
        start = line.rfind(b',"uuid":"')
        tail = _object(b'{' + line[start + 1:]) if start > cut else None
        if head is None or tail is None or tail.get('type', head.get('type')) != 'user':
            return None
    else:
        # Attachments and the like say what they are after their bulk.
        start = line.rfind(b',"type":"')
        tail = _object(b'{' + line[start + 1:]) if start > 0 else None
        if tail is None or tail.get('type') in (None, 'assistant', 'user'):
            return None
    if 'timestamp' not in tail or 'sessionId' not in tail:
        return None
    return {'sessionId': tail['sessionId'], 'timestamp': tail['timestamp']}


def _read(path, facts, reader, skim=False):
    """Read what the log gained since the last look. Logs only grow, so a big
    live one costs only its new lines; a line still being written waits.
    Returns the moments the new records were logged at."""
    times = []
    with path.open('rb') as stream:
        stream.seek(facts['offset'])
        for line in stream:
            record = _skim(line) if skim and len(line) > SKIM_OVER else None
            if record is None:
                try:
                    record = json.loads(line.decode('utf-8', 'replace'))
                except (ValueError, RecursionError):  # nested too deep is as unreadable as broken
                    if not line.endswith(b'\n'):
                        break
                    record = None
            facts['offset'] += len(line)
            if isinstance(record, dict):
                t = reader(record)
                if t is not None:
                    times.append(t)
    return times


def read_log(provider, path, offset=0, kind=None, state=None, seen=b''):
    """What one log says past `offset`, given the reader state and the ids
    (8-byte digests) an earlier read of it left. Runs on worker processes, so
    everything in and out is plain data."""
    path = Path(path)
    facts = {'kind': kind or ('subagent' if provider == 'claude' and 'subagents' in path.parts else 'session'),
             'offset': offset, 'requests': [], 'prompts': [], 'tools': [], 'cleared': False,
             'seen': {seen[i:i + 8] for i in range(0, len(seen), 8)}, 'fresh': []}
    for key, value in (state or {}).items():
        if key in STATE_KEYS and value is not None:
            facts[key] = tuple(value) if key == 'previous' else value
    reader = (_claude_reader if provider == 'claude' else _codex_reader)(path, facts)
    times = _read(path, facts, reader, skim=provider == 'claude')
    return {'offset': facts['offset'], 'kind': facts['kind'], 'cleared': facts['cleared'],
            'state': {key: facts[key] for key in STATE_KEYS if facts.get(key) is not None},
            'requests': facts['requests'], 'prompts': facts['prompts'], 'tools': facts['tools'],
            'intervals': _intervals(times), 'seen': b''.join(facts['fresh'])}


def _read_job(job):
    """read_log on a worker process; a log gone or locked comes back as an error."""
    try:
        return read_log(*job)
    except OSError as exc:
        return {'error': type(exc).__name__}


def _worker_start(engine):
    """Workers give way to whatever you are doing on the machine, and end
    with the engine that started them."""
    try:
        if sys.platform == 'win32':
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass
    threading.Thread(target=_end_with, args=(engine,), name='ledger-worker-watch', daemon=True).start()


def _end_with(engine):
    """The app ends the engine by killing it, and a killed engine leaves its
    workers waiting on a queue they hold both ends of; left alone they would
    outlive it (and hold its folder open against the next update)."""
    if sys.platform == 'win32':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        handle = kernel.OpenProcess(0x00100000, False, engine)  # SYNCHRONIZE; None once it is gone
        if handle:
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
    else:
        while os.getppid() == engine:
            time.sleep(1)
    os._exit(0)


# ── the ledger ────────────────────────────────────────────────────────────

class _Log:
    """A logs row."""
    __slots__ = ('id', 'kind', 'size', 'mtime_ns', 'offset', 'state')

    def __init__(self, row):
        self.id, self.kind, self.size, self.mtime_ns, self.offset, self.state = row


def _merge(spans, gap=0.0):
    """Spans as sorted, merged (start, end) pairs; spans closer than `gap` join."""
    out = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


class Ledger:
    """One ledger file. Writes go through one connection, one transaction at
    a time; reads each take an idle connection of their own, and WAL lets
    them read a consistent snapshot while a write is under way."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._idle = queue.SimpleQueue()
        # One ingest (or catch-up) at a time.
        self._ingesting = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._starting = threading.Lock()
        self._pool = None
        self._hot = {}
        self._status_lock = threading.Lock()
        # Whether a scan has run, the bytes of every log and how many are in
        # the ledger, and the never-read logs' modification times.
        self._status = {'scanned': False, 'read': 0, 'total': 0, 'unread': {}}
        self._con = self._connect()
        try:
            self._migrate()
            self._import_cursor_activity()
        except BaseException:
            self._con.close()
            raise
        self._forget_old_cache()

    # connections and transactions

    def _connect(self, query_only=False):
        con = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        if query_only:
            con.execute('PRAGMA query_only = ON')
        else:
            con.execute('PRAGMA journal_mode = WAL')
            con.execute('PRAGMA synchronous = NORMAL')
        return con

    @contextmanager
    def _writing(self):
        with self._lock:
            self._con.execute('BEGIN IMMEDIATE')
            try:
                yield self._con
            except BaseException:
                self._con.execute('ROLLBACK')
                raise
            self._con.execute('COMMIT')

    @contextmanager
    def reading(self):
        """A read transaction: every query in it sees the same moment."""
        try:
            con = self._idle.get_nowait()
        except queue.Empty:
            con = self._connect(query_only=True)
        ok = False
        try:
            con.execute('BEGIN')
            yield con
            ok = True
        finally:
            try:
                con.execute('COMMIT')
            except sqlite3.Error:
                ok = False
            if ok:
                self._idle.put(con)
            else:
                con.close()

    def close(self):
        self.stop()
        with self._lock:
            self._con.close()
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break

    def _migrate(self):
        with self._lock:
            for number, statements in enumerate(SCHEMA, 1):
                self._con.execute('BEGIN IMMEDIATE')
                try:
                    version = self._con.execute('PRAGMA user_version').fetchone()[0]
                    if version > len(SCHEMA):
                        raise LedgerError(f'{self.path.name} was written by a newer TOKENMAXXING '
                                          f'(layout {version}); update the app to read it.')
                    if version < number:
                        for statement in statements:
                            self._con.execute(statement)
                        self._con.execute(f'PRAGMA user_version = {number}')
                except BaseException:
                    self._con.execute('ROLLBACK')
                    raise
                self._con.execute('COMMIT')

    def _import_cursor_activity(self):
        """Take in, once, the minutes the JSON store before the ledger kept
        (cursor-activity.json, beside this file)."""
        source = self.path.parent / 'cursor-activity.json'
        with self._writing() as con:
            if con.execute("SELECT 1 FROM meta WHERE key = 'cursor_activity_imported'").fetchone():
                return
            try:
                store = json.loads(source.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                store = None
            rows = []
            if isinstance(store, dict) and store.get('schema_version') == 1 and isinstance(store.get('email'), str):
                account = store['email'].strip().casefold()
                minutes = store.get('minutes') if isinstance(store.get('minutes'), dict) else {}
                for kind in CURSOR_KINDS:
                    times = minutes.get(kind)
                    if account and isinstance(times, list):
                        rows += [(account, kind, int(t)) for t in times if _finite(t)]
            con.executemany('INSERT OR IGNORE INTO cursor_minutes VALUES (?, ?, ?)', rows)
            con.execute("INSERT INTO meta VALUES ('cursor_activity_imported', ?)", (str(len(rows)),))

    def _forget_old_cache(self):
        """overview-facts.json, the cache the ledger replaces, is dead weight."""
        folder = self.path.parent
        for stale in [folder / 'overview-facts.json', *folder.glob('.overview-facts-*.tmp')]:
            try:
                stale.unlink()
            except OSError:
                pass

    # reading the logs

    def _scan(self):
        """Every Claude Code and Codex log, with its size and modification time."""
        out = []
        for provider in ('claude', 'codex'):
            root = su.session_root(provider)
            if not root.exists():
                continue
            for path in root.rglob('*.jsonl'):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                out.append((provider, str(path), stat.st_size, stat.st_mtime_ns))
        return out

    def ingest(self, parallel=True):
        """Read what every log gained since the last look, newest first."""
        with self._ingesting:
            self._ingest(parallel)

    def _ingest(self, parallel):
        scanned = self._scan()
        with self.reading() as con:
            known = {(provider, path): _Log(rest) for provider, path, *rest in con.execute(
                'SELECT provider, path, id, kind, size, mtime_ns, offset, state FROM logs')}
        todo, unread, hot = [], {}, {}
        total = done = 0
        recent = time.time() - HOT_FOR
        for provider, path, size, mtime_ns in scanned:
            row = known.get((provider, path))
            total += size
            if mtime_ns / 1e9 >= recent:
                hot[(provider, path)] = mtime_ns
            if row is not None:
                done += min(row.offset, size)
                if (row.size, row.mtime_ns) == (size, mtime_ns):
                    continue
            else:
                unread[(provider, path)] = mtime_ns / 1e9
            todo.append((provider, path, size, mtime_ns, row))
        todo.sort(key=lambda item: -item[3])
        self._hot = dict(sorted(hot.items(), key=lambda item: -item[1])[:HOT_MAX])
        with self._status_lock:
            self._status.update(scanned=True, read=done, total=total, unread=unread)
        pending = sum(size - (row.offset if row is not None and size >= row.offset else 0)
                      for _, _, size, _, row in todo)
        if parallel and pending >= POOL_AFTER and len(todo) > 1:
            todo = self._read_in_pool(todo)
        for item in todo:
            if self._stop.is_set():
                return
            self._read_one(*item)

    def _job(self, provider, path, size, mtime_ns, row):
        """read_log's arguments for this log: from the start for a new or
        truncated one, else from where the last read stopped."""
        if row is None or size < row.offset:
            return (provider, path, 0, None, None, b'')
        with self.reading() as con:
            seen = b''.join(ids for ids, in con.execute('SELECT ids FROM seen WHERE log = ? ORDER BY rowid',
                                                         (row.id,)))
        return (provider, path, row.offset, row.kind, json.loads(row.state), seen)

    def _read_one(self, provider, path, size, mtime_ns, row):
        try:
            result = read_log(*self._job(provider, path, size, mtime_ns, row))
        except OSError:
            return
        except Exception as exc:
            # One log the readers choke on must not hold up every log after it.
            sys.stderr.write(f'ledger: a {provider} log could not be read ({type(exc).__name__})\n')
            return
        self._store(provider, path, size, mtime_ns, row, result)

    def _read_in_pool(self, todo):
        """Read these logs on worker processes, storing each as it comes back.
        Returns the logs left to read on this thread: all of them if no
        worker process would start, the rest if the workers stop."""
        workers = max(1, min((os.cpu_count() or 2) - 1, MAX_WORKERS, len(todo)))
        done = set()
        try:
            pool = concurrent.futures.ProcessPoolExecutor(
                workers, mp_context=multiprocessing.get_context('spawn'), initializer=_worker_start,
                initargs=(os.getpid(),))
        except (OSError, ValueError, ImportError, NotImplementedError) as exc:
            sys.stderr.write(f'ledger: no worker processes ({type(exc).__name__}); reading on one thread\n')
            return todo
        self._pool = pool
        try:
            futures = {pool.submit(_read_job, self._job(*item)): index for index, item in enumerate(todo)}
            for future in concurrent.futures.as_completed(futures):
                if self._stop.is_set():
                    break
                index = futures[future]
                try:
                    result = future.result()
                except concurrent.futures.process.BrokenProcessPool:
                    raise
                except Exception:
                    # One log the readers choke on waits for the next pass.
                    traceback.print_exc(file=sys.stderr)
                    done.add(index)
                    continue
                done.add(index)
                if 'error' not in result:
                    self._store(*todo[index], result)
        except (concurrent.futures.process.BrokenProcessPool, OSError, RuntimeError) as exc:
            sys.stderr.write(f'ledger: worker processes stopped ({type(exc).__name__}); reading on one thread\n')
        finally:
            self._pool = None
            pool.shutdown(wait=False, cancel_futures=True)
        return [item for index, item in enumerate(todo) if index not in done]

    def _store(self, provider, path, size, mtime_ns, row, result):
        """What one read of a log found, in one transaction with how far it
        got. Stored only if the log is still where the read began: another
        engine on the same ledger (the installed app and a source run, say)
        may have stored that stretch already, and it must not count twice."""
        reset = row is not None and size < row.offset
        requests = [(t, cents, _count(a), _count(b), _count(c), _count(d), model)
                    for t, cents, a, b, c, d, model in result['requests']]
        with self._writing() as con:
            current = con.execute('SELECT id, offset, size, mtime_ns FROM logs WHERE provider = ? AND path = ?',
                                  (provider, path)).fetchone()
            if current != (None if row is None else (row.id, row.offset, row.size, row.mtime_ns)):
                return
            if row is None:
                log = con.execute('INSERT INTO logs (provider, path, kind, size, mtime_ns, offset, state)'
                                  ' VALUES (?, ?, ?, 0, 0, 0, ?)', (provider, path, result['kind'], '{}')).lastrowid
            else:
                log = row.id
                if reset:
                    self._forget_log(con, log)
                elif result['cleared']:
                    con.execute('DELETE FROM prompts WHERE log = ?', (log,))
            con.executemany('INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            [(log, provider, *request) for request in requests])
            con.executemany(UPSERT_REQUEST_SLOT, self._request_slots(provider, requests))
            con.executemany('INSERT INTO prompts VALUES (?, ?, ?)', [(log, t, think) for t, think in result['prompts']])
            con.executemany('INSERT INTO tools VALUES (?, ?, ?)', [(log, t, group) for t, group in result['tools']])
            tool_slots = collections.Counter((slot_of(t), group) for t, group in result['tools'])
            con.executemany(UPSERT_TOOL_SLOT, [(slot, group, n) for (slot, group), n in tool_slots.items()])
            intervals = result['intervals']
            if row is not None and not reset and intervals:
                earlier = con.execute('SELECT start, end FROM busy WHERE log = ?', (log,)).fetchall()
                intervals = _intervals((), earlier + [tuple(span) for span in intervals])
                con.execute('DELETE FROM busy WHERE log = ?', (log,))
            con.executemany('INSERT INTO busy VALUES (?, ?, ?)', [(log, s, e) for s, e in intervals])
            if result['seen']:
                con.execute('INSERT INTO seen VALUES (?, ?)', (log, result['seen']))
            con.execute('UPDATE logs SET kind = ?, size = ?, mtime_ns = ?, offset = ?, state = ? WHERE id = ?',
                        (result['kind'], size, mtime_ns, result['offset'], json.dumps(result['state']), log))
        with self._status_lock:
            status = self._status
            status['read'] += result['offset'] - (0 if row is None else min(row.offset, size))
            status['unread'].pop((provider, path), None)

    @staticmethod
    def _request_slots(provider, requests):
        slots = {}
        for t, cents, a, b, c, d, _ in requests:
            slot = slots.setdefault(slot_of(t), [0, 0, 0.0, 0, 0, 0, 0])
            slot[0] += 1
            slot[1] += cents is None
            slot[2] += cents or 0
            slot[3] += a
            slot[4] += b
            slot[5] += c
            slot[6] += d
        return [(slot, provider, *values) for slot, values in slots.items()]

    def _forget_log(self, con, log):
        """A log cut shorter than what was read is read again from the start:
        what it said before goes, from the sums too."""
        rows = con.execute('SELECT provider, t, cents, input, output, cache_read, cache_write, model'
                           ' FROM requests WHERE log = ?', (log,)).fetchall()
        if rows:
            slots = self._request_slots(rows[0][0], [row[1:] for row in rows])
            con.executemany(UPSERT_REQUEST_SLOT, [(slot, provider, *(-v for v in values))
                                                  for slot, provider, *values in slots])
            con.execute('DELETE FROM request_slots WHERE n <= 0')
        tools = collections.Counter((slot_of(t), group) for t, group in
                                    con.execute('SELECT t, grp FROM tools WHERE log = ?', (log,)))
        con.executemany(UPSERT_TOOL_SLOT, [(slot, group, -n) for (slot, group), n in tools.items()])
        con.execute('DELETE FROM tool_slots WHERE n <= 0')
        for table in ('requests', 'prompts', 'tools', 'busy', 'seen'):
            con.execute(f'DELETE FROM {table} WHERE log = ?', (log,))

    def catch_up(self, seconds=CATCH_UP_SECONDS):
        """Read what the logs written to in the last few minutes gained, so an
        Overview counts the last seconds too. Skipped while an ingest runs:
        its figures land as it goes, and the next Overview has them."""
        if not self._hot or not self._ingesting.acquire(blocking=False):
            return
        try:
            deadline = time.monotonic() + seconds
            for provider, path in list(self._hot):
                if time.monotonic() > deadline:
                    break
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                with self.reading() as con:
                    found = con.execute('SELECT id, kind, size, mtime_ns, offset, state FROM logs'
                                        ' WHERE provider = ? AND path = ?', (provider, path)).fetchone()
                row = _Log(found) if found else None
                if row is None or (row.size, row.mtime_ns) == (stat.st_size, stat.st_mtime_ns) \
                        or not 0 <= stat.st_size - row.offset <= CATCH_UP_BYTES:
                    continue
                self._read_one(provider, path, stat.st_size, stat.st_mtime_ns, row)
        except Exception:
            # The background thread reads it soon enough; the Overview answers now.
            traceback.print_exc(file=sys.stderr)
        finally:
            self._ingesting.release()

    # the background thread

    def start(self):
        """Backfill and then follow the logs on a background thread; once.
        Every Overview calls this, so the usual case takes no lock."""
        if self._thread is not None:
            return
        with self._starting:
            if self._thread is None:
                self._thread = threading.Thread(target=self._follow, name='ledger-ingest', daemon=True)
                self._thread.start()

    def _follow(self):
        while not self._stop.is_set():
            try:
                self.ingest()
            except Exception:
                traceback.print_exc(file=sys.stderr)
            self._stop.wait(INGEST_EVERY)

    def stop(self, timeout=2.0):
        self._stop.set()
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def backfill(self):
        """How far reading every log has got. complete_from is the moment from
        which every log is in the ledger: logs are read newest first, so
        everything after the newest one still unread is complete."""
        with self._status_lock:
            status = dict(self._status)
            unread = list(status['unread'].values())
        with self.reading() as con:
            any_read = con.execute('SELECT 1 FROM logs LIMIT 1').fetchone() is not None
            earliest = con.execute('SELECT min(start) FROM busy').fetchone()[0] if not unread else None
        done = not unread if status['scanned'] else any_read
        return {'done': done, 'read_bytes': status['read'], 'total_bytes': max(status['read'], status['total']),
                'complete_from': (earliest if done else max(unread) if any_read else None)}

    # focus spans

    def record_focus(self, spans, now=None):
        """Store the spans an AI app had the focus, merged with what is stored.
        Bad spans are dropped; returns how many were good."""
        now = time.time() if now is None else now
        good = []
        for span in spans if isinstance(spans, list) else []:
            if not isinstance(span, dict):
                continue
            app, start, end = span.get('app'), span.get('start'), span.get('end')
            if (isinstance(app, str) and app in FOCUS_APPS and _finite(start) and _finite(end)
                    and 0 < start < end <= now + FOCUS_FUTURE_SLACK and end - start <= FOCUS_MAX_SPAN):
                good.append((app, float(start), float(end)))
        if good:
            with self._writing() as con:
                for app, start, end in good:
                    near = con.execute('SELECT rowid, start, end FROM focus WHERE app = ? AND start <= ? AND end >= ?',
                                       (app, end + FOCUS_MERGE_GAP, start - FOCUS_MERGE_GAP)).fetchall()
                    con.executemany('DELETE FROM focus WHERE rowid = ?', [(rowid,) for rowid, _, _ in near])
                    con.execute('INSERT INTO focus VALUES (?, ?, ?)',
                                (app, min([start] + [s for _, s, _ in near]), max([end] + [e for _, _, e in near])))
        return len(good)

    # Cursor

    def record_cursor(self, account, events=(), minutes=(), covered=None):
        """Store Cursor dashboard events for an account, once each by key: rows
        of (key, t, cents, input, output, cache_read, cache_write, kind, model).
        `minutes` are (kind, minute) pairs; `covered` is a (start, end) stretch
        a fetch saw every event of."""
        with self._writing() as con:
            con.executemany('INSERT OR IGNORE INTO cursor_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            [(key, account, *rest) for key, *rest in events])
            con.executemany('INSERT OR IGNORE INTO cursor_minutes VALUES (?, ?, ?)',
                            [(account, kind, int(t)) for kind, t in minutes])
            if covered is not None and covered[0] < covered[1]:
                start, end = covered
                near = con.execute('SELECT rowid, start, end FROM cursor_coverage WHERE account = ? AND start <= ?'
                                   ' AND end >= ?', (account, end, start)).fetchall()
                con.executemany('DELETE FROM cursor_coverage WHERE rowid = ?', [(rowid,) for rowid, _, _ in near])
                con.execute('INSERT INTO cursor_coverage VALUES (?, ?, ?)',
                            (account, min([start] + [s for _, s, _ in near]), max([end] + [e for _, _, e in near])))

    def cursor_minutes(self, account, since=None):
        """{kind: sorted minutes} for an account, or None if none are stored."""
        with self.reading() as con:
            rows = con.execute('SELECT kind, t FROM cursor_minutes WHERE account = ? AND t >= ? ORDER BY t',
                               (account, -math.inf if since is None else since)).fetchall()
        if not rows:
            return None
        out = {kind: [] for kind in CURSOR_KINDS}
        for kind, t in rows:
            out.setdefault(kind, []).append(t)
        return out

    # the Overview's facts

    def overview_facts(self, since, account=None):
        """Everything the Overview counts from `since` on, read in one snapshot."""
        first = slot_of(since)
        whole = first if since == first * SLOT else first + 1
        with self.reading() as con:
            requests = con.execute('SELECT provider, slot * ?, n, unpriced, cents, input, output, cache_read,'
                                   ' cache_write FROM request_slots WHERE slot >= ?', (SLOT, whole)).fetchall()
            tools = collections.Counter(dict(con.execute(
                'SELECT grp, sum(n) FROM tool_slots WHERE slot >= ? GROUP BY grp', (whole,)).fetchall()))
            if whole != first:
                # The quarter hour the range starts inside, request by request.
                edge = whole * SLOT
                requests += [(provider, t, 1, cents is None, cents or 0, a, b, c, d) for provider, t, cents, a, b, c, d in
                             con.execute('SELECT provider, t, cents, input, output, cache_read, cache_write FROM requests'
                                         ' WHERE t >= ? AND t < ?', (since, edge))]
                tools.update(group for group, in con.execute('SELECT grp FROM tools WHERE t >= ? AND t < ?', (since, edge)))
            facts = {
                'requests': requests,
                'tools': tools,
                'spans': con.execute('SELECT busy.log, logs.provider, logs.kind, busy.start, busy.end FROM busy'
                                     ' JOIN logs ON logs.id = busy.log WHERE busy.end >= ?', (since,)).fetchall(),
                'prompts': con.execute('SELECT t, think FROM prompts WHERE t >= ? ORDER BY t', (since,)).fetchall(),
                'focus': con.execute('SELECT app, start, end FROM focus WHERE end >= ?', (since,)).fetchall(),
                'focus_since': con.execute('SELECT min(start) FROM focus').fetchone()[0],
                'cursor': None,
            }
            if account:
                facts['cursor'] = {
                    'events': con.execute('SELECT t, cents, input, output, cache_read, cache_write FROM cursor_events'
                                          ' WHERE account = ? AND t >= ?', (account, since)).fetchall(),
                    'coverage': con.execute('SELECT start, end FROM cursor_coverage WHERE account = ?',
                                            (account,)).fetchall(),
                    'minutes': con.execute('SELECT kind, t FROM cursor_minutes WHERE account = ? AND t >= ?',
                                           (account, since)).fetchall(),
                }
        return facts

    def size(self):
        """The ledger's bytes on disk, its write-ahead log included."""
        total = 0
        for suffix in ('', '-wal'):
            try:
                total += os.path.getsize(f'{self.path}{suffix}')
            except OSError:
                pass
        return total


# ── the engine's ledger ───────────────────────────────────────────────────

_default = None
_default_lock = threading.Lock()


def default() -> Ledger:
    """The ledger in the HUD folder, opened once (again if the folder moves)."""
    global _default
    path = db_path()
    with _default_lock:
        if _default is None or _default.path != path:
            if _default is not None:
                _default.close()
            _default = Ledger(path)
        return _default


def start():
    """Start following the logs in the background; harmless to call again."""
    default().start()


def stop():
    with _default_lock:
        ledger = _default
    if ledger is not None:
        ledger.stop()


def close():
    """Stop and close the engine's ledger; the next default() opens it afresh."""
    global _default
    with _default_lock:
        ledger, _default = _default, None
    if ledger is not None:
        ledger.close()
