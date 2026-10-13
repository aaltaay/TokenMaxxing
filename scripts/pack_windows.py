"""Pack the Windows installer and prove its update feed names it.

    python scripts/pack_windows.py              # vNNN from this checkout's history
    python scripts/pack_windows.py --tag v042   # CI: refuse a tag that is not HEAD's

Runs electron-builder with the release version (passed as extraMetadata, never
written to app/package.json) and `--publish never`: publishing is the release
workflow's own step, after this check. It then refuses to pass unless
TokenMaxxing-Setup-vNNN.exe, its .blockmap and latest.yml all exist and
latest.yml advertises exactly this version and file. Installed copies trust that
feed to decide whether they are behind, so a Release whose feed names another
build is worse than none.

Expects the engine already built (scripts/build-engine.py) and `npm ci` done in app/.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_version  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
DIST_DIR = REPO_ROOT / "dist"
# Read by app/package.json `build.win.artifactName`.
TAG_ENV = "TOKENMAXXING_RELEASE_TAG"


def setup_name(tag: str) -> str:
    return f"TokenMaxxing-Setup-{tag}.exe"


def _yaml_value(text: str, key: str, *, nested: bool = False) -> str | None:
    """A scalar from latest.yml; `nested` reads it from the `files:` entry instead of the top level."""
    indent = r"[ \t]+(?:-[ \t]+)?" if nested else ""
    match = re.search(rf"(?m)^{indent}{re.escape(key)}:\s*['\"]?([^'\"\r\n]+?)['\"]?\s*$", text)
    return match.group(1) if match else None


def feed_problems(dist: Path, tag: str, version: str) -> list[str]:
    """Everything wrong with the packed installer and its feed; empty when they agree."""
    setup = dist / setup_name(tag)
    feed = dist / "latest.yml"
    problems = [f"missing {path.name}" for path in (setup, setup.with_name(setup.name + ".blockmap"), feed)
                if not path.is_file()]
    if feed.is_file():
        text = feed.read_text(encoding="utf-8")
        if _yaml_value(text, "version") != version:
            problems.append(f"latest.yml advertises version {_yaml_value(text, 'version')!r}, not {version!r}")
        if _yaml_value(text, "path") != setup.name:
            problems.append(f"latest.yml points at {_yaml_value(text, 'path')!r}, not {setup.name!r}")
        size = _yaml_value(text, "size", nested=True)
        if setup.is_file() and size != str(setup.stat().st_size):
            problems.append(f"latest.yml size {size} is not {setup.name}'s {setup.stat().st_size}")
    return problems


def electron_builder() -> str:
    name = "electron-builder.cmd" if os.name == "nt" else "electron-builder"
    path = APP_DIR / "node_modules" / ".bin" / name
    if not path.is_file():
        raise SystemExit(f"{path} not found; run `npm ci` in app/ first")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", help="the vNNN this build must be (CI); defaults to HEAD's")
    args = parser.parse_args(argv)
    count = release_version.git_commit_count()
    tag = release_version.format_release_tag(count)
    if args.tag and args.tag != tag:
        raise SystemExit(f"{args.tag} is not this commit's revision {tag}")
    version = release_version.format_package_version(count)
    print(f"pack_windows: {tag} -> {version}")
    subprocess.run(
        [electron_builder(), "--win", "nsis", "--x64", "--publish", "never", f"-c.extraMetadata.version={version}"],
        cwd=APP_DIR, env={**os.environ, TAG_ENV: tag, "CSC_IDENTITY_AUTO_DISCOVERY": "false"}, check=True,
    )
    problems = feed_problems(DIST_DIR, tag, version)
    if problems:
        raise SystemExit("pack_windows: the installer and its update feed disagree:\n  " + "\n  ".join(problems))
    print(f"pack_windows: {setup_name(tag)}, its .blockmap and latest.yml agree on {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
