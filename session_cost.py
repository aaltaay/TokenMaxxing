"""API-equivalent token value; never an account charge or subscription bill."""
import json
from pathlib import Path

# USD per million tokens, verified 2026-09-20:
# https://developers.openai.com/api/docs/models/gpt-6-astra
RATES = {'gpt-6-astra': (10.0, 1.0, 50.0)}
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')
MAX_BYTES = 64 * 1024 * 1024
_cache = {}


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


def estimate_file(path):
    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
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

    result = estimate_records(records())
    result['partial'] |= incomplete or stat.st_size > MAX_BYTES
    if len(_cache) >= 10:
        _cache.clear()
    _cache[key] = result
    return dict(result)
