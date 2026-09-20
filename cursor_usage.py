"""Local Cursor snapshots + unofficial dashboard usage (stdlib only).

Billing calls use the same WorkOS session the Cursor app already stored.
The session JWT is never written to disk by this module.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_TTL_S = 180
CYCLE_FETCH_MAX_SECONDS = 150
SCHEMA_VERSION = 2
EVENT_FETCH_MAX_SECONDS = 120
EVENT_FETCH_MAX_PAGES = 500
DASHBOARD = "https://cursor.com"
API2 = "https://api2.cursor.sh"
USER_AGENT = "token-hud/2.0"
OVERHEAD_IDS = ("system_prompt", "tools", "rules", "skills", "mcp", "subagents")
_cycle_fetch_lock = threading.Lock()


def cursor_state_db() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def hud_dir() -> Path:
    override = os.environ.get("TOKEN_HUD_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cursor" / "token-hud"


def cache_path() -> Path:
    return hud_dir() / "cycle-cache.json"


def connect(db: Path | None = None) -> sqlite3.Connection:
    path = db or cursor_state_db()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    con.execute("PRAGMA query_only=ON")
    return con


def item_text(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
    if not row or row[0] is None:
        return None
    val = row[0]
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    text = str(val).strip().strip('"').strip("'")
    return text or None


def to_int(v, default=None):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def to_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        value = float(v)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def session_cookie(con: sqlite3.Connection) -> str | None:
    token = item_text(con, "cursorAuth/accessToken")
    if not token:
        return None
    claims = jwt_claims(token)
    if claims.get("type") == "api_key_token":
        return None
    sub = claims.get("sub")
    if not sub:
        return None
    cookie_id = str(sub).split("|")[-1]
    return f"{cookie_id}::{token}"


def access_token(con: sqlite3.Connection) -> str | None:
    return item_text(con, "cursorAuth/accessToken")


def _normalized_email(value) -> str | None:
    return value.strip().casefold() or None if isinstance(value, str) else None


def _current_cursor_identity() -> tuple[str | None, str | None]:
    """Read current sign-in state; the credential is compared in memory only."""
    db = cursor_state_db()
    if not db.exists():
        return None, None
    con = connect(db)
    try:
        return session_cookie(con), _normalized_email(item_text(con, "cursorAuth/cachedEmail"))
    finally:
        con.close()


def read_cached_cycle(*, allow_stale: bool = False) -> dict | None:
    """Read only a validated snapshot belonging to the current local account.

    Missing identity, logout, future timestamps and unknown source/schema never
    authorize cache reuse. A caller may explicitly request an aged snapshot;
    its original timestamp is retained and it is marked stale.
    """
    try:
        report = json.loads(cache_path().read_text(encoding="utf-8"))
        if (not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION
                or report.get("source") != "cursor_dashboard"):
            return None
        fetched = report.get("fetched_at")
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
            return None
        age = time.time() - fetched
        if fetched <= 0 or age < 0 or (age >= CACHE_TTL_S and not allow_stale):
            return None
        cookie, email = _current_cursor_identity()
        if not cookie or not email or email != _normalized_email(report.get("email")):
            return None
        return {**report, "cached": True, "stale": age >= CACHE_TTL_S}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def _request_timeout(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Cursor refresh time budget exhausted")
    return min(maximum, remaining)


def dashboard_request(cookie: str, path: str, method: str = "GET", body=None, timeout: int = 45):
    headers = {
        "Cookie": "WorkosCursorSessionToken=" + cookie.replace("::", "%3A%3A"),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Origin"] = DASHBOARD
    req = urllib.request.Request(DASHBOARD + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {path}") from exc


def api2_post(token: str, path: str, body=None, timeout: int = 30):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(
        API2 + path,
        data=json.dumps(body if body is not None else {}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {path}") from exc


def iso_to_ms(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n > 10_000_000_000 else n * 1000
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n if n > 10_000_000_000 else n * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def fmt_money(cents: float | None) -> str:
    return f"${cents / 100.0:,.2f}" if cents is not None else "Unavailable"


def fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "--"


def pool_for_model(model: str, auto_models=()) -> str:
    """Only use the dashboard's explicit membership list, never name guesses."""
    return "cursor" if model in auto_models else "unknown"


def recent_chats(con: sqlite3.Connection, limit: int = 10) -> list[tuple[str, str]]:
    rows = con.execute(
        """
        SELECT composerId, COALESCE(json_extract(value, '$.name'), '(untitled)')
        FROM composerHeaders
        WHERE COALESCE(isArchived, 0) = 0
          AND COALESCE(isSubagent, 0) = 0
          AND COALESCE(json_extract(value, '$.isDraft'), 0) = 0
        ORDER BY recency DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(r[0], str(r[1])) for r in rows]


def followed_chat_id(con: sqlite3.Connection, pinned: str | None) -> str | None:
    if pinned:
        return pinned
    for key in ("cursor/glass.selectedAgent", "cursor/glass.lastRealAgent"):
        cid = item_text(con, key)
        if cid:
            return cid
    row = con.execute(
        """
        SELECT composerId
        FROM composerHeaders
        WHERE COALESCE(isArchived, 0) = 0
          AND COALESCE(isSubagent, 0) = 0
          AND COALESCE(json_extract(value, '$.isDraft'), 0) = 0
        ORDER BY recency DESC
        LIMIT 1
        """
    ).fetchone()
    return row[0] if row else None


def _parse_breakdown(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def snapshot_for(con: sqlite3.Connection, cid: str) -> dict:
    row = con.execute(
        """
        SELECT
          json_extract(value, '$.name'),
          json_extract(value, '$.contextTokensUsed'),
          json_extract(value, '$.contextTokenLimit'),
          json_extract(value, '$.contextUsagePercent'),
          json_extract(value, '$.promptTokenBreakdown'),
          json_extract(value, '$.modelConfig.modelName'),
          json_extract(value, '$.numSubComposers')
        FROM cursorDiskKV
        WHERE key = ?
        """,
        (f"composerData:{cid}",),
    ).fetchone()
    if not row:
        return {"error": "Chat blob missing", "id": cid}
    breakdown = _parse_breakdown(row[4])
    used = to_int(row[1])
    cats = {}
    for cat in breakdown.get("categories") or []:
        key = cat.get("id") or cat.get("label") or "?"
        cats[key] = to_int(cat.get("estimatedTokens"))
    # Categories are explicitly provider estimates. Do not treat an omitted
    # category or context value as an observed zero.
    overhead_values = [cats[k] for k in OVERHEAD_IDS if k in cats]
    overhead = (sum(overhead_values) if overhead_values
                and all(v is not None for v in overhead_values) else None)
    conv = cats.get("conversation", cats.get("Conversation"))
    tax_pct = (100.0 * overhead / used) if used and overhead is not None else None
    return {
        "id": cid,
        "name": row[0] or "(untitled)",
        "used": used,
        "limit": to_int(row[2]),
        "pct": to_float(row[3]),
        "breakdown": breakdown,
        "cats": cats,
        "overhead": overhead,
        "conversation": conv,
        "tax_pct": tax_pct,
        "model": row[5] or "?",
        "num_sub": to_int(row[6]),
        "source": "cursor_local_database",
        "fetched_at": time.time(),
        "breakdown_estimated": True,
    }


def chat_names(con: sqlite3.Connection) -> dict[str, str]:
    out = {}
    for cid, name in con.execute(
        "SELECT composerId, COALESCE(json_extract(value, '$.name'), '(untitled)') FROM composerHeaders"
    ):
        out[str(cid)] = str(name)
    return out


def cloud_agent_names(con: sqlite3.Connection) -> dict[str, str]:
    row = con.execute(
        "SELECT value FROM ItemTable WHERE key LIKE 'cloudAgentRepository.agents%' LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return {}
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        agents = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out = {}
    if not isinstance(agents, list):
        return out
    for a in agents:
        if not isinstance(a, dict):
            continue
        aid = a.get("bcId") or a.get("id")
        name = a.get("name")
        if aid and name:
            out[str(aid)] = str(name)
    return out


def label_conversation(cid: str, models: dict, local: dict[str, str], cloud: dict[str, str]) -> str:
    if cid in local:
        return local[cid]
    if cid in cloud:
        return f"Cloud: {cloud[cid]}"
    model_names = list(models)
    if model_names and all(str(m).startswith("grok-bot") for m in model_names):
        return "Grok Bot session"
    if str(cid).startswith("bc-"):
        return f"Cloud agent {cid[3:11]}"
    return f"Other session {cid[:8]}"


def _first_number(*values):
    """Coalesce numeric fields without discarding an observed zero."""
    for value in values:
        number = to_float(value)
        if number is not None:
            return number
    return None


def _event_cents(ev: dict) -> float | None:
    tu = ev.get("tokenUsage") or {}
    # Raw usage value and chargedCents are different source metrics. Mixing
    # them makes an apparently precise sum with no consistent meaning.
    return to_float(tu.get("totalCents"))


def _add_known(target: dict, key: str, value):
    """An aggregate is unknown if any contributing value is unknown."""
    target[key] = (target[key] + value
                   if target.get(key) is not None and value is not None else None)


def fetch_events(cookie: str, query: dict, max_seconds: float = EVENT_FETCH_MAX_SECONDS,
                 max_pages: int = EVENT_FETCH_MAX_PAGES) -> dict:
    """Fetch the complete range, or explicitly withhold partial-range totals."""
    result = {"events": [], "events_total": None, "events_complete": False,
              "events_incomplete_reason": None, "events_pages": 0}
    deadline = time.monotonic() + max_seconds
    previous_pages = set()
    for page in range(1, max_pages + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result["events_incomplete_reason"] = "Event fetch time limit reached"
            break
        try:
            chunk = dashboard_request(
                cookie, "/api/dashboard/get-filtered-usage-events", "POST",
                {**query, "page": page, "pageSize": 1000},
                timeout=max(0.1, min(45, remaining)),
            )
        except Exception as exc:
            result["events_incomplete_reason"] = f"Event page fetch failed ({type(exc).__name__})"
            break
        result["events_pages"] += 1
        rows = chunk.get("usageEventsDisplay")
        total = to_int(chunk.get("totalUsageEventsCount"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            result["events_incomplete_reason"] = "Event response has no valid row list"
            break
        if total is None or total < 0:
            result["events_incomplete_reason"] = "Dashboard did not provide a valid total event count"
            break
        if result["events_total"] is not None and total != result["events_total"]:
            result["events_incomplete_reason"] = "Event count changed during pagination; refresh required"
            break
        result["events_total"] = total
        if rows:
            fingerprint = hashlib.sha256(
                json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            ).digest()
            if fingerprint in previous_pages:
                result["events_incomplete_reason"] = "Dashboard repeated an event page"
                break
            previous_pages.add(fingerprint)
        result["events"].extend(rows)
        count = len(result["events"])
        if count == total:
            result["events_complete"] = True
            break
        if count > total or not rows:
            result["events_incomplete_reason"] = "Fetched event count does not match dashboard total"
            break
    else:
        result["events_incomplete_reason"] = "Event pagination safety limit reached"
    return result


def _event_summaries(events: list[dict], local: dict, cloud: dict,
                     auto_models: list) -> dict:
    by_model, by_day, by_conv = {}, {}, {}
    headless, interactive, unclassified = ({"n": 0, "cents": 0.0} for _ in range(3))
    for ev in events:
        tu = ev.get("tokenUsage") or {}
        cents = _event_cents(ev)
        model = ev.get("model") or "Unknown model"
        ts = iso_to_ms(ev.get("timestamp"))
        try:
            day = datetime.fromtimestamp(ts / 1000).astimezone().strftime("%Y-%m-%d") if ts is not None else "Unknown date"
        except (OSError, ValueError, OverflowError):
            day = "Unknown date"
        metrics = {"cents": cents, "in": to_int(tu.get("inputTokens")),
                   "out": to_int(tu.get("outputTokens")),
                   "cw": to_int(tu.get("cacheWriteTokens")),
                   "cr": to_int(tu.get("cacheReadTokens"))}
        m = by_model.setdefault(model, {"n": 0, "cents": 0.0, "in": 0,
            "out": 0, "cw": 0, "cr": 0, "headless": 0,
            "pool": pool_for_model(model, auto_models)})
        d = by_day.setdefault(day, {"n": 0, "cents": 0.0, "in": 0, "out": 0})
        cid = str(ev.get("conversationId") or ev.get("cloudAgentId") or "(none)")
        if cid in ("null", "None", ""):
            cid = str(ev.get("cloudAgentId") or "(none)")
        c = by_conv.setdefault(cid, {"n": 0, "cents": 0.0, "in": 0,
            "out": 0, "cr": 0, "headless": 0, "models": {},
            "other_cents": None, "cursor_cents": None})
        for target in (m, d, c):
            target["n"] += 1
            for key, value in metrics.items():
                if key in target:
                    _add_known(target, key, value)
        c["models"][model] = c["models"].get(model, 0) + 1
        if ev.get("isHeadless") is True:
            _add_known(m, "headless", 1)
            _add_known(c, "headless", 1)
            bucket = headless
        elif ev.get("isHeadless") is False:
            bucket = interactive
        else:
            m["headless"] = c["headless"] = None
            bucket = unclassified
        bucket["n"] += 1
        _add_known(bucket, "cents", cents)
    if unclassified["n"]:
        # Counts cannot claim all headless/interactive activity when flags are absent.
        headless = interactive = {"n": None, "cents": None}
    sort_cost = lambda row: -(row.get("cents") if row.get("cents") is not None else -1)
    conversations = [{"id": cid, "name": label_conversation(cid, c["models"], local, cloud), **c}
                     for cid, c in by_conv.items()]
    conversations.sort(key=sort_cost)
    event_models = [{"model": model, **values} for model, values in by_model.items()]
    event_models.sort(key=sort_cost)
    return {"event_models": event_models,
            "days": [{"date": day, **by_day[day]} for day in sorted(by_day)],
            "conversations": conversations,
            "headless": headless, "interactive": interactive,
            "unclassified": unclassified}


def fetch_cycle(force: bool = False) -> dict:
    """Serialize network refreshes and cache writes within the bridge process."""
    if not _cycle_fetch_lock.acquire(blocking=False):
        raise RuntimeError("Cursor refresh already in progress; wait for it to finish and retry.")
    try:
        return _fetch_cycle(force=force)
    finally:
        _cycle_fetch_lock.release()


def _fetch_cycle(force: bool = False) -> dict:
    path = cache_path()
    if not force:
        cached = read_cached_cycle()
        if cached is not None:
            return cached
    deadline = time.monotonic() + CYCLE_FETCH_MAX_SECONDS
    db = cursor_state_db()
    if not db.exists():
        raise FileNotFoundError("Cursor DB not found")
    con = connect(db)
    try:
        cookie, token = session_cookie(con), access_token(con)
        email = item_text(con, "cursorAuth/cachedEmail")
        plan_name = item_text(con, "cursorAuth/stripeMembershipType")
        local, cloud = chat_names(con), cloud_agent_names(con)
    finally:
        con.close()
    if not cookie:
        raise RuntimeError("No Cursor web session. Sign in to the Cursor app and retry.")
    initial_identity = cookie, _normalized_email(email)
    fetched_at = time.time()
    me = dashboard_request(cookie, "/api/auth/me", timeout=_request_timeout(deadline, 45))
    dashboard_email = me.get("email")
    if (initial_identity[1] and _normalized_email(dashboard_email)
            and initial_identity[1] != _normalized_email(dashboard_email)):
        raise RuntimeError("Cursor account changed during refresh; retry to load the current account.")
    if _normalized_email(dashboard_email):
        email = dashboard_email
    summary = dashboard_request(cookie, "/api/usage-summary", timeout=_request_timeout(deadline, 45))
    period = dashboard_request(cookie, "/api/dashboard/get-current-period-usage", "POST", {},
                               timeout=_request_timeout(deadline, 45))
    user_id = me.get("id")
    summary_plan = (summary.get("individualUsage") or {}).get("plan") or {}
    plan = {**summary_plan, **(period.get("planUsage") or {})}
    breakdown = summary_plan.get("breakdown") or {}
    start = summary.get("billingCycleStart") or period.get("billingCycleStart")
    end = summary.get("billingCycleEnd") or period.get("billingCycleEnd")
    start_ms, end_ms = iso_to_ms(start), iso_to_ms(end)
    valid_cycle = start_ms is not None and end_ms is not None and end_ms > start_ms
    query = {"teamId": 0, "startDate": str(start_ms), "endDate": str(end_ms), "userId": user_id}
    errors = []
    agg, aggregate_available = {}, False
    event_result = {"events": [], "events_total": None, "events_complete": False,
                    "events_incomplete_reason": "Missing user or billing-cycle boundaries",
                    "events_pages": 0}
    if user_id and valid_cycle:
        try:
            agg = dashboard_request(cookie, "/api/dashboard/get-aggregated-usage-events", "POST", query,
                                    timeout=_request_timeout(deadline, 45))
            aggregate_available = isinstance(agg.get("aggregations"), list)
        except Exception as exc:
            errors.append(f"Aggregate usage unavailable ({type(exc).__name__})")
        event_result = fetch_events(cookie, query,
                                    max_seconds=max(0.0, min(EVENT_FETCH_MAX_SECONDS, deadline - time.monotonic())))
    grok_bot = None
    if token:
        try:
            grok_bot = api2_post(token, "/aiserver.v1.DashboardService/GetSandUsageStatus",
                                 timeout=_request_timeout(deadline, 30))
        except Exception as exc:
            errors.append(f"Grok Bot usage unavailable ({type(exc).__name__})")
    auto_models = period.get("autoBucketModels") or []
    models = []
    for row in agg.get("aggregations") or []:
        model = row.get("modelIntent") or row.get("model") or "Unknown model"
        models.append({"model": model, "cents": to_float(row.get("totalCents")),
            "input": to_int(row.get("inputTokens")), "output": to_int(row.get("outputTokens")),
            "cache_write": to_int(row.get("cacheWriteTokens")), "cache_read": to_int(row.get("cacheReadTokens")),
            "tier": row.get("tier"), "pool": pool_for_model(model, auto_models)})
    models.sort(key=lambda row: -(row["cents"] if row["cents"] is not None else -1))
    event_data = {"event_models": [], "days": [], "conversations": [],
                  "headless": {"n": None, "cents": None},
                  "interactive": {"n": None, "cents": None},
                  "unclassified": {"n": None, "cents": None}}
    if event_result["events_complete"]:
        event_data = _event_summaries(event_result["events"], local, cloud, auto_models)
    event_values = [_event_cents(event) for event in event_result["events"]]
    event_value_total = (sum(event_values) if event_result["events_complete"]
                         and all(value is not None for value in event_values) else None)
    aggregate_value_total = to_float(agg.get("totalCostCents"))
    on_demand = (summary.get("individualUsage") or {}).get("onDemand") or {}
    now_ms = time.time() * 1000
    report = {
        "schema_version": SCHEMA_VERSION, "source": "cursor_dashboard",
        "fetched_at": fetched_at, "cached": False, "stale": False,
        "sources": {"quota": "/api/usage-summary + /api/dashboard/get-current-period-usage",
                    "aggregate": "/api/dashboard/get-aggregated-usage-events",
                    "events": "/api/dashboard/get-filtered-usage-events",
                    "grok_bot": "/aiserver.v1.DashboardService/GetSandUsageStatus"},
        "source_errors": errors, "aggregate_available": aggregate_available,
        "email": email, "plan": (summary.get("membershipType") or plan_name or "Unknown").title(),
        "cycle_start": start if valid_cycle else None, "cycle_end": end if valid_cycle else None,
        "elapsed_days": (now_ms - start_ms) / 86400000 if valid_cycle else None,
        "remain_days": (end_ms - now_ms) / 86400000 if valid_cycle else None,
        "api_pct": to_float(plan.get("apiPercentUsed")),
        "auto_pct": to_float(plan.get("autoPercentUsed")),
        "total_pct": to_float(plan.get("totalPercentUsed")),
        "api_days_left": None,
        "total_spend_cents": _first_number(plan.get("totalSpend"), breakdown.get("total"), agg.get("totalCostCents")),
        "included_cents": _first_number(plan.get("includedSpend"), breakdown.get("included")),
        "bonus_cents": _first_number(plan.get("bonusSpend"), breakdown.get("bonus")),
        "on_demand_enabled": on_demand.get("enabled") if isinstance(on_demand.get("enabled"), bool) else None,
        "auto_msg": summary.get("autoModelSelectedDisplayMessage") or period.get("autoModelSelectedDisplayMessage"),
        "api_msg": summary.get("namedModelSelectedDisplayMessage") or period.get("namedModelSelectedDisplayMessage"),
        "grok_bot_pct": to_float(grok_bot.get("usagePercent")) if grok_bot else None,
        "grok_bot_reset": grok_bot.get("nextResetTimestampUtc") if grok_bot else None,
        "models": models, **event_data,
        "other_event_cents": None, "cursor_event_cents": None, "grok_bot_event_cents": None,
        "events_fetched": len(event_result["events"]),
        "events_total": event_result["events_total"],
        "events_complete": event_result["events_complete"],
        "events_incomplete_reason": event_result["events_incomplete_reason"],
        "events_pages": event_result["events_pages"],
        "events_value_cents": event_value_total,
        "aggregate_value_cents": aggregate_value_total,
        "events_value_basis": "tokenUsage.totalCents",
        "event_costs_reconciled": (abs(event_value_total - aggregate_value_total) < 0.01
                                  if event_value_total is not None and aggregate_value_total is not None else None),
        "agg_input": to_int(agg.get("totalInputTokens")),
        "agg_output": to_int(agg.get("totalOutputTokens")),
        "agg_cache_write": to_int(agg.get("totalCacheWriteTokens")),
        "agg_cache_read": to_int(agg.get("totalCacheReadTokens")),
    }
    report["findings"] = build_findings(report)
    if _current_cursor_identity() != initial_identity:
        raise RuntimeError("Cursor account changed during refresh; retry to load the current account.")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
    pending.replace(path)
    return report


def build_findings(report: dict) -> list[str]:
    """Statements supported directly by the returned fields, without predictions."""
    notes = []
    for key, label in (("api_pct", "Other models"), ("auto_pct", "Cursor models"),
                       ("total_pct", "Overall included usage"), ("grok_bot_pct", "Grok Bot")):
        value = report.get(key)
        if value is not None:
            notes.append(f"{label}: {value:.1f}% used, as reported by Cursor.")
    if report.get("total_spend_cents") is not None:
        notes.append(f"Cursor reports {fmt_money(report['total_spend_cents'])} of cycle usage value.")
    if report.get("on_demand_enabled") is not None:
        notes.append(f"On-demand billing is {'enabled' if report['on_demand_enabled'] else 'disabled'}.")
    if report.get("events_complete") is False:
        notes.append("Event-derived totals are unavailable: " + str(report.get("events_incomplete_reason") or "incomplete event data") + ".")
    if report.get("event_costs_reconciled") is False:
        notes.append("Event-reported usage values differ from the dashboard cycle total; they are separate source metrics, not a billing reconciliation.")
    return notes


def billed_for_chat(report: dict | None, cid: str | None) -> dict | None:
    if not report or not cid:
        return None
    for row in report.get("conversations") or []:
        if row.get("id") == cid:
            return row
    return None


if __name__ == "__main__":
    force = "--refresh" in sys.argv
    report = fetch_cycle(force=force)
    percentages = "  ".join(
        f"{label} {report[key]:.1f}%" if report[key] is not None else f"{label} unavailable"
        for label, key in (("other", "api_pct"), ("cursor", "auto_pct"), ("overall", "total_pct"))
    )
    print(f"{report['plan']}  {percentages}")
    print(f"spend {fmt_money(report['total_spend_cents'])}  events {report['events_fetched']}")
    for line in report.get("findings") or []:
        print("-", line)
