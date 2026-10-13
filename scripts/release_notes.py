"""Release notes for a vNNN GitHub Release, from the PR that made it.

Ported from Nova's tools/release_notes.py. Every app-changing merge to main
becomes a Release (.github/workflows/release.yml). Main uses merge commits, so
the workflow looks up the merged PR and passes its title and body as the
message; a direct push passes the commit message. The PR template's `## What`
opens with a sentence or three for the user, which becomes the Release body: a
readable "What's new" section, the installer text, and one hidden
machine-readable line for a future in-app What's new view:

    <!-- tokenmaxxing-release-notes {"schema_version": 1, "tag": "v042", ...} -->

Pure text in, text out; the only I/O is the CLI reading the message file.

    python scripts/release_notes.py body --tag v042 --message-file msg.txt [--pr 14]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
MARKER = "tokenmaxxing-release-notes"
SUMMARY_MAX_CHARS = 500
POINT_MAX_CHARS = 160
POINTS_MAX = 8

_TAG_RE = re.compile(r"^v\d+$")
_SUBJECT_RE = re.compile(r"^(?P<kind>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<title>.+)$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<name>.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LEAD_RE = re.compile(r"^\*\*(?P<lead>[^*]+?)\*\*")
_ASIDE_RE = re.compile(r"\s*\(#\d+[^()]*\)")
_SKIP_PARAGRAPH_RE = re.compile(r"^(?:>|\||<!--)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")


def parse_subject(subject: str) -> tuple[str | None, str | None, str]:
    """`fix(sessions): Follow the open chat` -> ("fix", "sessions", "Follow the open chat")."""
    line = subject.strip()
    match = _SUBJECT_RE.match(line)
    if not match:
        return None, None, line
    title = match.group("title").strip()
    return match.group("kind"), match.group("scope"), title[:1].upper() + title[1:]


def section(message: str, name: str) -> str:
    """The body of `## <name>` (case-insensitive), up to the next heading; '' when absent."""
    out: list[str] = []
    inside = False
    for line in message.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            if inside:
                break
            inside = heading.group("name").strip().lower() == name.lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def blocks(text: str) -> list[list[str]]:
    """Blank-line separated blocks, each a list of its lines. HTML comments are dropped first."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    found: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line.rstrip())
        elif current:
            found.append(current)
            current = []
    if current:
        found.append(current)
    return found


def plain(text: str) -> str:
    """Markdown inline syntax to plain text: links, emphasis, code, HTML, PR asides."""
    out = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    out = re.sub(r"<[^>]+>", "", out)
    out = out.replace("**", "").replace("__", "").replace("`", "")
    out = _ASIDE_RE.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


def clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a sentence end when one is close enough."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.start() for m in _SENTENCE_END_RE.finditer(head)]
    if ends and ends[-1] >= limit // 2:
        return head[: ends[-1]].rstrip()
    return head[: limit - 1].rstrip() + "…"


def _is_list(block: list[str]) -> bool:
    return bool(_LIST_RE.match(block[0]))


def _list_items(block: list[str]) -> list[str]:
    items: list[str] = []
    for line in block:
        if _LIST_RE.match(line) and not line.startswith((" ", "\t")):
            items.append(_LIST_RE.sub("", line, count=1))
        elif items and line.startswith((" ", "\t")) and not _LIST_RE.match(line):
            items[-1] += " " + line.strip()
    return items


def _point(item: str) -> str:
    lead = _LEAD_RE.match(item.strip())
    text = plain(lead.group("lead")) if lead else plain(item)
    first = _SENTENCE_END_RE.split(text, maxsplit=1)[0]
    return clip(first.rstrip(".,;: "), POINT_MAX_CHARS)


def _points(summary: str, items: list[str]) -> list[str]:
    """A list the summary introduces ("...:"), or the bold lead-ins of a list mostly made of them.

    A list of plain items after a self-contained paragraph is engineering detail
    (file paths, flags), not something a user needs in What's new.
    """
    if summary.endswith(":"):
        chosen = items
    else:
        chosen = [item for item in items if _LEAD_RE.match(item.strip())]
        if not chosen or len(chosen) * 2 < len(items):
            return []
    return [p for p in (_point(item) for item in chosen) if p][:POINTS_MAX]


def user_summary(message: str) -> tuple[str, list[str]]:
    """The What section's first paragraph, and the list right after it."""
    source = section(message, "What")
    if not source:
        # No PR template (a direct push): the commit body after its subject line.
        source = "\n".join(message.splitlines()[1:])
    found = blocks(source)
    for index, block in enumerate(found):
        if _is_list(block) or _HEADING_RE.match(block[0]) or _SKIP_PARAGRAPH_RE.match(block[0].lstrip()):
            continue
        summary = clip(plain(" ".join(block)), SUMMARY_MAX_CHARS)
        if not summary:
            continue
        following = found[index + 1] if index + 1 < len(found) else None
        points = _points(summary, _list_items(following)) if following and _is_list(following) else []
        return summary, points
    return "", []


def notes_record(tag: str, message: str, pr: int | None = None) -> dict:
    lines = message.strip().splitlines()
    kind, scope, title = parse_subject(lines[0] if lines else "")
    summary, points = user_summary(message)
    return {
        "schema_version": SCHEMA_VERSION,
        "tag": tag,
        "title": plain(title),
        "kind": kind,
        "scope": scope,
        "pr": pr,
        "summary": summary,
        "points": points,
    }


def marker_line(record: dict) -> str:
    """One HTML comment GitHub hides; `<` / `>` escaped so the JSON can never close it."""
    payload = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- {MARKER} {payload} -->"


def installer_text(tag: str) -> str:
    return (
        f"TOKENMAXXING {tag} for Windows.\n\n"
        f"- Installer: TokenMaxxing-Setup-{tag}.exe (NSIS, x64)\n"
        "- latest.yml + .blockmap are the in-app update feed: installed copies download this "
        "build in the background and install it when you choose Restart to update.\n\n"
        "The installer isn't code-signed yet, so SmartScreen may warn on a fresh download.\n"
        "GitHub also attaches Source code zip/tar automatically. Those are not the app."
    )


def release_body(tag: str, message: str, pr: int | None = None) -> str:
    if not _TAG_RE.match(tag):
        raise ValueError(f"not a release tag: {tag!r}")
    record = notes_record(tag, message, pr)
    heading = f"**{record['title']}**" if record["title"] else f"**TOKENMAXXING {tag}**"
    meta = " · ".join(x for x in (record["kind"], record["scope"], f"#{pr}" if pr else None) if x)
    parts = [f"## What's new in {tag}", f"{heading}  \n{meta}" if meta else heading]
    if record["summary"]:
        parts.append(record["summary"])
    if record["points"]:
        parts.append("\n".join(f"- {point}" for point in record["points"]))
    parts += [marker_line(record), "---", installer_text(tag)]
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    body = sub.add_parser("body", help="print the Release body for a tag")
    body.add_argument("--tag", required=True)
    body.add_argument("--message-file", required=True, type=Path)
    body.add_argument("--pr", type=int, default=None)
    args = parser.parse_args(argv)
    message = args.message_file.read_text(encoding="utf-8")
    # UTF-8 whatever the console's code page: the body carries "·" and "…".
    sys.stdout.buffer.write(release_body(args.tag, message, args.pr).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
