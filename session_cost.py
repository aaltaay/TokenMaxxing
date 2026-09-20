"""API-equivalent token value; never an account charge or subscription bill."""
import json
import re
from pathlib import Path

# USD per million tokens, verified 2026-09-20:
# https://developers.openai.com/api/docs/models/gpt-6-astra
RATES = {'gpt-6-astra': (10.0, 1.0, 50.0)}
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')
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
_cache = {}


def claude_rates(model):
    """Rates for a logged model id; dated and long-context variants price the same."""
    if not isinstance(model, str):
        return None
    name = re.sub(r'\s*\[1m\]$', '', model.strip())
    name = re.sub(r'-\d{8}$', '', name)
    return CLAUDE_RATES.get(name)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def claude_request_cents(model, usage):
    """One recorded Claude request. Cache fields absent from a usage record
    mean no cache traffic, which the API reports as a missing key."""
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
    return (inp * rates[0] + read * rates[1] + (write - hour) * rates[2]
            + hour * rates[0] * 2 + out * rates[3]) / 10_000


def estimate_claude_records(records):
    """Price each recorded request once. Claude Code writes one record per
    content block with the same request id and usage; those are one request."""
    cents = 0.0
    priced = skipped = 0
    last_cents = None
    seen = set()
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
        request = claude_request_cents(message.get('model'), usage)
        if request is None:
            skipped += 1
            continue
        cents += request
        priced += 1
        last_cents = request
    return {'cents': cents if priced else None, 'last_request_cents': last_cents,
            'priced_requests': priced, 'skipped_records': skipped,
            'partial': skipped > 0, 'pricing_date': '2026-09-20',
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


def request_cents(model, usage):
    rates = RATES.get(model) if isinstance(model, str) else None
    values = counts(usage)
    if not rates or values is None:
        return None
    inp, cached, output = values
    long = inp > 272_000
    return ((inp - cached) * rates[0] * (2 if long else 1)
            + cached * rates[1] * (2 if long else 1)
            + output * rates[2] * (1.5 if long else 1)) / 10_000


def estimate_records(records):
    model = None
    previous = (0, 0, 0)
    cents = 0.0
    priced = skipped = 0
    last_cents = None
    seen = False
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
        last_cents = request_cents(model, last)
        delta = tuple(a - b for a, b in zip(total, previous)) if total is not None and previous is not None else None
        # Only price an attributable request. Gaps/resets cannot be assigned
        # a model or a long-context rate from a cumulative total alone.
        if last_cents is not None and delta == counts(last):
            cents += last_cents
            priced += 1
        else:
            skipped += 1
        previous = total
    return {'cents': cents if priced else None, 'last_request_cents': last_cents,
            'priced_requests': priced, 'skipped_records': skipped,
            'partial': skipped > 0, 'pricing_date': '2026-09-20',
            'basis': 'Standard API token rates; excludes Fast mode, cache-write surcharges, tools, taxes and subscription fees. Actual charges are not reported by this log.'}


def estimate_records_for(provider, records):
    return (estimate_claude_records if provider == 'claude' else estimate_records)(records)


def estimate_file(path, provider='codex'):
    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, provider)
    if key in _cache:
        return dict(_cache[key])
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
    if len(_cache) >= 10:
        _cache.clear()
    _cache[key] = result
    return dict(result)
