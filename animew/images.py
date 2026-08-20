"""Card image cache (PRD F6): downloads MAL card images to disk."""

from __future__ import annotations

import requests

from . import config

_USER_AGENT = "animew-widget/0.1 (personal tool)"


def _ext(url: str) -> str:
    base = url.split("?")[0].rstrip("/")
    if base.lower().endswith(".webp"):
        return "webp"
    return "jpg"


def ensure_image(url: str | None, mal_id: int) -> str | None:
    """Download the card image for mal_id if not cached.

    Returns the local path, or None if the URL is missing or the download
    fails (the UI falls back to a placeholder).
    """
    if not url:
        return None
    config.IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.IMAGE_CACHE_DIR / f"{mal_id}.{_ext(url)}"
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return str(path)
    except requests.RequestException:
        return None
