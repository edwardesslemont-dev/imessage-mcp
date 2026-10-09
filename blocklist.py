"""blocklist.py — handles whose messages this server must never return.

The list is a plain text or Markdown file. Every line shaped like a bullet with
a handle in backticks is one entry; everything else is free text and ignored:

    - `+15555550123` a label for the human reading the file
    - `someone@example.com`

Default location is `~/.claude/imessage-mcp/blocklist.md` (override with
IMESSAGE_BLOCKLIST). It may be a symlink, so the real list can live wherever
its owner edits it.

Fail-closed by design. No file at the path means no blocklist. A path that
exists but cannot be read (a dangling symlink, a permission error) raises
BlocklistUnreadable, so callers return an error rather than an unfiltered read.

The file is re-read on every call: an edit applies to the next tool call with
no restart, and the file is a few lines long.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BLOCKLIST_PATH = Path(
    os.environ.get(
        "IMESSAGE_BLOCKLIST",
        str(Path.home() / ".claude" / "imessage-mcp" / "blocklist.md"),
    )
)

_ENTRY = re.compile(r"^\s*[-*]\s+`([^`]+)`")


class BlocklistUnreadable(RuntimeError):
    """The blocklist exists but could not be read, so nothing may be returned."""


def normalize(handle: str | None) -> str:
    """Comparable form of a handle: lowercased email, or the last 10 phone digits.

    chat.db stores +15555550123 where a contact card says (555) 555-0123, so
    phone numbers compare on digits, and on the last 10 so a missing country
    code still matches.
    """
    h = (handle or "").strip().lower()
    if not h:
        return ""
    if "@" in h:
        return h
    digits = "".join(c for c in h if c.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


def load() -> frozenset[str]:
    """Return the normalized blocked handles. Raises BlocklistUnreadable."""
    if not os.path.lexists(BLOCKLIST_PATH):
        return frozenset()
    try:
        raw = BLOCKLIST_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise BlocklistUnreadable(
            f"iMessage blocklist at {BLOCKLIST_PATH} exists but cannot be read "
            f"({type(e).__name__}: {e}). Refusing to return messages unfiltered."
        ) from e
    entries = set()
    for line in raw.splitlines():
        m = _ENTRY.match(line)
        if m and normalize(m.group(1)):
            entries.add(normalize(m.group(1)))
    return frozenset(entries)


def is_blocked(handle: str | None, entries: frozenset[str]) -> bool:
    n = normalize(handle)
    return bool(n) and n in entries
