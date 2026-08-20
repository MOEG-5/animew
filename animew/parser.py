"""Release-title parsing (PRD F2).

Detects whether a media filename belongs to a configured release group and
extracts the anime name and episode number. The canonical title looks like::

    [Tag] Lv999 no Murabito - 05 (1080p) [ABC123].mkv

A file is tracked only if its name contains one of the configured release
tags (case-insensitive substring). **An empty tag list matches nothing** —
no file is tracked until the user configures tags (this prevents non-anime
files from ever reaching MAL). Everything we need sits between "]" and "(" —
the resolution/hash after the opening paren is ignored. The name regex is
right-anchored (greedy expansion with backtracking), so series names
containing " - " parse correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Order matters: batch first, then episode, then movie.
_BATCH = re.compile(
    r"^\[[^\]]*\]\s*(.+?)\s*-\s*(\d{1,4})-(\d{1,4})\s*\(",
    re.IGNORECASE,
)
_EPISODE = re.compile(
    r"^\[[^\]]*\]\s*(.+?)\s*-\s*(\d{1,4})(?:v\d+)?\s*\(",
    re.IGNORECASE,
)
_MOVIE = re.compile(
    r"^\[[^\]]*\]\s*(.+?)\s*\(",
    re.IGNORECASE,
)


def _matches_tags(name: str, tags) -> bool:
    if not tags:
        return False  # no tags configured -> track nothing
    lower = name.lower()
    return any(t.strip().lower() in lower for t in tags if t.strip())


@dataclass(frozen=True)
class ParsedTitle:
    kind: str  # "episode" | "movie" | "batch"
    anime_name: str
    episode: int | None  # episode number (kind == "episode"); None otherwise
    episode_end: int | None = None  # upper bound for kind == "batch"


def parse_title(filename: str, tags=None) -> ParsedTitle | None:
    """Parse a media filename into a ParsedTitle.

    Returns None for anything that does not match a configured release tag
    (or whose name format is unrecognized).
    """
    raw = filename.strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    name = Path(raw).name
    if not _matches_tags(name, tags):
        return None

    m = _BATCH.search(name)
    if m:
        return ParsedTitle("batch", m.group(1).strip(), int(m.group(2)), int(m.group(3)))

    m = _EPISODE.search(name)
    if m:
        return ParsedTitle("episode", m.group(1).strip(), int(m.group(2)))

    m = _MOVIE.search(name)
    if m:
        return ParsedTitle("movie", m.group(1).strip(), None)

    return None
