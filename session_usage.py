"""Read-only, bounded local session metadata. Never return transcript content."""
import json
import math
import os
import threading
import time
from pathlib import Path
import cursor_usage as cu
import session_cost

HEAD_BYTES = 128 * 1024
TAIL_BYTES = 2 * 1024 * 1024
LIST_LIMIT = 100
# A status line reports the chat a person is actually typing in. Anything
# older than this is treated as unknown rather than followed.
POINTER_MAX_AGE = 600
# Without a status line (the Claude desktop app never runs one), the Claude
# Code log written to most recently is the session in use. A log idle for
# longer than this is background history, not an open chat.
RECENT_LOG_SECONDS = 900

_lock = threading.Lock()
_cache = {}
# Parsed sessions keyed by (path, mtime_ns, size): an unchanged log is never
# re-read, so a two-second poll costs one parse of the log being written.
_parsed = {}
_parsed_lock = threading.Lock()


def numeric(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _load(line):
    try:
        item = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    return item if isinstance(item, dict) else None


def read_records(path, full=True):
    """Head record plus a bounded tail; head only when a summary is enough."""
    with path.open('rb') as stream:
        head = stream.readline(HEAD_BYTES)
        if not full:
            return [item for item in (_load(head),) if item is not None]
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - TAIL_BYTES)
        stream.seek(start)
        if start:
            stream.readline()
        tail = stream.read(TAIL_BYTES)
    records = []
    for line in ([head] if start else []) + tail.splitlines():
        item = _load(line)
        if item is not None:
            records.append(item)
    return records


def parse_session(provider, path, modified, full=True):
    records = read_records(path, full)
    result = dict(id=path.stem, name=path.stem[-12:], model=None, updated_at=modified,
                  usage_at=None, used=None, limit=None, pct=None, cats={}, cat_rows=[],
                  metrics={}, source=f'{provider} local session log', provider=provider)
    for record in records:
        if provider == 'codex':
            payload = record.get('payload') or {}
            if record.get('type') == 'session_meta':
                source = payload.get('source')
                if isinstance(source, dict) and 'subagent' in source: return None
                result['id'] = payload.get('id') or payload.get('session_id') or result['id']
                cwd = str(payload.get('cwd') or '').replace('\\', '/')
                result['name'] = f"{cwd.rstrip('/').split('/')[-1] or 'Codex'} · {str(result['id'])[:8]}"
            elif record.get('type') == 'turn_context':
                result['model'] = payload.get('model')
            elif record.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict): continue
                last = info.get('last_token_usage') or {}
                total = info.get('total_token_usage') or {}
                result['usage_at'] = record.get('timestamp')
                result['used'] = numeric(last.get('input_tokens'))
                result['limit'] = numeric(info.get('model_context_window'))
                result['metrics'] = {'Last request input':numeric(last.get('input_tokens')),
                    'Last request cached input (included in input)':numeric(last.get('cached_input_tokens')),
                    'Last request output':numeric(last.get('output_tokens')),
                    'Recorded cumulative tokens':numeric(total.get('total_tokens'))}
        elif provider == 'claude':
            if record.get('isSidechain'): continue
            if record.get('sessionId'): result['id'] = record['sessionId']
            cwd = str(record.get('cwd') or '').replace('\\', '/')
            if cwd: result['name'] = f"{cwd.rstrip('/').split('/')[-1]} · {str(result['id'])[:8]}"
            message = record.get('message') or {}
            if record.get('type') != 'assistant' or record.get('isApiErrorMessage') or message.get('model') == '<synthetic>': continue
            usage = message.get('usage')
            if not isinstance(usage, dict): continue
            result['model'] = message.get('model')
            result['usage_at'] = record.get('timestamp')
            result['metrics'] = {label:numeric(usage.get(key)) for label,key in [
                ('Last request uncached input','input_tokens'), ('Last request cache read','cache_read_input_tokens'),
                ('Last request cache write','cache_creation_input_tokens'), ('Last request output','output_tokens')]}
            # These disjoint input fields describe one recorded request, not
            # the current live context or a sum of repeated streaming records.
            inputs = [numeric(usage.get(k)) for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')]
            result['used'] = sum(inputs) if all(v is not None for v in inputs) else None
    result['measurement'] = 'Last recorded request input; not a live context reading.'
    return result


def parse_cached(provider, path, modified, full=True):
    """Parse once per (file, size, mtime); polling an idle log costs nothing."""
    try:
        stat = path.stat()
        key = (provider, str(path), stat.st_mtime_ns, stat.st_size, full)
    except OSError:
        return None
    with _parsed_lock:
        if key in _parsed:
            cached = _parsed[key]
            return dict(cached) if cached else cached
    session = parse_session(provider, path, modified, full)
    with _parsed_lock:
        if len(_parsed) >= 300:
            _parsed.clear()
        _parsed[key] = session
    return dict(session) if session else session


def claude_titles():
    """Titles of live Claude Code sessions, from the per-process registry the
    CLI keeps under its config folder. Identifiers and names only."""
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))/'sessions'
    titles, stamps = {}, {}
    try:
        files = list(root.glob('*.json'))
    except OSError:
        return titles
    for path in files:
        try:
            if path.stat().st_size > 64 * 1024: continue
            row = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError): continue
        if not isinstance(row, dict): continue
        ident, name = row.get('sessionId'), row.get('name')
        if not isinstance(ident, str) or not isinstance(name, str) or not name: continue
        stamp = numeric(row.get('updatedAt')) or 0
        if ident not in titles or stamp >= stamps[ident]:
            titles[ident], stamps[ident] = name, stamp
    return titles


def codex_titles():
    path = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'session_index.jsonl'
    try:
        return {row['id']: row['thread_name'] for row in read_records(path)
                if isinstance(row.get('id'), str) and isinstance(row.get('thread_name'), str)}
    except (OSError, ValueError):
        return {}


def hud_home():
    return Path(os.environ.get('TOKENMAXXING_HOME') or Path.home()/'.tokenmaxxing')


def active_pointer(provider, now=None):
    """Read the status-line pointer for the chat a person is typing in.

    Written by ``cli_statusline.py`` from Claude Code's own status-line input.
    It carries identifiers and counters only, never transcript content.
    """
    if provider != 'claude':
        return None
    try:
        data = json.loads((hud_home()/'claude-active.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('session_id'), str):
        return None
    stamp = numeric(data.get('at'))
    if stamp is None or (now or time.time()) - stamp > POINTER_MAX_AGE:
        return None
    return data


def session_root(provider):
    return (Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'sessions' if provider == 'codex'
            else Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))/'projects')


def file_sessions(provider, selected_id=None):
    """List recent logs from their head record only; the tail is read once a
    session is selected, so the list stays cheap with large logs."""
    entries = []
    for path in session_root(provider).rglob('*.jsonl'):
        if 'subagents' in path.parts or path.name.startswith('agent-'): continue
        try: entries.append((path.stat().st_mtime, path))
        except OSError: continue
    sessions = []
    ordered = sorted(entries, key=lambda item:item[0], reverse=True)
    chosen = ordered[:LIST_LIMIT]
    if selected_id:
        chosen += [entry for entry in ordered[LIST_LIMIT:] if entry[1].stem.endswith(selected_id)]
    for modified,path in chosen:
        try:
            session = parse_cached(provider,path,modified,full=False)
            if session:
                session['_path'] = str(path)
                sessions.append(session)
        except (OSError, ValueError, TypeError): continue
    return sessions


def cursor_sessions():
    if not cu.cursor_state_db().exists(): return []
    con = cu.connect()
    try:
        rows = con.execute("""SELECT h.composerId, h.recency FROM composerHeaders h
            JOIN cursorDiskKV d ON d.key = 'composerData:' || h.composerId
            WHERE COALESCE(h.isArchived,0)=0 AND COALESCE(h.isSubagent,0)=0
              AND COALESCE(json_extract(h.value,'$.isDraft'),0)=0
              AND json_valid(d.value)
            ORDER BY h.recency DESC LIMIT 100""").fetchall()
        sessions = []
        for cid, recency in rows:
            item = cu.snapshot_for(con, cid)
            if item.get('error'): continue
            stamp = numeric(recency)
            item.update(provider='cursor', updated_at=stamp/1000 if stamp and stamp>1e11 else stamp,
                        usage_at=None, measurement='Context snapshot stored by Cursor; may lag the open chat.', metrics={})
            sessions.append(item)
        return sessions
    finally: con.close()


def _detail(provider, selected):
    """Full read of the one selected log, with its recorded usage and cost."""
    path = selected.pop('_path', None)
    if provider == 'cursor' or not path:
        return selected
    detailed = None
    try:
        detailed = parse_cached(provider, Path(path), selected.get('updated_at'), full=True)
    except (OSError, ValueError, TypeError):
        detailed = None
    if detailed:
        detailed.pop('_path', None)
        detailed['name'] = selected.get('name') or detailed.get('name')
        selected = detailed
    if provider in ('codex', 'claude'):
        try:
            selected['cost'] = session_cost.estimate_file(path, provider)
        except (OSError, ValueError, TypeError):
            selected['cost'] = None
    return selected


def _claude_live(selected, pointer):
    """Add the status line's own counters for the chat it points at."""
    if not pointer or pointer.get('session_id') != selected.get('id'):
        return selected
    window = pointer.get('context_window') if isinstance(pointer.get('context_window'), dict) else {}
    limit = numeric(window.get('context_window_size'))
    used = numeric(window.get('total_input_tokens'))
    if limit is not None:
        selected['limit'] = limit
    if used is not None:
        selected['used'] = used
        selected['pct'] = numeric(window.get('used_percentage'))
        selected['usage_at'] = None
        selected['measurement'] = 'Live context reported by the Claude Code status line.'
        selected['source'] = 'Claude Code status line'
        selected['metrics'] = {label: numeric(window.get(key)) for label, key in [
            ('Context input tokens', 'total_input_tokens'),
            ('Output tokens this session', 'total_output_tokens')]}
    cost = numeric(pointer.get('cost_usd'))
    if cost is not None:
        selected['cost'] = {'cents': cost * 100, 'reported': True, 'partial': False,
                            'priced_requests': None, 'last_request_cents': None, 'pricing_date': None,
                            'basis': 'Reported by Claude Code for this session. API-equivalent value, '
                                     'not a charge against a subscription.'}
    selected['model'] = pointer.get('model') or selected.get('model')
    return selected


def get_sessions(provider='codex', pinned=None, follow='latest', active_title=None):
    if provider not in ('codex','claude','cursor'): raise ValueError('Unknown session provider')
    if follow not in ('latest', 'active'): raise ValueError('Unknown follow mode')
    titles = codex_titles() if provider == 'codex' else claude_titles() if provider == 'claude' else {}
    active = provider in ('codex', 'claude') and follow == 'active' and not pinned
    pointer = active_pointer(provider) if active else None
    matches = [ident for ident, title in titles.items() if title == active_title] if active_title else []
    target = pinned
    if target is None and active:
        # The status line names the exact session; the desktop window names
        # the open session's title, which the registry maps back to an id.
        target = pointer.get('session_id') if pointer else (matches[0] if len(matches) == 1 else None)
    key = (provider, target)
    with _lock:
        cached = _cache.get(key)
        if not cached or time.monotonic()-cached[0]>=5:
            sessions = cursor_sessions() if provider == 'cursor' else file_sessions(provider, target)
            if len(_cache) >= 10: _cache.clear()
            _cache[key] = (time.monotonic(),sessions)
        else: sessions=cached[1]
    sessions = [{**s, 'name': f"{titles[s['id']]} · {s['id'][:8]}" if s['id'] in titles else s['name']} for s in sessions]
    selected = next((s for s in sessions if s['id']==target),None) if target else (None if active else next(iter(sessions),None))
    follow_source = ('status line' if pointer else 'open window' if matches else None) if selected and active else None
    if active and provider == 'claude' and not selected and not pointer and sessions:
        newest = sessions[0]
        modified = numeric(newest.get('updated_at'))
        if modified is not None and time.time() - modified <= RECENT_LOG_SECONDS:
            selected, follow_source = newest, 'recent log'
    reason = 'Pinned session is unavailable. Choose another session.' if pinned else 'No local session records found for this provider.'
    if active and not selected:
        if provider == 'claude':
            reason = ('Open Claude Code session has no local log yet. Choose a session manually.' if pointer or matches else
                      'No Claude Code session has been active in the last 15 minutes. Open one, or choose a session manually.')
        else:
            reason = ('More than one local chat has this title. Select the chat manually.' if len(matches) > 1 else
                      'Open chat has no matching local session record. Select a chat manually.' if active_title else
                      'Open chat could not be detected. Open Codex, select a chat, or choose a session manually.')
    if selected:
        selected = _detail(provider, dict(selected))
        if provider == 'claude':
            selected = _claude_live(selected, pointer or active_pointer('claude'))
    return {'available':True, 'provider':provider, 'pinned':pinned,
            'selection_mode': 'pinned' if pinned else 'active' if active else 'latest',
            'follow_source': follow_source,
            'chats':[{'id':s['id'],'name':s['name']} for s in sessions], 'chat':dict(selected) if selected else None,
            'reason':reason}
