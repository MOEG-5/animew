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
