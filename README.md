# Cursor Token HUD

Always-on-top window that answers two questions the Cursor UI splits apart:

1. **This cycle -- where did the 75% go?** Other Models vs Cursor Models, dollars by chat, cloud vs local, burn rate.
2. **This chat -- how fat is the context window right now?** Same snapshot Cursor already writes to `state.vscdb`.

This is not an official Cursor product. Billing numbers come from the same unofficial `cursor.com` dashboard session the app already has (`cursorAuth/accessToken`). That JWT is never written to disk by this tool.

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

## Requirements

- Python 3.10+ (stdlib only: `tkinter`, `sqlite3`, `urllib`)
- Cursor desktop, signed in, with at least one agent chat so `state.vscdb` exists

## Run it

```bash
python token_hud.py
```

On Windows, `start.bat` launches it without a console (`pythonw` if available). Closing the window is fine. Starting it again only allows one copy.

`python cursor_usage.py` prints a text cycle summary (add `--refresh` to bypass the 3-minute cache).

The HUD follows `cursor/glass.selectedAgent`. If the tab switch lags, click a chat in the list, or hit **Follow Cursor tab**.

Cycle usage refreshes about every 3 minutes, or when you click **Refresh usage**. Local context still updates about once a second.

### Database path

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

Cache file (no secrets): `~/.cursor/token-hud/cycle-cache.json`

### Open with Cursor (optional)

1. Copy `token_hud.py`, `cursor_usage.py`, `start.bat`, and `start.vbs` to `~/.cursor/token-hud/`
2. Copy `examples/start-token-hud.cmd` to `~/.cursor/hooks/start-token-hud.cmd`
3. Merge `examples/hooks.json` into `~/.cursor/hooks.json`

`sessionStart` then launches the HUD when an agent session starts. If it is already running, the second launch exits immediately.

On Windows you can also drop `start.vbs` in the Startup folder, and keep a Desktop / Start Menu shortcut named **Cursor tokens**.

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
