# Token HUD

Always-on-top window that answers three questions the Cursor UI splits apart:

1. **This cycle -- where did the 75% go?** Other Models vs Cursor Models, dollars by chat, cloud vs local, burn rate.
2. **This chat -- how fat is the context window right now?** Same snapshot Cursor already writes to `state.vscdb`.
3. **Resets -- when do Claude / Codex provider windows refresh?** 5-hour session and weekly countdowns, plus a Windows desktop buzz. Not the Cursor billing-cycle %.

This is not an official Cursor product. Billing numbers come from the same unofficial `cursor.com` dashboard session the app already has (`cursorAuth/accessToken`). That JWT is never written to disk by this tool. Source: [aaltaay/token-hud](https://github.com/aaltaay/token-hud).

## Why the old HUD felt useless

Cursor's agent prompt does not include billed `input_tokens` / `output_tokens`, and the previous HUD only showed the current chat's context ring. The dashboard 75% is a **different meter**: the **Other Models** pool (Claude, GPT, Gemini), not Grok/Composer, and not "how full this chat is."

## What the numbers mean

### This cycle tab

| You see | What it is |
|---|---|
| OTHER MODELS % | Named/API pool (the bar people read as "I used 75%") |
| CURSOR MODELS % | Grok 4.5/4.6 and Composer |
| Overall % | Combined included usage |
| Spend $ | Included + bonus compute consumed this billing cycle |
| Chat / cloud agent rows | Billed events grouped by `conversationId` / cloud agent, named from local chats when possible |
| Other $ | Slice of that row that hit the Other Models pool |
| cloud | Headless / cloud-agent events |
| Grok Bot % | Separate weekly meter. Not the 75%. |

### This chat tab

| You see | What it is |
|---|---|
| INPUT `used / limit` | Current context-window snapshot for the selected chat |
| Category rows | System prompt, tools, rules, skills, MCP, subagents, conversation |
| Context tax | Share of the window that is rules/tools/skills/MCP/subagents (paid again every turn) |
| THIS CYCLE $ | Billed events whose `conversationId` matches this chat |
| Next Grok Fast ~$ | Rough cache-read cost of one more turn at the current window size |

Billed **output** is not a local field. It is on the cycle tab, from the dashboard.

Cache tokens are not stored in `promptTokenBreakdown`. Cycle-tab cache-read totals come from the dashboard.

### Resets tab

These clocks are **provider session / weekly windows** (Claude and Codex/ChatGPT). They are **not** Cursor Other Models %, Cursor Models %, Grok Bot weekly, or the billing cycle. v1 does not scrape Anthropic or OpenAI.

| You see | What it is |
|---|---|
| Session countdown | Next 5-hour session boundary for that provider |
| Weekly countdown | Next weekday+time in `America/New_York` |
| next ... ET | Fire time shown in Eastern Time |
| rolling midnight ET | No `session_anchor` yet -- guess aligned to midnight ET. Mark session to sync. |
| PLACEHOLDER | Shipped weekly time. Paste the real one from Settings -> Usage. |
| last buzz | Last desktop reset buzz recorded for that clock |
| Mark ... session now | Sets `session_anchor` to now (next reset = now + 5h) |
| Test buzz | Plays the Windows `winsound` buzzer (or Tk bell elsewhere) and flashes a banner |

**Buzz rules**

- Windows: two-tone `winsound.Beep` plus a short topmost banner / window flash inside this HUD.
- Optional pre-warn (default **on**, **2 minutes** before). Config: `prewarn_enabled`, `prewarn_minutes`.
- The same reset id is persisted in `~/.cursor/token-hud/reset-buzzed.json` so one fire does not spam.
- Session clocks **do not buzz** until you mark a session start (otherwise the rolling guess would beep at 00:00 / 05:00 / 10:00 ET).
- Weekly clocks **do not buzz** while `weekly_placeholder` is true. After you paste the real weekday+time, set `"weekly_placeholder": false`.

**Config** (created with defaults if missing): `~/.cursor/token-hud/reset-schedule.json`

On Windows that is `C:\Users\<you>\.cursor\token-hud\reset-schedule.json`. Editing the JSON is enough; the HUD reloads on save.

Default shape (weekly times are placeholders):

```json
{
  "timezone": "America/New_York",
  "prewarn_minutes": 2,
  "prewarn_enabled": true,
  "buzz_enabled": true,
  "providers": {
    "claude": {
      "session_hours": 5,
      "session_anchor": null,
      "weekly_weekday": "thursday",
      "weekly_time": "09:00",
      "weekly_placeholder": true
    },
    "codex": {
      "session_hours": 5,
      "session_anchor": null,
      "weekly_weekday": "thursday",
      "weekly_time": "09:00",
      "weekly_placeholder": true
    }
  }
}
```

**Schedule math**

- Session with an anchor: windows are `anchor + n * session_hours`. Next fire is the next boundary after now.
- Session without an anchor: rolling 5-hour slots from midnight America/New_York. Countdown only; no buzz until you re-anchor.
- Weekly: next occurrence of `weekly_weekday` at `weekly_time` Eastern, then every 7 days.
- The HUD checks these clocks on the existing `root.after` loop (about once a second). It never blocks the UI thread for network.

CLI (no window):

```bash
python reset_schedule.py
python reset_schedule.py --mark-session claude
python token_hud.py --test-buzz
```

## UI

The HUD uses an Apple-inspired dark layout: near-black chrome (`#1c1c1e`), grouped cards, segmented tabs, thin capsule meters, and monospaced numbers. Visual / interaction polish only -- cycle fetch, chat follow, reset countdowns, Test buzz, and the single-instance lock are unchanged.

## Requirements

- Python 3.10+ (stdlib only: `tkinter`, `sqlite3`, `urllib`; `winsound` on Windows; `zoneinfo` with a built-in Eastern fallback)
- Cursor desktop, signed in, with at least one agent chat so `state.vscdb` exists (cycle + chat tabs). The Resets tab works without that.

## Run it

```bash
python token_hud.py
```

On Windows, `start.bat` launches it without a console (`pythonw` if available). Closing the window is fine. Starting it again only allows one copy.

`python cursor_usage.py` prints a text cycle summary (add `--refresh` to bypass the 3-minute cache).
`python reset_schedule.py` prints Claude / Codex reset countdowns.

The HUD follows `cursor/glass.selectedAgent`. If the tab switch lags, click a chat in the list, or hit **Follow Cursor tab**.

Cycle usage refreshes about every 3 minutes, or when you click **Refresh usage**. Local context still updates about once a second.

### Database path

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

Cache / config (no secrets):

- `~/.cursor/token-hud/cycle-cache.json` -- last Cursor dashboard snapshot
- `~/.cursor/token-hud/reset-schedule.json` -- Claude / Codex reset clocks
- `~/.cursor/token-hud/reset-buzzed.json` -- last-fired reset event ids (dedupe)

### Open with Cursor (optional)

1. Copy `token_hud.py`, `cursor_usage.py`, `reset_schedule.py`, `start.bat`, and `start.vbs` to `~/.cursor/token-hud/`
2. Copy `examples/start-token-hud.cmd` to `~/.cursor/hooks/start-token-hud.cmd`
3. Merge `examples/hooks.json` into `~/.cursor/hooks.json`

`sessionStart` then launches the HUD when an agent session starts. If it is already running, the second launch exits immediately.

On Windows you can also drop `start.vbs` in the Startup folder, and keep a Desktop / Start Menu shortcut named **Token HUD**.

### Cursor CLI status line (optional)

`cli_statusline.py` prints model, input, output, and context percent from the CLI status-line JSON payload. Point `~/.cursor/cli-config.json` at it:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python /path/to/cli_statusline.py",
    "padding": 1
  }
}
```

CLI `total_output_tokens` is often null. Billed output is on the HUD cycle tab.

## How to actually spend less

- Treat **Other Models** as the scarce pool. Opus / GPT Sol / Fable eat it. Grok and Composer do not.
- One Opus xhigh Max cloud turn on a huge window can cost ~$45. Three of those is a triple-digit hole.
- Grok Fast cache-read is about 2x non-fast. Fat chats in Fast quietly drain the Cursor pool even when Other Models is the bar you are staring at.
- Start a new chat instead of pushing a 200k-500k window through another expensive model.
- Cloud agents in parallel at 1am are real spend. Headless can be half the dollars and a tiny fraction of the event count.

## Notes

Reads Cursor's existing local fields (`promptTokenBreakdown`, `contextTokensUsed`, `cursor/glass.selectedAgent`) plus the unofficial dashboard endpoints documented by community tools such as [cursor-usage](https://github.com/javaisbetterthanpython/cursor-usage) and [OpenUsage](https://openusage.sh/docs/providers/cursor/). Cursor can change those endpoints without notice. Sign in again in the app if a fetch fails.
