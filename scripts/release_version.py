"""TOKENMAXXING release identity: vNNN, the commit count of the released commit.

Ported from Nova's tools/bump_version.py. Every commit on main is numbered by
`git rev-list --count HEAD`, which only grows while main is never rewritten.
The public tag is vNNN (at least three digits: v001, v042, v1000). electron-builder
needs semver, so the packed app is 3.0.N with the same N. Major 3 sits above the
last hand-numbered release (2.1.1), so installs of it update to these.
app/update-policy.js maps 3.0.N back to vNNN for display.

The version is never committed: app/package.json stays 0.0.0-dev in git and
scripts/pack_windows.py hands electron-builder the version at pack time, so no
PR touches a version line and amend/rebase/cherry-pick stay safe.

    python scripts/release_version.py --show
    python scripts/release_version.py --print-tag        # v042
    python scripts/release_version.py --print-version    # 3.0.42
    python scripts/release_version.py --ensure-tag --push-tag
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MAJOR = 3
PACKAGE_MINOR = 0
TAG_MIN_WIDTH = 3
TAG_RE = re.compile(r"^v(\d+)$")
SEMVER_RE = re.compile(rf"^{PACKAGE_MAJOR}\.{PACKAGE_MINOR}\.(\d+)$")


def git_commit_count(ref: str = "HEAD") -> int:
    out = subprocess.check_output(["git", "rev-list", "--count", ref], cwd=REPO_ROOT, text=True)
    return int(out.strip())


def format_release_tag(count: int) -> str:
    if count < 1:
        raise ValueError(f"commit count must be >= 1, got {count}")
    return f"v{count:0{TAG_MIN_WIDTH}d}"


def format_package_version(count: int) -> str:
    if count < 1:
        raise ValueError(f"commit count must be >= 1, got {count}")
    return f"{PACKAGE_MAJOR}.{PACKAGE_MINOR}.{count}"


def parse_release_count(text: str) -> int | None:
    """`v042` or `3.0.42` -> 42; anything else (2.1.1, 0.0.0-dev) -> None."""
    raw = text.strip()
    match = TAG_RE.match(raw) or SEMVER_RE.match(raw)
    return int(match.group(1)) if match else None


def existing_tag_commit(tag: str) -> str | None:
    if not subprocess.check_output(["git", "tag", "-l", tag], cwd=REPO_ROOT, text=True).strip():
        return None
    return subprocess.check_output(["git", "rev-list", "-n", "1", tag], cwd=REPO_ROOT, text=True).strip()


def ensure_tag(*, push: bool = False) -> int:
    """Create vNNN on HEAD when missing. Never moves an existing tag."""
    tag = format_release_tag(git_commit_count())
    existing = existing_tag_commit(tag)
    if existing:
        print(f"release_version: tag {tag} already exists ({existing[:12]})")
        return 0
    subprocess.run(["git", "tag", tag], cwd=REPO_ROOT, check=True)
    print(f"release_version: created tag {tag}")
    if push:
        subprocess.run(["git", "push", "origin", tag], cwd=REPO_ROOT, check=True)
        print(f"release_version: pushed {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TOKENMAXXING commit-count vNNN versioning")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="print the count, tag and packed semver")
    group.add_argument("--print-tag", action="store_true", help="print only HEAD's vNNN tag")
    group.add_argument("--print-version", action="store_true", help="print only HEAD's packed semver")
    group.add_argument("--ensure-tag", action="store_true", help="create the vNNN git tag on HEAD if missing")
    parser.add_argument("--push-tag", action="store_true", help="with --ensure-tag, push the new tag")
    args = parser.parse_args(argv)
    if args.ensure_tag:
        return ensure_tag(push=args.push_tag)
    count = git_commit_count()
    if args.print_tag:
        print(format_release_tag(count))
    elif args.print_version:
        print(format_package_version(count))
    else:
        print(f"commits={count}\ntag={format_release_tag(count)}\nversion={format_package_version(count)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
