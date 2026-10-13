"""Does a change need a new installer? Documentation-only changes do not.

Ported from Nova's tools/ci_scope.py, reduced to the one question this repo
asks. A main commit that only touches documentation still gets its vNNN tag but
no Release: installed copies read the latest Release's latest.yml, so a Release
without one would break every copy's update check, and one carrying an
unchanged app would make every copy download it for nothing.

When the diff is unknown (a tag push, a manual run, a new branch) the answer is
yes: a release always packs when in doubt.

Writes `app=true|false` to $GITHUB_OUTPUT; no other side effects.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Nothing here is packaged into the app (app/package.json `build.files` and
# `extraResources` name everything that is).
DOC_FILES = {"LICENSE", "screenshot.png", ".github/pull_request_template.md"}
DOC_PREFIXES = ("docs/", ".github/ISSUE_TEMPLATE/")
SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


def is_doc(path: str) -> bool:
    path = path.replace("\\", "/")
    return path.endswith(".md") or path in DOC_FILES or path.startswith(DOC_PREFIXES)


def needs_app(paths: list[str] | None) -> bool:
    """True unless every changed path is documentation. Unknown or empty -> True."""
    if not paths:
        return True
    return not all(is_doc(path) for path in paths)


def changed_paths(event_name: str, event: dict, repo: Path = REPO_ROOT) -> list[str] | None:
    """The full git diff of a PR or push, or None when there is no usable base."""
    if event_name == "pull_request":
        base, head, sep = event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"], "..."
    elif event_name == "push":
        base, head, sep = event.get("before", ""), event.get("after", ""), ".."
    else:
        return None
    if not all(isinstance(sha, str) and SHA_RE.fullmatch(sha) for sha in (base, head)) or set(base) == {"0"}:
        return None
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}{sep}{head}"],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    paths = changed_paths(event_name, event)
    app = needs_app(paths)
    described = "unknown diff" if paths is None else f"{len(paths)} changed path(s)"
    print(f"ci_scope: {described}; app={'true' if app else 'false'}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"app={'true' if app else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
