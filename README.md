# Cursor Token HUD

A small always-on-top window that shows how full the current Cursor chat's context window is.

It does not talk to Cursor's billing API. It reads the same local snapshot Cursor already writes for the in-app context ring: `composerData.promptTokenBreakdown` inside `state.vscdb`.

## Why this exists

Cursor's agent does not get `input_tokens` / `output_tokens` in its prompt, so asking the model to print usage at the end of every chat produces `not provided`. The real input/context number is already on disk. This tool puts that number on screen so you do not have to open [cursor.com/dashboard/usage](https://cursor.com/dashboard/usage) for every prompt.

This is not an official Cursor product.

## What the numbers mean

| You see | What it is |
|---|---|
| INPUT `used / limit` | Current context-window snapshot for the selected chat |
| Category rows | System prompt, tools, rules, skills, MCP, subagents, conversation |
| OUTPUT billed | **Not stored locally.** Cursor only keeps the input/context snapshot on disk. |

That input figure is the latest window size, not a cumulative per-turn bill. Cache tokens are not on disk either. For billed usage, use Cursor Settings or the usage dashboard.

## Requirements

- Python 3.10+ (stdlib only: `tkinter`, `sqlite3`)
- Cursor desktop, with at least one agent chat used so `state.vscdb` exists

## Run it

```bash
python token_hud.py
```

On Windows, `start.bat` launches it without a console (`pythonw` if available). Closing the window is fine. Starting it again only allows one copy.

The HUD follows `cursor/glass.selectedAgent` (the chat tab you have selected). If the tab switch lags, click a chat in the list, or hit **Follow Cursor tab**.

### Database path

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

### Open with Cursor (optional)

1. Copy `token_hud.py`, `start.bat`, and `start.vbs` to `~/.cursor/token-hud/`
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

CLI `total_output_tokens` is often null. Same limitation as the HUD: billed output is not a local number.

## Credits

The request came from [Ahmi Altaay](https://github.com/aaltaay): show token use in front of you, on every chat, without opening a website.

The implementation reads Cursor's existing local fields (`promptTokenBreakdown`, `contextTokensUsed`, `cursor/glass.selectedAgent`). Cursor's own context ring in the agent panel is the official UI for the same input snapshot.
