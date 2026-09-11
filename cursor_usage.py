"""Local Cursor snapshots + unofficial dashboard usage (stdlib only).

Billing calls use the same WorkOS session the Cursor app already stored.
The session JWT is never written to disk by this module.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_TTL_S = 180
DASHBOARD = "https://cursor.com"
API2 = "https://api2.cursor.sh"
USER_AGENT = "cursor-token-hud/2.0"
OVERHEAD_IDS = ("system_prompt", "tools", "rules", "skills", "mcp", "subagents")
CURSOR_MODEL_HINTS = (
    "cursor-grok",
    "grok-4",
    "composer-",
    "vega",
    "cursor-small",
)


def cursor_state_db() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def cache_path() -> Path:
    return Path.home() / ".cursor" / "token-hud" / "cycle-cache.json"


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


def to_int(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
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
        err = exc.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError(f"HTTP {exc.code} {path}: {err}") from exc


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
        err = exc.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError(f"HTTP {exc.code} {path}: {err}") from exc


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


def fmt_money(cents: float) -> str:
    return f"${cents / 100.0:,.2f}"


def fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "--"


def pool_for_model(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("grok-bot"):
        return "grok-bot"
    if m.startswith(CURSOR_MODEL_HINTS) or m in ("default", "auto"):
        return "cursor"
    return "other"


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
    overhead = sum(cats.get(k, 0) for k in OVERHEAD_IDS)
    if not overhead and cats:
        overhead = sum(
            v
            for k, v in cats.items()
            if "conversation" not in str(k).lower()
        )
    conv = cats.get("conversation") or cats.get("Conversation") or 0
    tax_pct = (100.0 * overhead / used) if used else 0.0
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


def _event_cents(ev: dict) -> float:
    tu = ev.get("tokenUsage") or {}
    return to_float(tu.get("totalCents")) or to_float(ev.get("chargedCents"))


def fetch_cycle(force: bool = False) -> dict:
    path = cache_path()
    if not force and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("fetched_at") or 0) < CACHE_TTL_S:
                return cached
        except Exception:
            pass

    db = cursor_state_db()
    if not db.exists():
        raise FileNotFoundError("Cursor DB not found")

    con = connect(db)
    try:
        cookie = session_cookie(con)
        token = access_token(con)
        email = item_text(con, "cursorAuth/cachedEmail")
        plan_name = item_text(con, "cursorAuth/stripeMembershipType") or "?"
        local = chat_names(con)
        cloud = cloud_agent_names(con)
    finally:
        con.close()

    if not cookie:
        raise RuntimeError("No Cursor web session. Sign in to the Cursor app and retry.")

    me = dashboard_request(cookie, "/api/auth/me")
    summary = dashboard_request(cookie, "/api/usage-summary")
    period = dashboard_request(cookie, "/api/dashboard/get-current-period-usage", "POST", {})
    user_id = me.get("id")
    plan = (period.get("planUsage") or summary.get("individualUsage", {}).get("plan") or {})
    start = summary.get("billingCycleStart") or period.get("billingCycleStart")
    end = summary.get("billingCycleEnd") or period.get("billingCycleEnd")
    start_ms = iso_to_ms(start)
    end_ms = iso_to_ms(end)

    agg = {}
    if user_id and start_ms and end_ms:
        agg = dashboard_request(
            cookie,
            "/api/dashboard/get-aggregated-usage-events",
            "POST",
            {
                "teamId": 0,
                "startDate": str(start_ms),
                "endDate": str(end_ms),
                "userId": user_id,
            },
        )

    events: list[dict] = []
    total_events = 0
    if user_id and start_ms and end_ms:
        page = 1
        while page <= 12:
            chunk = dashboard_request(
                cookie,
                "/api/dashboard/get-filtered-usage-events",
                "POST",
                {
                    "teamId": 0,
                    "startDate": str(start_ms),
                    "endDate": str(end_ms),
                    "userId": user_id,
                    "page": page,
                    "pageSize": 1000,
                },
                timeout=90,
            )
            rows = chunk.get("usageEventsDisplay") or []
            total_events = to_int(chunk.get("totalUsageEventsCount"), total_events)
            events.extend(rows)
            if not rows or len(events) >= total_events:
                break
            page += 1

    grok_bot = None
    if token:
        try:
            grok_bot = api2_post(token, "/aiserver.v1.DashboardService/GetSandUsageStatus")
        except Exception:
            grok_bot = None

    models = []
    for row in agg.get("aggregations") or []:
        model = row.get("modelIntent") or row.get("model") or "?"
        models.append(
            {
                "model": model,
                "cents": to_float(row.get("totalCents")),
                "input": to_int(row.get("inputTokens")),
                "output": to_int(row.get("outputTokens")),
                "cache_write": to_int(row.get("cacheWriteTokens")),
                "cache_read": to_int(row.get("cacheReadTokens")),
                "tier": row.get("tier"),
                "pool": pool_for_model(model),
            }
        )
    models.sort(key=lambda r: -r["cents"])

    by_model_events: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_conv: dict[str, dict] = {}
    headless = {"n": 0, "cents": 0.0}
    interactive = {"n": 0, "cents": 0.0}
    other_cents = 0.0
    cursor_cents = 0.0
    grok_bot_cents = 0.0

    for ev in events:
        tu = ev.get("tokenUsage") or {}
        cents = _event_cents(ev)
        model = ev.get("model") or "?"
        pool = pool_for_model(model)
        ts = to_int(ev.get("timestamp"))
        local_dt = datetime.fromtimestamp(ts / 1000).astimezone() if ts else None
        day = local_dt.strftime("%Y-%m-%d") if local_dt else "?"
        m = by_model_events.setdefault(
            model,
            {"n": 0, "cents": 0.0, "in": 0, "out": 0, "cw": 0, "cr": 0, "headless": 0, "pool": pool},
        )
        m["n"] += 1
        m["cents"] += cents
        m["in"] += to_int(tu.get("inputTokens"))
        m["out"] += to_int(tu.get("outputTokens"))
        m["cw"] += to_int(tu.get("cacheWriteTokens"))
        m["cr"] += to_int(tu.get("cacheReadTokens"))
        d = by_day.setdefault(day, {"n": 0, "cents": 0.0, "in": 0, "out": 0})
        d["n"] += 1
        d["cents"] += cents
        d["in"] += to_int(tu.get("inputTokens"))
        d["out"] += to_int(tu.get("outputTokens"))
        conv = str(ev.get("conversationId") or ev.get("cloudAgentId") or "(none)")
        if conv in ("null", "None", ""):
            conv = str(ev.get("cloudAgentId") or "(none)")
        c = by_conv.setdefault(
            conv,
            {
                "n": 0,
                "cents": 0.0,
                "other_cents": 0.0,
                "cursor_cents": 0.0,
                "headless": 0,
                "models": {},
                "in": 0,
                "out": 0,
                "cr": 0,
            },
        )
        c["n"] += 1
        c["cents"] += cents
        c["in"] += to_int(tu.get("inputTokens"))
        c["out"] += to_int(tu.get("outputTokens"))
        c["cr"] += to_int(tu.get("cacheReadTokens"))
        c["models"][model] = c["models"].get(model, 0) + 1
        if pool == "other":
            c["other_cents"] += cents
            other_cents += cents
        elif pool == "cursor":
            c["cursor_cents"] += cents
            cursor_cents += cents
        else:
            grok_bot_cents += cents
        if ev.get("isHeadless"):
            m["headless"] += 1
            c["headless"] += 1
            headless["n"] += 1
            headless["cents"] += cents
        else:
            interactive["n"] += 1
            interactive["cents"] += cents

    conversations = []
    for cid, c in by_conv.items():
        conversations.append(
            {
                "id": cid,
                "name": label_conversation(cid, c["models"], local, cloud),
                **c,
            }
        )
    conversations.sort(key=lambda r: -r["cents"])

    days = [{"date": k, **by_day[k]} for k in sorted(by_day)]
    event_models = [
        {"model": k, **v} for k, v in sorted(by_model_events.items(), key=lambda kv: -kv[1]["cents"])
    ]

    api_pct = to_float(plan.get("apiPercentUsed"))
    auto_pct = to_float(plan.get("autoPercentUsed"))
    total_pct = to_float(plan.get("totalPercentUsed"))
    total_spend = to_float(plan.get("totalSpend")) or to_float(agg.get("totalCostCents"))
    included = to_int(plan.get("includedSpend") or plan.get("limit"))
    bonus = to_int(plan.get("bonusSpend"))
    on_demand = (summary.get("individualUsage") or {}).get("onDemand") or {}

    now = datetime.now(timezone.utc)
    start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else None
    end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else None
    elapsed_days = ((now - start_dt).total_seconds() / 86400.0) if start_dt else 0.0
    remain_days = ((end_dt - now).total_seconds() / 86400.0) if end_dt else 0.0
    api_days_left = None
    if elapsed_days > 0.15 and api_pct > 0:
        rate = api_pct / elapsed_days
        if rate > 0:
            api_days_left = max(0.0, (100.0 - api_pct) / rate)

    grok_bot_pct = to_float(grok_bot.get("usagePercent")) if grok_bot else None
    report = {
        "fetched_at": time.time(),
        "email": email,
        "plan": (summary.get("membershipType") or plan_name or "?").title(),
        "cycle_start": start,
        "cycle_end": end,
        "elapsed_days": elapsed_days,
        "remain_days": remain_days,
        "api_pct": api_pct,
        "auto_pct": auto_pct,
        "total_pct": total_pct,
        "api_days_left": api_days_left,
        "total_spend_cents": total_spend,
        "included_cents": included,
        "bonus_cents": bonus,
        "on_demand_enabled": bool(on_demand.get("enabled")),
        "auto_msg": summary.get("autoModelSelectedDisplayMessage") or period.get("autoModelSelectedDisplayMessage"),
        "api_msg": summary.get("namedModelSelectedDisplayMessage") or period.get("namedModelSelectedDisplayMessage"),
        "grok_bot_pct": grok_bot_pct,
        "grok_bot_reset": grok_bot.get("nextResetTimestampUtc") if grok_bot else None,
        "models": models,
        "event_models": event_models,
        "days": days,
        "conversations": conversations[:60],
        "headless": headless,
        "interactive": interactive,
        "other_event_cents": other_cents,
        "cursor_event_cents": cursor_cents,
        "grok_bot_event_cents": grok_bot_cents,
        "events_fetched": len(events),
        "events_total": total_events,
        "agg_input": to_int(agg.get("totalInputTokens")),
        "agg_output": to_int(agg.get("totalOutputTokens")),
        "agg_cache_write": to_int(agg.get("totalCacheWriteTokens")),
        "agg_cache_read": to_int(agg.get("totalCacheReadTokens")),
    }
    report["findings"] = build_findings(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    return report


def build_findings(report: dict) -> list[str]:
    notes = []
    api_pct = report.get("api_pct") or 0
    auto_pct = report.get("auto_pct") or 0
    total_pct = report.get("total_pct") or 0
    elapsed = report.get("elapsed_days") or 0
    days_left = report.get("api_days_left")
    notes.append(
        f"The ~{api_pct:.0f}% you are seeing is the Other Models pool (Claude / GPT / Gemini), "
        f"not total Ultra usage. Cursor Models (Grok / Composer) are at {auto_pct:.0f}%. "
        f"Overall included usage is {total_pct:.0f}%."
    )
    if elapsed:
        notes.append(
            f"This cycle is {elapsed:.1f} days in (resets in {report.get('remain_days') or 0:.0f} days). "
            "That is why it feels fast: a 30-day meter is already most of the way gone on the expensive pool."
        )
    if days_left is not None:
        notes.append(
            f"At this Other-Models burn rate the remaining {max(0, 100-api_pct):.0f}% lasts about "
            f"{days_left:.1f} days."
        )
    notes.append(
        f"Included compute {fmt_money(report.get('included_cents') or 0)} is used up; "
        f"bonus so far is {fmt_money(report.get('bonus_cents') or 0)}. "
        f"On-demand is {'on' if report.get('on_demand_enabled') else 'off'}."
    )
    hl = report.get("headless") or {}
    inter = report.get("interactive") or {}
    hl_c = hl.get("cents") or 0
    all_c = hl_c + (inter.get("cents") or 0)
    if all_c:
        notes.append(
            f"Cloud / headless agents are {hl.get('n') or 0} of {report.get('events_fetched') or 0} events "
            f"but {100.0 * hl_c / all_c:.0f}% of dollars ({fmt_money(hl_c)}). "
            "A few Max-mode Opus turns overnight cost more than hundreds of Grok Bot pings."
        )
    other_chats = [
        c for c in report.get("conversations") or [] if (c.get("other_cents") or 0) >= 1000
    ]
    if other_chats:
        top = other_chats[:4]
        bits = ", ".join(f"{c['name']} {fmt_money(c['other_cents'])}" for c in top)
        notes.append(f"Other-Models dollars by chat: {bits}.")
    grok_fast = next(
        (m for m in report.get("models") or [] if "grok-4.6" in str(m.get("model")) and "fast" in str(m.get("model"))),
        None,
    )
    if grok_fast:
        cr = grok_fast.get("cache_read") or 0
        notes.append(
            f"Grok 4.6 Fast billed {fmt_money(grok_fast['cents'])}. Most of that is cache reads "
            f"({fmt_int(cr)} tokens). Fast mode charges about 2x Grok cache-read vs non-fast, "
            "so huge chats in Fast are a silent leak even though they sit in the Cursor Models pool."
        )
    gb = report.get("grok_bot_pct")
    if gb is not None:
        notes.append(
            f"Grok Bot is a separate weekly meter at {gb:.0f}% "
            f"(resets {str(report.get('grok_bot_reset') or '')[:10]}). "
            "Those sessions do not explain the 75% Other-Models bar."
        )
    notes.append(
        "Proper vs waste: real Nova work happened (execution packs, news, IB). "
        "The waste is model + window choice -- Opus xhigh / GPT-5.6 Sol High / Grok Fast Max "
        "on already-huge contexts, plus stacking many cloud agents overnight. "
        "Composer or Grok (non-fast) on a fresh chat is the cheap path; save Opus/Sol for one hard bug."
    )
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
    print(f"{report['plan']}  other {report['api_pct']:.1f}%  cursor {report['auto_pct']:.1f}%  overall {report['total_pct']:.0f}%")
    print(f"spend {fmt_money(report['total_spend_cents'])}  events {report['events_fetched']}")
    for line in report.get("findings") or []:
        print("-", line)
