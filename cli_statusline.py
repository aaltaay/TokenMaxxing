"""Claude Code status line that also tells TOKENMAXXING which chat is open.

Claude Code runs this for the session a person is typing in and hands it that
session's own status payload on stdin. Only identifiers and counters are kept;
no transcript content, file path, or prompt text is written or printed.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

POINTER_NAME = "claude-active.json"


def hud_home():
    return Path(os.environ.get("TOKENMAXXING_HOME") or Path.home() / ".tokenmaxxing")


def pointer(payload, now=None):
    """Identifiers and counters only; unknown fields stay absent."""
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    window = payload.get("context_window")
    cost = payload.get("cost")
    record = {"session_id": session, "at": now if now is not None else time.time()}
    model = (payload.get("model") or {}).get("display_name") if isinstance(payload.get("model"), dict) else None
    if isinstance(model, str):
        record["model"] = model
    if isinstance(window, dict):
        numbers = {key: window[key] for key in
                   ("total_input_tokens", "total_output_tokens", "context_window_size", "used_percentage")
                   if isinstance(window.get(key), (int, float)) and not isinstance(window.get(key), bool)}
        if numbers:
            record["context_window"] = numbers
    total = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        record["cost_usd"] = total
    return record


def write_pointer(record, home=None):
    """Replace the pointer atomically so a reader never sees a half file."""
    if not record:
        return False
    directory = home or hud_home()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".claude-active-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        os.replace(temporary, directory / POINTER_NAME)
        return True
    except OSError:
        return False


def line(payload):
    model = (payload.get("model") or {}).get("display_name") or "?"
    window = payload.get("context_window") or {}
    inp = window.get("total_input_tokens")
    out = window.get("total_output_tokens")
    used = window.get("used_percentage")
    return (f"{model}  in {inp if inp is not None else '?'}  out {out if out is not None else '?'}  "
            f"ctx {f'{float(used):.0f}%' if used is not None else '?%'}")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    write_pointer(pointer(payload))
    sys.stdout.write(line(payload) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
