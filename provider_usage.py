"""Read account quota from provider services, never from elapsed clock time.

Codex uses the documented app-server account/rateLimits/read RPC. Claude uses
the read-only OAuth usage endpoint used by the installed Claude Code client.
Credentials stay in memory and are never returned, logged, or copied to cache.
No login, token refresh, model request, or quota-reset operation is performed.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from urllib import error, request

CACHE_SECONDS = 60
# A reading is held and labelled with its age rather than blanked: quota
# windows run for hours, so a few minutes old is still the truth on screen.
STALE_SECONDS = 900
FETCH_TIMEOUT = 20
# Claude's usage endpoint rejects frequent polling, so requests to it are
# spaced out and a refusal backs off instead of retrying every minute.
MIN_INTERVAL = {"claude": 120.0, "codex": 30.0}
RATE_LIMIT_BACKOFF = 300.0
MAX_BACKOFF = 1800.0
CODEX_SOURCE = "Codex account/rateLimits/read"
CLAUDE_SOURCE = "Claude Code OAuth usage"
_cache: dict | None = None
_cache_time = 0.0
_lock = threading.Lock()
_fetch_lock = threading.Lock()
# Last reading that actually returned windows, per provider, plus request
# pacing state. Held readings keep their own fetched_at, so age stays honest.
_last_good: dict = {}
_attempted: dict = {}
_backoff_until: dict = {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _percent(value):
    value = _number(value)
    return value if value is not None and 0 <= value <= 100 else None


def _timestamp(value):
    number = _number(value)
    if number is not None:
        return number if 0 < number < 253402300800 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # A timestamp without a timezone cannot locate the actual reset.
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _provider(provider_id, source, windows=None, reason=None, fetched_at=None, retry_after=None):
    result = {
        "id": provider_id,
        "label": "Codex" if provider_id == "codex" else "Claude",
        "status": "ok" if windows else "unavailable",
        "source": source,
        "fetched_at": fetched_at,
        "windows": windows or [],
    }
    if reason:
        result["reason"] = reason
    if retry_after:
        result["retry_after"] = retry_after
    return result


def _window_label(minutes, fallback):
    if minutes == 10080:
        return "Weekly"
    if minutes == 300:
        return "5-hour"
    if minutes is not None:
        if minutes % 1440 == 0:
            return f"{minutes / 1440:g}-day"
        if minutes % 60 == 0:
            return f"{minutes / 60:g}-hour"
        return f"{minutes:g}-minute"
    return fallback.capitalize()


def normalize_codex(payload, fetched_at=None):
    """Use returned durations, never assume primary means a five-hour window."""
    if not isinstance(payload, dict):
        return _provider("codex", CODEX_SOURCE, reason="Invalid quota response.")
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict):
        legacy = payload.get("rateLimits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    windows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for key in ("primary", "secondary"):
            raw = bucket.get(key)
            if not isinstance(raw, dict):
                continue
            used = _percent(raw.get("usedPercent"))
            if used is None:
                continue
            minutes = _number(raw.get("windowDurationMins"))
            minutes = minutes if minutes is not None and minutes > 0 else None
            label = _window_label(minutes, key)
            if bucket_id != "codex":
                label = f"{bucket.get('limitName') or bucket_id} · {label}"
            windows.append({
                "id": f"{bucket_id}:{key}", "label": label,
                "used_percent": used,
                "resets_at": _timestamp(raw.get("resetsAt")),
                "window_minutes": minutes,
            })
    return _provider("codex", CODEX_SOURCE, windows,
                     None if windows else "No quota percentage returned for this account.",
                     fetched_at if fetched_at is not None else time.time())


def normalize_claude(payload, fetched_at=None):
    if not isinstance(payload, dict):
        return _provider("claude", CLAUDE_SOURCE, reason="Invalid quota response.")
    windows = []
    for key, raw in payload.items():
        # Extra usage is a separate monetary budget, not a subscription window.
        if key != "five_hour" and not key.startswith("seven_day"):
            continue
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("utilization"))
        if used is None:
            continue
        minutes = 300 if key == "five_hour" else 10080
        suffix = key.removeprefix("seven_day").strip("_").replace("_", " ")
        label = "5-hour" if key == "five_hour" else "Weekly"
        if key != "five_hour" and suffix:
            label += f" ({suffix})"
        windows.append({
            "id": key, "label": label, "used_percent": used,
            "resets_at": _timestamp(raw.get("resets_at")),
            "window_minutes": minutes,
        })
    # Newer responses also list every limit, including weekly caps scoped to
    # one model that have no seven_day_* key of their own.
    listed = payload.get("limits")
    for raw in listed if isinstance(listed, list) else []:
        if not isinstance(raw, dict) or raw.get("kind") != "weekly_scoped":
            continue
        scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
        named = [part.get("display_name") for part in (scope.get("model"), scope.get("surface"))
                 if isinstance(part, dict) and isinstance(part.get("display_name"), str)]
        used = _percent(raw.get("percent"))
        if not named or not named[0].strip() or used is None:
            continue
        name = named[0].strip()
        if any(w["id"] == f"seven_day_{name.lower()}" for w in windows):
            continue
        windows.append({
            "id": f"weekly_scoped:{name.lower().replace(' ', '_')}", "label": f"Weekly ({name} only)",
            "used_percent": used, "resets_at": _timestamp(raw.get("resets_at")),
            "window_minutes": 10080,
        })
    result = _provider("claude", CLAUDE_SOURCE, windows,
                       None if windows else "No quota percentage returned for this account.",
                       fetched_at if fetched_at is not None else time.time())
    # Where this week's usage went, by surface, as Claude reports it.
    breakdown = payload.get("seven_day_breakdown")
    rows = breakdown.get("rows") if isinstance(breakdown, dict) else None
    if isinstance(rows, list):
        shares = [{"name": row["display_name"], "percent": _percent(row.get("percent"))}
                  for row in rows if isinstance(row, dict) and isinstance(row.get("display_name"), str)]
        shares = [row for row in shares if row["percent"] is not None]
        if shares:
            result["breakdown"] = {"as_of": _timestamp(breakdown.get("as_of")), "rows": shares}
    return result


def _codex_command():
    command = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if command:
        return command
    if os.name == "nt":
        # Desktop shortcuts may inherit a PATH from before Codex was installed.
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = list(base.glob("*/codex.exe")) if base.is_dir() else []
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return None


def _read_codex():
    command = _codex_command()
    if not command:
        return _provider("codex", CODEX_SOURCE, reason="Codex CLI was not found.")
    process = None
    try:
        process = subprocess.Popen(
            [command, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages = queue.Queue()

        def read_lines():
            try:
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                        if isinstance(message, dict):
                            messages.put(message)
                    except ValueError:
                        continue
            finally:
                messages.put(None)

        threading.Thread(target=read_lines, daemon=True).start()
        deadline = time.monotonic() + 16

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def response(message_id):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = messages.get(timeout=remaining)
                if message is None:
                    raise RuntimeError("closed")
                if message.get("id") == message_id:
                    if "error" in message:
                        # Provider errors can contain sensitive internal details.
                        raise RuntimeError("rpc")
                    return message.get("result")
                if "id" in message and "method" in message:
                    # In particular, never supply or refresh authentication here.
                    send({"id": message["id"], "error": {
                        "code": -32601, "message": "Read-only quota client"}})

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "tokenmaxxing_usage", "version": "1.0.0"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        return normalize_codex(response(2))
    except (queue.Empty, TimeoutError):
        return _provider("codex", CODEX_SOURCE, reason="Codex quota request timed out.")
    except (OSError, RuntimeError, ValueError):
        return _provider("codex", CODEX_SOURCE,
                         reason="Codex quota could not be read. Check the existing Codex sign-in.")
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_claude():
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    credentials = config / ".credentials.json"
    try:
        data = json.loads(credentials.read_text(encoding="utf-8"))
        auth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = auth.get("accessToken") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token:
            return _provider("claude", CLAUDE_SOURCE,
                             reason="No Claude Code subscription sign-in is available.")
        expires = _number(auth.get("expiresAt"))
        if expires is not None and expires / 1000 <= time.time():
            return _provider("claude", CLAUDE_SOURCE,
                             reason="Claude Code sign-in has expired. Open Claude Code to reconnect.")
        req = request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "TokenMaxxing/1.0", "Accept": "application/json",
        })
        with request.build_opener(_NoRedirect).open(req, timeout=12) as response:
            payload = response.read(1024 * 1024)
        return normalize_claude(json.loads(payload))
    except FileNotFoundError:
        reason = "No Claude Code subscription sign-in is available."
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            reason = "Claude did not authorize quota access. Check the existing Claude Code sign-in."
        elif exc.code == 429:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = max(60.0, min(MAX_BACKOFF, float(retry)))
            except (TypeError, ValueError):
                wait = RATE_LIMIT_BACKOFF
            return _provider("claude", CLAUDE_SOURCE,
                             reason=f"Claude limited quota requests. Retrying in {round(wait / 60)} min.",
                             retry_after=wait)
        else:
            reason = f"Claude usage service returned HTTP {exc.code}."
    except (TimeoutError, error.URLError):
        reason = "Claude usage service could not be reached."
    except (OSError, ValueError, TypeError):
        reason = "Claude usage data could not be read."
    return _provider("claude", CLAUDE_SOURCE, reason=reason)


def _mark_stale(snapshot):
    if snapshot is None:
        return None
    snapshot = copy.deepcopy(snapshot)
    now = time.time()
    for provider in snapshot["providers"]:
        if provider["status"] != "ok":
            continue
        fetched = provider.get("fetched_at")
        age = now - fetched if isinstance(fetched, (int, float)) else float("inf")
        for window in provider["windows"]:
            expired = window.get("resets_at") is not None and window["resets_at"] <= now
            window["status"] = "expired" if expired else "ok"
        expired = bool(provider["windows"]) and all(w["status"] == "expired" for w in provider["windows"])
        if age > STALE_SECONDS or expired:
            provider["status"] = "stale"
            provider["reason"] = ("A reported reset has passed; awaiting fresh usage."
                                  if expired else "Last quota reading is stale.")
    return snapshot


def peek_provider_usage():
    """Return a copy of the cache immediately, marking aged readings stale."""
    with _lock:
        return _mark_stale(_cache)


def _hold(provider_id, source, note):
    """Show the last reading that returned data, labelled with why it is old."""
    held = _last_good.get(provider_id)
    if not held:
        return _provider(provider_id, source, reason=note)
    held = copy.deepcopy(held)
    held["warning"] = note
    return held


def _fetch_one(provider_id, source, reader, force):
    """One paced request: never hammer a provider that just refused us."""
    now = time.monotonic()
    until = _backoff_until.get(provider_id, 0.0)
    if now < until:
        return _hold(provider_id, source,
                     f"{'Claude' if provider_id == 'claude' else 'Codex'} limited quota requests. "
                     f"Retrying in {max(1, round((until - now) / 60))} min.")
    # A refresh the person asked for always reaches the provider; only
    # automatic polling is paced, and a refusal is honoured either way.
    last = _attempted.get(provider_id)
    spacing = 0.0 if force else MIN_INTERVAL.get(provider_id, 0.0)
    if last is not None and now - last < spacing:
        return _hold(provider_id, source, "Reusing the last reading; this provider limits how often "
                                          "usage can be requested.")
    _attempted[provider_id] = now
    try:
        result = reader()
    except Exception:
        result = _provider(provider_id, source, reason="Quota request failed.")
    retry_after = result.pop("retry_after", None)
    if retry_after:
        _backoff_until[provider_id] = time.monotonic() + retry_after
    if result.get("status") == "ok":
        _backoff_until.pop(provider_id, None)
        _last_good[provider_id] = result
        return result
    # A failed request never becomes zero usage, and never blanks a reading
    # that was true minutes ago; staleness alone retires it.
    return _hold(provider_id, source, result.get("reason") or "Quota request returned no data.")


def get_provider_usage(force=False):
    """Fetch both services in parallel with a 20-second overall response bound.

    The bridge executes requests off the UI thread. Concurrent polling reads
    reuse the current cache. A forced refresh waits for an active read, then
    fetches again so credentials changed during that read are picked up.
    Unavailable readings replace old readings; they never become zero usage.
    """
    global _cache, _cache_time
    with _lock:
        if not force and _cache is not None and time.monotonic() - _cache_time < CACHE_SECONDS:
            return _mark_stale(_cache)
    acquired = _fetch_lock.acquire(timeout=FETCH_TIMEOUT + 1) if force else _fetch_lock.acquire(blocking=False)
    if not acquired:
        # A refresh is already running; show what is on hand rather than
        # replacing the dashboard with a progress message.
        return peek_provider_usage() or {
            "fetched_at": None, "providers": [
                _provider("codex", CODEX_SOURCE, reason="Reading quota\u2026"),
                _provider("claude", CLAUDE_SOURCE, reason="Reading quota\u2026")]}
    try:
        results = queue.Queue()

        def fetch(provider_id, source, reader):
            try:
                result = _fetch_one(provider_id, source, reader, force)
            except Exception:
                result = _hold(provider_id, source, "Quota request failed.")
            results.put(result)

        readers = (("codex", CODEX_SOURCE, _read_codex), ("claude", CLAUDE_SOURCE, _read_claude))
        for args in readers:
            threading.Thread(target=fetch, args=args, daemon=True).start()
        deadline = time.monotonic() + FETCH_TIMEOUT
        providers = {}
        while len(providers) < len(readers):
            try:
                result = results.get(timeout=max(0, deadline - time.monotonic()))
                providers[result["id"]] = result
            except queue.Empty:
                break
        snapshot = {"fetched_at": time.time(), "providers": [
            providers.get(provider_id) or _hold(provider_id, source, "Quota request timed out.")
            for provider_id, source, _ in readers]}
        with _lock:
            _cache, _cache_time = snapshot, time.monotonic()
        return _mark_stale(snapshot)
    finally:
        _fetch_lock.release()
