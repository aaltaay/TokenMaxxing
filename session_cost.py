"""API-equivalent token value; never an account charge or subscription bill."""
import heapq
import json
import re
import threading
from datetime import datetime
from pathlib import Path

# USD per million tokens, verified 2026-09-20:
# https://developers.openai.com/api/docs/models/gpt-6-astra
RATES = {'gpt-6-astra': (10.0, 1.0, 50.0)}
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')
# Codex bills the whole request at the long-context rate past this many input tokens.
LONG_CONTEXT = 272_000
# Claude, USD per million tokens as (uncached input, cache read, 5-minute
# cache write, output), verified 2026-09-20 against Anthropic's published API
# pricing. A 1-hour cache write is 2x input instead of 1.25x.
CLAUDE_RATES = {
    'claude-fable-5-1': (10.0, 0.25, 12.5, 50.0),
    'claude-mythos-5-1': (10.0, 0.25, 12.5, 50.0),
    'claude-fable-5': (10.0, 1.0, 12.5, 50.0),
    'claude-mythos-5': (10.0, 1.0, 12.5, 50.0),
    'claude-opus-5': (5.0, 0.5, 6.25, 25.0),
    'claude-opus-4-8': (5.0, 0.5, 6.25, 25.0),
    'claude-opus-4-7': (5.0, 0.5, 6.25, 25.0),
    'claude-opus-4-6': (5.0, 0.5, 6.25, 25.0),
    'claude-sonnet-5': (2.0, 0.2, 2.5, 10.0),
    'claude-sonnet-4-6': (3.0, 0.3, 3.75, 15.0),
    'claude-haiku-4-5': (1.0, 0.1, 1.25, 5.0),
}
CLAUDE_FIELDS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
MAX_BYTES = 64 * 1024 * 1024
# The per-request series behind the session graph keeps the most recent
# requests; the priciest ones are kept in full whatever their age.
MAX_SERIES = 1500
TOP_REQUESTS = 8
# A request that pays to put this much fresh context in front of the model,
# and at least half of its input, rebuilt the cache rather than extending it.
REBUILD_TOKENS = 10_000
_cache = {}
# Slim per-log totals for the session list, keyed like _cache but kept for
# many more logs because each entry is a handful of numbers.
_summaries = {}
_summary_lock = threading.Lock()


def claude_rates(model):
    """Rates for a logged model id; dated and long-context variants price the same."""
    if not isinstance(model, str):
        return None
    name = re.sub(r'\s*\[1m\]$', '', model.strip())
    name = re.sub(r'-\d{8}$', '', name)
    return CLAUDE_RATES.get(name)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _epoch(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def claude_request_parts(model, usage):
    """One recorded Claude request, priced per token class as
    [label, tokens, cents] rows. Cache fields absent from a usage record mean
    no cache traffic, which the API reports as a missing key."""
    rates = claude_rates(model)
    if not rates or not isinstance(usage, dict):
        return None
    inp, out = _count(usage.get('input_tokens')), _count(usage.get('output_tokens'))
    read = _count(usage.get('cache_read_input_tokens', 0))
    write = _count(usage.get('cache_creation_input_tokens', 0))
    if None in (inp, out, read, write):
        return None
    breakdown = usage.get('cache_creation')
    hour = _count(breakdown.get('ephemeral_1h_input_tokens', 0)) if isinstance(breakdown, dict) else 0
    hour = min(hour, write) if hour is not None else 0
    return [['Uncached input', inp, inp * rates[0] / 10_000],
            ['Cache read', read, read * rates[1] / 10_000],
            ['Cache write (5-minute)', write - hour, (write - hour) * rates[2] / 10_000],
            ['Cache write (1-hour)', hour, hour * rates[0] * 2 / 10_000],
            ['Output', out, out * rates[3] / 10_000]]


def claude_request_cents(model, usage):
    parts = claude_request_parts(model, usage)
    return sum(row[2] for row in parts) if parts is not None else None


class _Requests:
    """Per-request series and the priciest requests of one log, built while
    the estimator walks it."""

    def __init__(self):
        self.series = []
        self.top = []
        self.count = 0
        self.previous = None

    def add(self, stamp, model, parts, context, long=False):
        cents = sum(row[2] for row in parts)
        fresh = sum(row[1] for row in parts if row[0] == 'Uncached input' or row[0].startswith('Cache write'))
        rebuild = self.count > 0 and fresh >= REBUILD_TOKENS and fresh * 2 >= context
        self.series.append([stamp, round(cents, 4), 1 if rebuild else 0, context])
        if len(self.series) > MAX_SERIES:
            del self.series[:len(self.series) - MAX_SERIES]
        gap = stamp - self.previous[0] if stamp is not None and self.previous and self.previous[0] is not None else None
        detail = {'index': self.count, 'at': stamp, 'cents': cents, 'model': model,
                  'previous_model': self.previous[1] if self.previous else None,
                  'gap': gap if gap is None or gap >= 0 else None, 'context': context,
                  'rebuild': rebuild, 'long_context': long,
                  'parts': [row for row in parts if row[1]]}
        entry = (cents, -self.count, detail)
        if len(self.top) < TOP_REQUESTS:
            heapq.heappush(self.top, entry)
        elif entry > self.top[0]:
            heapq.heapreplace(self.top, entry)
        self.previous = (stamp, model)
        self.count += 1

    def result(self):
        return {'series': self.series, 'series_start': self.count - len(self.series),
                'top_requests': [entry[2] for entry in sorted(self.top, key=lambda e: (-e[0], -e[1]))]}


def estimate_claude_records(records):
    """Price each recorded request once. Claude Code writes one record per
    content block with the same request id and usage; those are one request."""
    cents = 0.0
    priced = skipped = 0
    last_cents = None
    model = None
    seen = set()
    requests = _Requests()
    for record in records:
        if not isinstance(record, dict) or record.get('isSidechain'):
            continue
        message = record.get('message') or {}
        if (record.get('type') != 'assistant' or record.get('isApiErrorMessage')
                or not isinstance(message, dict) or message.get('model') == '<synthetic>'):
            continue
        usage = message.get('usage')
        if not isinstance(usage, dict):
            continue
        key = record.get('requestId') or message.get('id')
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        parts = claude_request_parts(message.get('model'), usage)
        if parts is None:
            skipped += 1
            continue
        request = sum(row[2] for row in parts)
        cents += request
        priced += 1
        last_cents = request
        model = message.get('model')
        requests.add(_epoch(record.get('timestamp')), model, parts, parts[0][1] + parts[1][1] + parts[2][1] + parts[3][1])
    return {'cents': cents if priced else None, 'last_request_cents': last_cents,
            'priced_requests': priced, 'skipped_records': skipped, 'model': model,
            'partial': skipped > 0, 'pricing_date': '2026-09-20', **requests.result(),
            'basis': 'Anthropic API token rates for the recorded model, priced once per request: '
                     'uncached input, cache reads, cache writes and output. API-equivalent value, '
                     'not a charge against a subscription.'}


def counts(usage):
    if not isinstance(usage, dict):
        return None
    values = tuple(usage.get(key) for key in FIELDS)
    if any(type(v) is not int or v < 0 for v in values):
        return None
    return values if values[1] <= values[0] else None


def request_parts(model, usage):
    rates = RATES.get(model) if isinstance(model, str) else None
    values = counts(usage)
    if not rates or values is None:
        return None
    inp, cached, output = values
    long = inp > LONG_CONTEXT
    return [['Uncached input', inp - cached, (inp - cached) * rates[0] * (2 if long else 1) / 10_000],
            ['Cached input', cached, cached * rates[1] * (2 if long else 1) / 10_000],
            ['Output', output, output * rates[2] * (1.5 if long else 1) / 10_000]]


def request_cents(model, usage):
    parts = request_parts(model, usage)
    return sum(row[2] for row in parts) if parts is not None else None


def estimate_records(records):
    model = None
    previous = (0, 0, 0)
    cents = 0.0
    priced = skipped = 0
    last_cents = None
    seen = False
    requests = _Requests()
    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if record.get('type') == 'turn_context':
            model = payload.get('model')
        if record.get('type') != 'event_msg' or payload.get('type') != 'token_count':
            continue
        info = payload.get('info')
        if not isinstance(info, dict):
            continue
        total = counts(info.get('total_token_usage'))
        last = info.get('last_token_usage')
        # Repeated quota updates carry the same cumulative counters.
        if seen and total is not None and total == previous:
            continue
        seen = True
        parts = request_parts(model, last)
        last_cents = sum(row[2] for row in parts) if parts is not None else None
        delta = tuple(a - b for a, b in zip(total, previous)) if total is not None and previous is not None else None
        # Only price an attributable request. Gaps/resets cannot be assigned
        # a model or a long-context rate from a cumulative total alone.
        if last_cents is not None and delta == counts(last):
            cents += last_cents
            priced += 1
            context = counts(last)[0]
            requests.add(_epoch(record.get('timestamp')), model, parts, context, context > LONG_CONTEXT)
        else:
            skipped += 1
        previous = total
    return {'cents': cents if priced else None, 'last_request_cents': last_cents,
            'priced_requests': priced, 'skipped_records': skipped,
            'model': model if priced else None,
            'partial': skipped > 0, 'pricing_date': '2026-09-20', **requests.result(),
            'basis': 'Standard API token rates; excludes Fast mode, cache-write surcharges, tools, taxes and subscription fees. Actual charges are not reported by this log.'}


def estimate_records_for(provider, records):
    return (estimate_claude_records if provider == 'claude' else estimate_records)(records)


def _file_key(path, provider):
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size, provider), stat


def _estimate_path(path, provider, stat):
    incomplete = False

    def records():
        nonlocal incomplete
        with path.open('rb') as stream:
            while stream.tell() < min(stat.st_size, MAX_BYTES):
                line = stream.readline(MAX_BYTES + 1)
                if stream.tell() > MAX_BYTES or not line.endswith(b'\n'):
                    incomplete = True
                    break
                try:
                    yield json.loads(line)
                except (ValueError, UnicodeError):
                    incomplete = True

    result = estimate_records_for(provider, records())
    result['partial'] |= incomplete or stat.st_size > MAX_BYTES
    return result


def _remember(key, result):
    series = result.get('series') or []
    summary = {'cents': result.get('cents'), 'priced_requests': result.get('priced_requests'),
               'partial': result.get('partial'), 'model': result.get('model'),
               'last_at': series[-1][0] if series else None}
    with _summary_lock:
        if len(_summaries) >= 500:
            _summaries.clear()
        _summaries[key] = summary
    return dict(summary)


def estimate_file(path, provider='codex'):
    path = Path(path)
    key, stat = _file_key(path, provider)
    if key in _cache:
        return dict(_cache[key])
    result = _estimate_path(path, provider, stat)
    if len(_cache) >= 10:
        _cache.clear()
    _cache[key] = result
    _remember(key, result)
    return dict(result)


def summarize_file(path, provider='codex'):
    """Totals only, for a session that is listed but not open. Never evicts
    the open session's full estimate."""
    path = Path(path)
    key, stat = _file_key(path, provider)
    with _summary_lock:
        hit = _summaries.get(key)
    if hit is not None:
        return dict(hit)
    return _remember(key, _estimate_path(path, provider, stat))


def cached_summary(path, provider='codex'):
    """Totals for a log if they are already known for its current contents."""
    try:
        key, _ = _file_key(Path(path), provider)
    except OSError:
        return None
    with _summary_lock:
        hit = _summaries.get(key)
    return dict(hit) if hit is not None else None
