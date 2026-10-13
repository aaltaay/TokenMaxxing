"""Read account quota from provider services, never from elapsed clock time.

Codex uses the documented app-server account/rateLimits/read RPC, and the
Codex app-server keeps its own ChatGPT sign-in renewed. Claude uses the
read-only OAuth usage endpoint used by the installed Claude Code client, with
the sign-in Claude Code keeps in .credentials.json. That access token lasts
about eight hours, and for someone who works in the Claude desktop app (which
signs its own sessions in) and never runs the `claude` CLI, nothing else
renews the file, so it would lapse overnight. It is renewed here the way
Claude Code renews it: with the stored refresh token, against Claude Code's
own token endpoint and client id, under Claude Code's own refresh lock, and
written back in place, in the same format, only over the sign-in it replaced.
Only a refresh token the provider refuses asks for a new sign-in.

Credentials stay in memory and in the file they came from; they are never
returned, logged, or copied to cache. No login, model request, or quota-reset
operation is performed. Plan names and emails come from local account files.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.client import HTTPException
from urllib import error, request

from cursor_usage import hud_dir

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
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Claude Code's production OAuth client, as its own build defines it
# (TOKEN_URL and CLIENT_ID in the 2.1.x bundle). Renewing with anything else
# would mint a sign-in Claude Code does not recognise.
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# The scopes Claude Code asks for when the stored sign-in does not list any.
CLAUDE_SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code",
                 "user:mcp_servers", "user:file_upload", "user:plugins")
# Claude Code renews five minutes before the stored expiry, so a request never
# leaves with a token that lapses on the way; the same margin is used here.
RENEW_MARGIN = 300
RENEW_TIMEOUT = 15
# Claude Code's refresh lock is a directory that its holder touches every few
# seconds; one untouched for a minute was left by a process that died.
LOCK_STALE_SECONDS = 60
LOCK_ATTEMPTS = 4
SIGNED_OUT = "No Claude Code subscription sign-in is available."
EXPIRED = "Claude Code sign-in has expired. Open Claude Code to reconnect."
USER_AGENT = "TokenMaxxing/1.0"
_cache: dict | None = None
_cache_time = 0.0
_lock = threading.Lock()
_fetch_lock = threading.Lock()
# Last reading that actually returned windows, per provider, plus request
# pacing state. Held readings keep their own fetched_at, so age stays honest.
_last_good: dict = {}
_attempted: dict = {}
_backoff_until: dict = {}
# Codex and Claude are read on threads of their own; their saves of the last
# good readings take turns, so neither overwrites the other's.
_save_lock = threading.Lock()
# A read that outlives its deadline can overlap the next one; renewals in this
# process take turns too. Refresh tokens the provider refused are remembered
# by fingerprint only, so a dead sign-in is not offered again every poll.
_renew_lock = threading.Lock()
_refused: set = set()
# Tokens renewed here that could not be written back yet, keyed by the
# fingerprint of the refresh token they replaced: if the provider rotated it,
# the file's copy no longer works and these are the only good ones.
_unsaved: dict = {}


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


def _provider(provider_id, source, windows=None, reason=None, fetched_at=None, retry_after=None,
              needs_sign_in=False):
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
    if needs_sign_in:
        result["needs_sign_in"] = True
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
                         reason="Codex quota could not be read. Check the existing Codex sign-in.",
                         needs_sign_in=True)
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


def _claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _load_credentials(path):
    """The whole credentials file and the Claude sign-in inside it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None, None
    auth = data.get("claudeAiOauth")
    return data, auth if isinstance(auth, dict) else None


def _usable(auth):
    token = auth.get("accessToken") if isinstance(auth, dict) else None
    return isinstance(token, str) and bool(token)


def _expiring(auth):
    expires = _number(auth.get("expiresAt"))
    return expires is not None and expires / 1000 - RENEW_MARGIN <= time.time()


def _fingerprint(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _take_lock(path):
    """Take one lock directory the way Claude Code's lock library does: mkdir
    either creates it or fails, so two processes can never both hold it."""
    for _ in range(2):
        try:
            path.mkdir()
            return True
        except FileExistsError:
            try:
                idle = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if idle <= LOCK_STALE_SECONDS:
                return False
            try:
                path.rmdir()
            except OSError:
                return False
        except OSError:
            return False
    return False


class _RefreshLock:
    """Claude Code's own refresh lock: the directory current builds take and
    the one older builds take, so the HUD and a running Claude Code never spend
    the same refresh token at once. A renewal holds it for seconds, well inside
    the minute after which Claude Code would call it abandoned."""

    def __init__(self, config):
        self.paths = (config / ".oauth_refresh.lock", Path(os.path.realpath(config) + ".lock"))
        self.held = []

    def __enter__(self):
        for path in self.paths:
            if not _take_lock(path):
                self.release()
                return False
            self.held.append(path)
        return True

    def __exit__(self, *_):
        self.release()

    def release(self):
        while self.held:
            try:
                self.held.pop().rmdir()
            except OSError:
                pass


def _write_credentials(path, data):
    """Replace the file in one step, so no reader ever sees half of it. The
    layout is Claude Code's own (compact JSON), and the file stays private."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(f"{path.name}.tokenmaxxing-{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                             0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # Windows refuses while another process has the file open.
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _save_renewal(path, spent, renewal):
    """Write a renewal back only over the sign-in it replaced. If the file
    meanwhile holds another refresh token, someone signed in or renewed since,
    and theirs is kept. Every other field, known or not, is left as it was."""
    data, auth = _load_credentials(path)
    if data is None or auth is None or auth.get("refreshToken") not in ("", spent):
        return False
    _write_credentials(path, {**data, "claudeAiOauth": {**auth, **renewal}})
    return True


def _request_renewal(auth):
    """One refresh grant, shaped exactly as Claude Code sends it. Returns the
    fields Claude Code updates; raises on any failure."""
    scopes = auth.get("scopes")
    scopes = [scope for scope in scopes if isinstance(scope, str) and scope] if isinstance(scopes, list) else []
    client = auth.get("clientId")
    body = json.dumps({
        "grant_type": "refresh_token", "refresh_token": auth["refreshToken"],
        "client_id": client if isinstance(client, str) and client else CLAUDE_CLIENT_ID,
        "scope": " ".join(scopes or CLAUDE_SCOPES),
    }).encode("utf-8")
    req = request.Request(CLAUDE_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
    with request.build_opener(_NoRedirect).open(req, timeout=RENEW_TIMEOUT) as response:
        granted = json.loads(response.read(1024 * 1024))
    if not isinstance(granted, dict):
        raise ValueError("Malformed token response.")
    access, refresh = granted.get("access_token"), granted.get("refresh_token")
    lifetime = _number(granted.get("expires_in"))
    if not isinstance(access, str) or not access or lifetime is None or lifetime <= 0:
        raise ValueError("Malformed token response.")
    now = int(time.time() * 1000)
    renewal = {"accessToken": access,
               # Without a new refresh token, the one just used stays valid.
               "refreshToken": refresh if isinstance(refresh, str) and refresh else auth["refreshToken"],
               "expiresAt": now + int(lifetime * 1000)}
    lasts = _number(granted.get("refresh_token_expires_in"))
    if lasts is not None and lasts > 0:
        renewal["refreshTokenExpiresAt"] = now + int(lasts * 1000)
    scope = granted.get("scope")
    if isinstance(scope, str) and scope.split():
        renewal["scopes"] = scope.split()
    return renewal


def _renew_claude(config, used, force=False):
    """Return (sign-in, None) with a token worth sending, or (None, result)
    saying why there is none. `used` is the access token already tried; a
    different one on file means another process renewed it first."""
    path = config / ".credentials.json"
    with _renew_lock:
        for attempt in range(LOCK_ATTEMPTS):
            if attempt:
                time.sleep(1)
            with _RefreshLock(config) as held:
                # Read again now: the file may have changed since it was read.
                try:
                    _, auth = _load_credentials(path)
                except FileNotFoundError:
                    auth = None
                except (OSError, ValueError):
                    continue
                if not _usable(auth):
                    return None, _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=SIGNED_OUT)
                if auth["accessToken"] != used and not _expiring(auth):
                    return auth, None
                if not held:
                    continue
                refresh = auth.get("refreshToken")
                if not isinstance(refresh, str) or not refresh:
                    return None, _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=EXPIRED)
                spent = _fingerprint(refresh)
                renewal = _unsaved.get(spent)
                if renewal is None or force or _expiring(renewal):
                    current = {**auth, **(renewal or {})}
                    if _fingerprint(current["refreshToken"]) in _refused:
                        return None, _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=EXPIRED)
                    try:
                        renewal = _request_renewal(current)
                    except error.HTTPError as exc:
                        if exc.code in (400, 401, 403):
                            # The provider refused this refresh token: only a
                            # new sign-in can help, so it is not offered again.
                            _refused.add(_fingerprint(current["refreshToken"]))
                            return None, _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=EXPIRED)
                        return None, _provider("claude", CLAUDE_SOURCE,
                                               reason="Claude sign-in could not be renewed just now; retrying.")
                    except (OSError, ValueError, TypeError, HTTPException):
                        return None, _provider("claude", CLAUDE_SOURCE,
                                               reason="Claude sign-in could not be renewed just now; retrying.")
                    _unsaved[spent] = renewal
                try:
                    # Written back, or a newer sign-in is already on file:
                    # either way this renewal no longer needs keeping.
                    _save_renewal(path, refresh, renewal)
                    _unsaved.pop(spent, None)
                except (OSError, ValueError):
                    # Kept in memory and written back on the next read.
                    pass
                return {**auth, **renewal}, None
        return None, _provider("claude", CLAUDE_SOURCE,
                               reason="Claude Code is renewing its sign-in; retrying shortly.")


def _claude_usage(token):
    req = request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    with request.build_opener(_NoRedirect).open(req, timeout=12) as response:
        payload = response.read(1024 * 1024)
    return normalize_claude(json.loads(payload))


def _read_claude():
    config = _claude_dir()
    try:
        _, auth = _load_credentials(config / ".credentials.json")
        if not _usable(auth):
            return _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=SIGNED_OUT)
        refresh = auth.get("refreshToken")
        unsaved = isinstance(refresh, str) and bool(refresh) and _fingerprint(refresh) in _unsaved
        renewed = _expiring(auth) or unsaved
        if renewed:
            auth, problem = _renew_claude(config, auth["accessToken"])
            if problem:
                return problem
        try:
            return _claude_usage(auth["accessToken"])
        except error.HTTPError as exc:
            # A token can be withdrawn before its stated expiry: renew it once
            # and ask again. A refusal right after renewing is a real sign-in
            # problem, and so is a second one.
            if exc.code != 401 or renewed or not auth.get("refreshToken"):
                raise
        auth, problem = _renew_claude(config, auth["accessToken"], force=True)
        if problem:
            return problem
        return _claude_usage(auth["accessToken"])
    except FileNotFoundError:
        return _provider("claude", CLAUDE_SOURCE, needs_sign_in=True, reason=SIGNED_OUT)
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            return _provider("claude", CLAUDE_SOURCE, needs_sign_in=True,
                             reason="Claude did not authorize quota access. Check the existing Claude Code sign-in.")
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


# Subscription names as each provider's own client stores them, in the words
# its plan page uses. A name not listed here stays unknown rather than guessed.
CLAUDE_PLANS = {"max": "Max", "pro": "Pro", "team": "Team", "enterprise": "Enterprise", "free": "Free"}
CLAUDE_ORGANIZATION_PLANS = {"claude_max": "max", "claude_pro": "pro", "claude_team": "team",
                             "claude_enterprise": "enterprise"}
CODEX_PLANS = {
    "free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite", "promax": "Pro Max",
    "team": "Team", "business": "Business", "self_serve_business_prolite": "Business",
    "self_serve_business_usage_based": "Business", "enterprise": "Enterprise", "ent26": "Enterprise",
    "enterprise_cbp_automation": "Enterprise", "enterprise_cbp_usage_based": "Enterprise",
    "edu": "Edu", "education": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro",
}


def _json_file(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def claude_account():
    """Plan and email from Claude Code's own files; nothing is requested."""
    config = _claude_dir()
    auth = _json_file(config / ".credentials.json").get("claudeAiOauth")
    auth = auth if isinstance(auth, dict) else {}
    # Claude Code's global config: a legacy .config.json wins where it exists.
    legacy = config / ".config.json"
    profile = legacy if legacy.is_file() else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    account = _json_file(profile).get("oauthAccount")
    account = account if isinstance(account, dict) else {}
    kind = _text(auth.get("subscriptionType")) or CLAUDE_ORGANIZATION_PLANS.get(account.get("organizationType"))
    plan = CLAUDE_PLANS.get(kind.lower()) if kind else None
    if plan == "Max":
        # Max comes in sizes; the rate-limit tier says which, e.g. default_claude_max_20x.
        for tier in (auth.get("rateLimitTier"), account.get("userRateLimitTier"),
                     account.get("organizationRateLimitTier")):
            size = re.search(r"max_(\d+)x", tier) if isinstance(tier, str) else None
            if size:
                plan = f"Max {size.group(1)}x"
                break
    if plan is None and _text(account.get("planDisplayName")):
        plan = re.sub(r"^Claude\s+", "", _text(account["planDisplayName"])) or None
    return {"plan": plan, "email": _text(account.get("emailAddress"))}


def _jwt_claims(token):
    """The claims of a JWT, read without verifying it: for display only."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, IndexError, TypeError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def codex_account():
    """Plan and email from the sign-in Codex keeps; nothing is requested."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    tokens = _json_file(home / "auth.json").get("tokens")
    claims = _jwt_claims(tokens.get("id_token")) if isinstance(tokens, dict) else {}
    auth = claims.get("https://api.openai.com/auth")
    kind = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return {"plan": CODEX_PLANS.get(kind.lower()) if isinstance(kind, str) else None,
            "email": _text(claims.get("email"))}


def _with_account(provider):
    """Add the plan and email to a provider entry at the moment it is shown,
    so they are never saved with the readings."""
    reader = codex_account if provider.get("id") == "codex" else claude_account
    try:
        details = reader()
    except Exception:
        details = {"plan": None, "email": None}
    return {**provider, **details}


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


def _saved_path():
    return hud_dir() / "provider-last-good.json"


def _remember_good(provider_id, result):
    """Keep a reading that returned data, in memory and on disk. The file is
    updated one provider at a time: after a restart, the provider that answers
    first must not erase the other's saved reading before it is needed."""
    with _save_lock:
        _last_good[provider_id] = result
        try:
            path = _saved_path()
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            saved = {**(saved if isinstance(saved, dict) else {}), provider_id: result}
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass


def _saved_reading(provider_id):
    """The last good reading from an earlier run, so a restart that meets a
    refusal still shows the quota instead of a blank card."""
    try:
        saved = json.loads(_saved_path().read_text(encoding="utf-8")).get(provider_id)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(saved, dict) or saved.get("status") != "ok" or not saved.get("windows"):
        return None
    return saved


def _hold(provider_id, source, note, needs_sign_in=False):
    """Show the last reading that returned data, labelled with why it is old."""
    held = _last_good.get(provider_id)
    if not held and not needs_sign_in:
        held = _saved_reading(provider_id)
        if held:
            _last_good[provider_id] = held
    if not held:
        return _provider(provider_id, source, reason=note, needs_sign_in=needs_sign_in)
    held = copy.deepcopy(held)
    held["warning"] = note
    if needs_sign_in:
        held["needs_sign_in"] = True
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
        _remember_good(provider_id, result)
        return result
    # A failed request never becomes zero usage, and never blanks a reading
    # that was true minutes ago; staleness alone retires it.
    return _hold(provider_id, source, result.get("reason") or "Quota request returned no data.",
                 needs_sign_in=bool(result.get("needs_sign_in")))


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
                _with_account(_provider("codex", CODEX_SOURCE, reason="Reading quota\u2026")),
                _with_account(_provider("claude", CLAUDE_SOURCE, reason="Reading quota\u2026"))]}
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
            _with_account(providers.get(provider_id) or _hold(provider_id, source, "Quota request timed out."))
            for provider_id, source, _ in readers]}
        with _lock:
            _cache, _cache_time = snapshot, time.monotonic()
        return _mark_stale(snapshot)
    finally:
        _fetch_lock.release()
