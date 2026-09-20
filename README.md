# TOKENMAXXING

A local, always-on-top dashboard for reported Cursor, Codex, and Claude usage.
The Electron window reads data through a Python bridge.

## What it shows

- **Overview:** Cursor quota percentages, reported cycle usage value, token totals, and recorded event counts; Codex and Claude quota percentages when their services return them.
- **Sessions:** choose Codex, Claude Code, or Cursor. On Windows, Codex defaults to **Open Codex chat**, matching the desktop's accessible document title to the local session index. Switching chats changes the displayed session within a few seconds, even when background tasks are running. **Latest activity** follows the newest log instead; selecting a session pins it. Selection is saved across restarts. Codex and Claude show the last recorded request's token usage; Cursor shows its saved context snapshot and estimated category breakdown. Unknown metrics remain unavailable.
- **Resets:** reset times returned by the provider alongside the relevant usage percentage. A countdown is time until a reported reset, not an allowance of working hours.

Missing values display as unavailable. A failed connection, missing quota field, or expired reading never becomes a zero-percent meter or a guessed countdown. Last-known Cursor snapshots carry their age and stale status.

## Data sources and limits

| Data | Source |
|---|---|
| Cursor quota and billing-cycle boundaries | Cursor dashboard usage-summary and current-period endpoints |
| Cursor cycle token totals and model usage values | Cursor dashboard aggregated-usage endpoint |
| Cursor event counts and conversation activity | Cursor dashboard paginated event history |
| Cursor selected chat and context snapshot | Cursor's local `state.vscdb` database |
| Grok Bot usage | Cursor's `GetSandUsageStatus` endpoint |
| Codex quota and resets | Installed Codex client's `account/rateLimits/read` response |
| Claude quota and resets | Claude Code's OAuth usage endpoint, using an existing subscription sign-in |

Cursor dashboard endpoints are unofficial and may change. This project is independent of Cursor, OpenAI, and Anthropic.

**Usage value is not necessarily money charged.** Cursor's cycle value can include included and bonus usage. Event-level `tokenUsage.totalCents` values may differ from the dashboard cycle total; they are separate reported metrics and must not be treated as reconciled billing. The app does not combine raw event values with `chargedCents` or infer model prices.

Event history is fetched beyond the first page until the reported count is reached, subject to bounded time and pagination limits. If retrieval is incomplete, the app labels that state and withholds event-derived totals. The report also records data sources, fetch time, and completeness.

Codex and Claude may return different quota windows for different accounts. Only returned percentages and timestamps are displayed; subscription price does not determine a fixed number of working hours. If a provider cannot be read, its row explains that it is unavailable.

## Connect your accounts

Existing Cursor, Codex, and Claude Code sign-ins are detected automatically. When Claude or Codex needs authentication, click **Reconnect** in Overview or Limits & resets. TOKENMAXXING launches the installed provider's official browser sign-in. Complete sign-in on the provider's page; the app detects completion and fetches verified usage automatically.

While sign-in is pending, you can reopen its page or cancel from the app. **Retry usage** checks again without starting another sign-in. If the required client is missing or too old, the app offers its official installation page. Authentication failures stay unavailable until a real quota response arrives.

The legacy `reset_schedule.py` utility and saved schedule settings remain for compatibility. Its manual schedules are not quota evidence and are not used to populate provider usage meters or reset timestamps in the dashboard.

## Run locally

### Windows installer and automatic updates

Download the Windows x64 installer from [GitHub Releases](https://github.com/aaltaay/TokenMaxxing/releases/latest). It includes the Python engine; installed copies do not need Python, Node.js, or a Git checkout. Provider clients and their sign-ins are still required for their respective usage feeds.

Installed copies check for stable releases on startup and every six hours, download a newer version, and offer **Restart to update** at the bottom of the window. Updates are installed only when you select that button. **Check for updates** also runs a check manually. Connection failures leave the running version usable. Source launches display the version but do not auto-update.

To publish: bump `app/package.json` and its lockfile to a higher version, commit, and push a matching tag such as `v2.1.0`. The Windows release workflow runs tests, bundles the Python bridge, builds an NSIS installer, and publishes the installer, blockmap, and `latest.yml` together. Ordinary pushes to `main` do not release. A manual workflow run builds downloadable artifacts without publishing. The first installer must be installed manually before automatic updates are available.

For a local build on Windows: install `scripts/requirements-build.txt` with Python 3.12, run `python scripts/build-engine.py`, then run `npm ci` and `npm run dist:win` from `app/`. Output is in `dist/`. No signing certificate is configured yet, so Windows may show an unknown-publisher warning. User settings and provider credentials are stored outside the installation and are preserved across updates.

### Running from source

Requirements:

- Node.js 18 or newer for Electron.
- Python 3.10 or newer; the engine uses the standard library. Set `TOKEN_HUD_PYTHON` to select an interpreter.
- A signed-in Cursor desktop installation for Cursor data.
- An existing Codex sign-in and installed Codex client for Codex data.
- An existing supported Claude Code subscription sign-in for Claude data.

From the repository:

```text
start.bat          Windows
./start.sh         macOS / Linux
```

The launcher installs Electron dependencies on first run. Alternatively, run `npm install` and `npm start` inside `app/`.

The app refreshes data automatically. Use the title-bar refresh button to request a fresh account reading. In Sessions, **Open Codex chat** uses a read-only Windows accessibility helper that reads only the Codex document title, never transcript text or keyboard input. An unavailable title, duplicate title, cloud chat, or missing local record shows unavailable rather than a background task's usage. When several Codex windows are visible, the focused one is used; an ambiguous selection is unavailable. This depends on Codex's current accessibility layout; other platforms retain **Latest activity**. Choosing a session pins it; the follow button resumes the selected automatic mode. Up to 100 recent session files plus an explicitly matched older session are inspected using bounded log tails. Subagent logs are excluded. Missing token records do not become zero usage, and request usage is not presented as live context occupancy. Cursor selection skips chat headers with missing stored data.

## Local storage and credentials

### Reset alerts and SMS

While TOKENMAXXING is running (including minimized), a main-process monitor checks provider quotas every minute and alert eligibility every five seconds. It warns 15 minutes before a provider-reported reset and gives a separate reset confirmation when a fresh response advances that same window beyond its previous boundary. Merely reaching zero on a countdown does not claim a reset. Unavailable providers cannot generate verified alerts.

Each alert restores and brings forward the window, pulses a large notice, flashes the taskbar, and repeats a sound for at most one minute. The notice remains until dismissed. **Dismiss alert** or Escape stops attention immediately. **Pause alerts** disables automatic desktop and SMS alerts. **Preview full alert** exercises desktop attention without sending an SMS. Closing the app or sleeping the computer stops monitoring; no background service is installed.

Use **Connect Twilio SMS** under Limits & resets to enter your Account SID, Auth Token, Twilio sending number, and destination. Credentials are encrypted using Electron safeStorage (Windows DPAPI on Windows), never returned to the renderer after saving. **Send test SMS** sends a real message using your Twilio account. Automatic messages use the same two verified reset events. API acceptance is shown as queued/accepted, not delivered; errors are shown in the app. Uncertain sends are not blindly retried. Normal Twilio messaging charges and account/sender requirements apply.

Cursor's database is read from its standard installation location:

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

The runtime folder defaults to `~/.cursor/token-hud/`; `TOKEN_HUD_DIR` can override it. `cycle-cache.json` stores the last Cursor snapshot. `reset-schedule.json` stores notification preferences and legacy schedule settings. `reset-buzzed.json` tracks sent notifications.

The cache can include account email, local conversation names, and usage data. Treat it as private. Provider access credentials stay in memory and are not copied into the usage cache or returned to the renderer.

## Development

```text
app/main.js            Electron window and bridge process
app/preload.js         Renderer IPC interface
app/provider-link.js   Official CLI browser sign-in and connection status
app/renderer/          Dashboard UI
hud_bridge.py          NDJSON commands and data presentation
cursor_usage.py        Cursor database and dashboard reads
provider_usage.py      Codex and Claude quota reads
reset_schedule.py      Notification support and legacy schedule utility
```

Run tests with `python -m unittest discover -v` and `node --test app/test-*.cjs`. Windows installations without timezone data use the built-in Eastern timezone fallback; fallback boundary tests do not require `tzdata`.
