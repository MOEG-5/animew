"""M3: MyAnimeList list sync (PRD F10).

Rules:
- add to *Watching* with episode progress when not on the list,
- only ever raise progress (never lower it),
- flip to *Completed* once the final episode is known,
- never send score or any field beyond status/progress,
- failed calls are queued in `pending_sync` and retried.
"""

from __future__ import annotations

import threading
import time

import requests

from . import config
from .auth import TokenStore, refresh_access_token
from .images import ensure_image

LIST_FIELDS = "list_status"
_PAGE = 100


class SyncError(Exception):
    """Permanent or retryable API error."""


class MALSync:
    def __init__(self, client_id: str, client_secret: str = "",
                 token_store: TokenStore | None = None, store=None,
                 session: requests.Session | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = token_store or TokenStore()
        self.store = store
        self.session = session or requests.Session()
        self._lock = threading.RLock()

    # -- tokens -----------------------------------------------------------------

    def _access_token(self) -> str:
        tok = self.tokens.load()
        if not tok or not tok.get("access_token"):
            raise SyncError("no access token — authorize first")
        expires_at = tok.get("expires_at", 0)
        if expires_at and time.time() > expires_at - 60 and tok.get("refresh_token"):
            try:
                new = refresh_access_token(self.client_id, self.client_secret, tok["refresh_token"])
                self.tokens.save(new)
                return new["access_token"]
            except requests.RequestException:
                pass  # fall through to the (possibly expired) token
        return tok["access_token"]

    def _request(self, method: str, path: str, params: dict | None = None,
                 data: dict | None = None, _retried: bool = False) -> dict:
        url = config.MAL_API_BASE + path
        kwargs: dict = {}
        if params:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        resp = self.session.request(method, url, headers={"Authorization": f"Bearer {self._access_token()}"},
                                    timeout=30, **kwargs)
        if resp.status_code == 401 and not _retried:
            tok = self.tokens.load() or {}
            if tok.get("refresh_token"):
                try:
                    new = refresh_access_token(self.client_id, self.client_secret, tok["refresh_token"])
                    self.tokens.save(new)
                    return self._request(method, path, params, data, _retried=True)
                except requests.RequestException:
                    pass
            raise SyncError(f"401: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise SyncError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # -- list import --------------------------------------------------------------

    def import_list(self) -> int:
        """Mirror the user's MAL list into `mal_list` (+ `anime` for images)."""
        offset, count = 0, 0
        while True:
            data = self._request(
                "GET", "/users/@me/animelist",
                params={"fields": LIST_FIELDS, "limit": _PAGE, "offset": offset},
            )
            for item in data.get("data", []):
                node = item.get("node", {})
                status = item.get("list_status", {})
                mal_id = node.get("id")
                if mal_id is None:
                    continue
                pic = node.get("main_picture") or {}
                image_url = pic.get("large") or pic.get("medium")
                image_path = ensure_image(image_url, mal_id) if image_url else None
                if self.store is not None:
                    self.store.upsert_anime(
                        mal_id, node.get("title", ""), node.get("media_type"),
                        None, None, image_path, f"https://myanimelist.net/anime/{mal_id}",
                    )
                    # NB: the API response key is "num_episodes_watched" (the
                    # PATCH *request* parameter is "num_watched_episodes" —
                    # different names, easy to mix up).
                    progress = status.get("num_episodes_watched") or 0
                    self.store.set_mal_status(
                        mal_id, status.get("status"),
                        progress,
                        status.get("score"), status.get("updated_at"),
                    )
                    if progress > 0:
                        # Mirror progress into local watch history so the
                        # widget grid reflects the imported list.
                        self.store.mark_episodes_watched(
                            mal_id, min(progress, 2000), status.get("updated_at"))
                count += 1
            if (data.get("paging") or {}).get("next"):
                offset += _PAGE
            else:
                break
        return count

    # -- episode push --------------------------------------------------------------

    def push_episode(self, mal_id: int, episode: int | None, num_episodes: int | None) -> None:
        """Mirror a watched episode to MAL. On failure, queue for retry."""
        with self._lock:
            try:
                self._push_episode_now(mal_id, episode, num_episodes)
            except (SyncError, requests.RequestException):
                if self.store is not None:
                    self.store.add_pending(mal_id, episode, num_episodes)

    def _push_episode_now(self, mal_id: int, episode: int | None, num_episodes: int | None) -> None:
        current = self.store.get_mal_status(mal_id) if self.store is not None else None
        current = current or {}
        cur_progress = current.get("num_watched_episodes") or 0
        cur_status = current.get("status")

        ep = episode if episode is not None else 1  # movies count as 1 episode
        if ep <= cur_progress and cur_status in ("watching", "completed", "on_hold", "dropped"):
            return  # nothing to change; never lower progress

        new_progress = max(cur_progress, ep)
        new_status = cur_status
        if cur_status in (None, "plan_to_watch"):
            new_status = "watching"
        if num_episodes and new_progress >= num_episodes:
            new_status = "completed"

        data: dict = {"num_watched_episodes": new_progress}
        if new_status and new_status != cur_status:
            data["status"] = new_status
        self._request("PATCH", f"/anime/{mal_id}/my_list_status", data=data)
        if self.store is not None:
            self.store.set_mal_status(mal_id, new_status, new_progress, current.get("score"), None)
        # Franchise integrity: completing/continuing any entry should keep
        # earlier seasons of the same franchise completed on MAL and locally.
        try:
            self._complete_prequels(mal_id)
        except (SyncError, requests.RequestException):
            pass

    def _complete_prequels(self, mal_id: int, depth: int = 8, visited: set[int] | None = None) -> None:
        """Walk the prequel chain and mark earlier seasons completed on MAL
        and locally.

        Notes:
        - related_anime nodes carry no num_episodes/media_type, so real
          details are fetched per franchise node.
        - Movies are never marked watched (they are not seasons), but the
          chain is walked *through* them (MAL often routes S2 -> Movie -> S1).
        - dropped/on-hold entries are respected; unknown shows not on the
          list are never guessed.
        """
        if depth <= 0:
            return
        visited = visited if visited is not None else set()
        if mal_id in visited:
            return
        visited.add(mal_id)
        try:
            d = self._request(
                "GET", f"/anime/{mal_id}",
                params={"fields": "id,title,media_type,num_episodes,related_anime"},
            )
        except (SyncError, requests.RequestException):
            return
        for rel in d.get("related_anime") or []:
            if rel.get("relation_type") != "prequel":
                continue
            node = rel.get("node") or {}
            rid = node.get("id")
            if rid is None or rid in visited:
                continue
            try:
                dd = self._request(
                    "GET", f"/anime/{rid}",
                    params={"fields": "id,media_type,num_episodes"},
                )
            except (SyncError, requests.RequestException):
                continue
            media = dd.get("media_type")
            num = dd.get("num_episodes")
            cur = self.store.get_mal_status(rid) if self.store is not None else None
            cur = cur or {}
            if cur.get("status") in ("dropped", "on_hold", "completed"):
                continue  # respect the user's choice / already done
            if media == "movie":
                self._complete_prequels(rid, depth - 1, visited)  # walk through
                continue
            if not cur and not num:
                self._complete_prequels(rid, depth - 1, visited)  # keep walking
                continue
            progress = num or cur.get("num_watched_episodes") or 1
            try:
                self._request(
                    "PATCH", f"/anime/{rid}/my_list_status",
                    data={"status": "completed", "num_watched_episodes": progress},
                )
            except (SyncError, requests.RequestException):
                continue
            if self.store is not None:
                self.store.set_mal_status(rid, "completed", progress, cur.get("score"), None)
                self.store.mark_episodes_watched(rid, min(progress, 2000))
            self._complete_prequels(rid, depth - 1, visited)

    # -- pending queue ----------------------------------------------------------------

    def reconcile(self) -> int:
        """Push any locally-confirmed episodes not yet reflected on MAL.

        Heals gaps from offline periods, crashes, or pre-auth backlogs, and
        keeps franchise integrity (previous seasons completed) even for
        shows that are already in sync. Returns the number of anime pushed.
        """
        if self.store is None:
            return 0
        n = 0
        for row in self.store.local_max_episodes():
            mal_id = row["mal_id"]
            ep = row["episode"]  # None for movies
            cur = self.store.get_mal_status(mal_id)
            needs_push = True
            if cur:
                if ep is not None:
                    if ep <= (cur.get("num_watched_episodes") or 0) and cur.get("status") in (
                        "watching", "completed", "on_hold", "dropped"
                    ):
                        needs_push = False
                elif cur.get("status") in ("watching", "completed", "on_hold", "dropped"):
                    needs_push = False
            if needs_push:
                anime = self.store.get_anime(mal_id)
                num = anime.get("num_episodes") if anime else None
                self.push_episode(mal_id, ep, num)  # prequel completion inside
                n += 1
            else:
                # Already in sync — still ensure the franchise is complete.
                try:
                    self._complete_prequels(mal_id)
                except (SyncError, requests.RequestException):
                    pass
        return n

    def retry_pending(self) -> int:
        """Attempt all queued syncs; returns how many are still pending."""
        if self.store is None:
            return 0
        for row in self.store.list_pending():
            try:
                self._push_episode_now(row["mal_id"], row["episode"], row["num_episodes"])
                self.store.remove_pending(row["id"])
            except (SyncError, requests.RequestException):
                continue
        return len(self.store.list_pending())
