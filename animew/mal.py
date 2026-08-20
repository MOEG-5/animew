"""MyAnimeList API v2 client (PRD F4/F5).

Public endpoints (search, anime details) need only the X-MAL-CLIENT-ID header,
so the detection pipeline can be built and tested before OAuth is wired in.
OAuth (M3) adds the list read/write endpoints.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from . import config

SEARCH_FIELDS = "id,title,main_picture,media_type"
DETAIL_FIELDS = (
    "id,title,main_picture,synopsis,media_type,num_episodes,status,start_date,related_anime"
)

# MAL's second/third-season entries often ship a stub synopsis like
# "Second season of X." (~30-70 chars) instead of a real description. A
# real synopsis is substantially longer, so anything under this length is
# treated as a stub and replaced with the franchise's first-season synopsis.
MIN_REAL_SYNOPSIS = 200


def is_real_synopsis(synopsis: str | None) -> bool:
    """True when the synopsis is a substantial description, not a MAL stub."""
    return bool(synopsis) and len(synopsis.strip()) >= MIN_REAL_SYNOPSIS


def franchise_synopsis(fetch, mal_id: int, min_len: int = MIN_REAL_SYNOPSIS,
                       depth: int = 6) -> str:
    """Best-effort walk of the prequel chain (through movies, which MAL often
    routes seasons through) returning the first synopsis of at least
    ``min_len`` characters — i.e. the first season's real description when
    the entry itself only has a "Second season of X." stub.

    ``fetch(mal_id)`` must return a details dict containing ``synopsis`` and
    ``related_anime``. Returns "" when no real synopsis is found.
    """
    seen: set[int] = set()
    frontier: list[tuple[int, int]] = [(mal_id, depth)]
    while frontier:
        mid, d = frontier.pop()
        if mid in seen or d <= 0:
            continue
        seen.add(mid)
        try:
            rel = fetch(mid)
        except Exception:
            continue  # best-effort: a failed fetch just skips this node
        syn = (rel.get("synopsis") or "").strip()
        if len(syn) >= min_len:
            return syn
        for r in rel.get("related_anime") or []:
            if r.get("relation_type") != "prequel":
                continue
            nid = (r.get("node") or {}).get("id")
            if nid is not None and nid not in seen:
                frontier.append((nid, d - 1))
    return ""


class MALError(Exception):
    """Base MAL API error."""


class MALAuthRequired(MALError):
    """Endpoint needs an OAuth token we do not have yet."""


class MALRateLimited(MALError):
    """Persistent 429 after backoff."""


class MALClient:
    def __init__(self, client_id: str | None = None, timeout: float = 15.0):
        cfg = config.load_config()
        self.client_id = client_id or cfg.get("mal_client_id", "") or ""
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-MAL-CLIENT-ID": self.client_id})
        self._min_interval = 1.0 / 30.0  # stay far below the 60 req/min limit
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = config.MAL_API_BASE + path
        for attempt in range(5):
            self._throttle()
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 429:
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 401:
                raise MALAuthRequired(resp.text[:200])
            if resp.status_code >= 400:
                raise MALError(f"{resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise MALRateLimited("still rate-limited after backoff")

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search anime; returns a normalized list of candidate dicts."""
        data = self._get("/anime", {"q": query, "limit": limit, "fields": SEARCH_FIELDS})
        out: list[dict] = []
        for item in data.get("data", []):
            node = item.get("node", {})
            pic = node.get("main_picture") or {}
            out.append(
                {
                    "id": node.get("id"),
                    "title": node.get("title", ""),
                    "image_url": pic.get("large") or pic.get("medium"),
                    "media_type": node.get("media_type"),
                }
            )
        return out

    def anime(self, mal_id: int, fields: str = DETAIL_FIELDS) -> dict:
        """Fetch anime details (card image, synopsis, episodes, related)."""
        return self._get(f"/anime/{mal_id}", {"fields": fields})

    def find_franchise_synopsis(self, mal_id: int) -> str:
        """First real synopsis found walking the prequel chain (see
        :func:`franchise_synopsis`)."""
        return franchise_synopsis(
            lambda mid: self.anime(mid, fields="id,synopsis,related_anime"),
            mal_id,
        )

    def details_with_synopsis(self, mal_id: int, fields: str = DETAIL_FIELDS) -> dict:
        """Anime details, but with MAL's "Second season of X." stub synopsis
        replaced by the franchise's first-season synopsis when available."""
        d = self.anime(mal_id, fields=fields)
        if not is_real_synopsis(d.get("synopsis")):
            real = self.find_franchise_synopsis(mal_id)
            if real:
                d["synopsis"] = real
        return d
