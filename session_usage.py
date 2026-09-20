"""Read-only, bounded local session metadata. Never return transcript content."""
import json
import math
import os
import threading
import time
from pathlib import Path
import cursor_usage as cu

_lock = threading.Lock()
_cache = {}


def numeric(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def read_records(path):
    with path.open('rb') as stream:
        head = stream.readline(131072)
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - 2 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        tail = stream.read(2 * 1024 * 1024)
    records = []
    for line in ([head] if start else []) + tail.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict): records.append(item)
        except (ValueError, UnicodeError):
            continue
    return records


def parse_session(provider, path, modified):
    records = read_records(path)
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


def codex_titles():
    path = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'session_index.jsonl'
    try:
        return {row['id']: row['thread_name'] for row in read_records(path)
                if isinstance(row.get('id'), str) and isinstance(row.get('thread_name'), str)}
    except (OSError, ValueError):
        return {}


def file_sessions(provider, selected_id=None):
    root = (Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'sessions' if provider == 'codex'
            else Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))/'projects')
    entries = []
    for path in root.rglob('*.jsonl'):
        if 'subagents' in path.parts or path.name.startswith('agent-'): continue
        try: entries.append((path.stat().st_mtime, path))
        except OSError: continue
    sessions = []
    ordered = sorted(entries, key=lambda item:item[0], reverse=True)
    chosen = ordered[:100]
    if selected_id:
        chosen += [entry for entry in ordered[100:] if entry[1].stem.endswith(selected_id)]
    for modified,path in chosen:
        try:
            session = parse_session(provider,path,modified)
            if session: sessions.append(session)
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


def get_sessions(provider='codex', pinned=None, follow='latest', active_title=None):
    if provider not in ('codex','claude','cursor'): raise ValueError('Unknown session provider')
    if follow not in ('latest', 'active'): raise ValueError('Unknown follow mode')
    titles = codex_titles() if provider == 'codex' else {}
    active = provider == 'codex' and follow == 'active' and not pinned
    matches = [ident for ident, title in titles.items() if title == active_title] if active_title else []
    target = pinned or (matches[0] if active and len(matches) == 1 else None)
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
    reason = 'Pinned session is unavailable. Choose another session.' if pinned else 'No local session records found for this provider.'
    if active and not selected:
        reason = ('More than one local chat has this title. Select the chat manually.' if len(matches) > 1 else
                  'Open chat has no matching local session record. Select a chat manually.' if active_title else
                  'Open chat could not be detected. Open Codex, select a chat, or choose a session manually.')
    return {'available':True, 'provider':provider, 'pinned':pinned,
            'selection_mode': 'pinned' if pinned else 'active' if active else 'latest',
            'chats':[{'id':s['id'],'name':s['name']} for s in sessions], 'chat':dict(selected) if selected else None,
            'reason':reason}
