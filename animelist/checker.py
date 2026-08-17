"""M4: daily new-content discovery (PRD F11).

Once a day, sweep ``related_anime`` for every locally-watched show and
surface untracked sequels/movies. No time window (Q7): any untracked
sequel/movie of a watched show qualifies — announced, airing, or older.

Relation handling:
- ``sequel``: any TV/OVA/ONA/special/movie qualifies.
- ``other``: only movies (parodies and spin-offs would be noise).

Badge placement: the badge appears on **all known franchise cards** (e.g. both
Youjo Senki seasons when a movie is detected), computed by walking the
relation graph (sequel/prequel/side_story/parent_story/other) from the source
and keeping only entries already in the local store. The badge stays until
the target is actually watched.
"""

from __future__ import annotations

import datetime
import queue

from .store import Store

RELATIONS = ("sequel", "other")
MEDIA_TYPES = ("tv", "movie", "ova", "ona", "special")
CHECK_INTERVAL = 24 * 3600  # seconds
DETAIL_CAP = 40  # max detail fetches per check (rate-limit headroom)

_SETTING = "new_content_last_check"


class NewContentChecker:
    def __init__(self, store: Store, mal, out_queue: queue.Queue):
        self.store = store
        self.mal = mal
        self.out = out_queue

    def _last_check(self) -> datetime.datetime | None:
        raw = self.store.get_setting(_SETTING)
        if not raw:
            return None
        try:
            return datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def due(self, now: datetime.datetime | None = None) -> bool:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        last = self._last_check()
        return last is None or (now - last).total_seconds() >= CHECK_INTERVAL

    def run_check(self) -> list[dict]:
        """Sweep related_anime; record and return newly-detected items:
        [{source_mal_id, target_mal_id, target_title, target_url}]."""
        now = datetime.datetime.now(datetime.timezone.utc)
        self.store.set_setting(_SETTING, now.isoformat())
        detected: list[dict] = []
        sources = [r["mal_id"] for r in self.store.local_max_episodes()]
        fetched = 0
        for source in sources:
            try:
                d = self.mal.anime(source, fields="id,related_anime")
            except Exception:
                continue
            for rel in d.get("related_anime") or []:
                rel_type = rel.get("relation_type")
                if rel_type not in RELATIONS:
                    continue
                node = rel.get("node") or {}
                target = node.get("id")
                if target is None or target == source:
                    continue
                if self.store.get_anime(target) is not None:
                    continue  # already known/tracked
                if fetched >= DETAIL_CAP:
                    break
                fetched += 1
                try:
                    dd = self.mal.anime(target, fields="id,title,media_type,main_picture")
                except Exception:
                    continue
                media = dd.get("media_type")
                if media not in MEDIA_TYPES:
                    continue
                if rel_type == "other" and media != "movie":
                    continue  # parodies/spin-offs are noise; movies only
                if self.store.get_anime(target) is not None:
                    continue  # re-check after the detail fetch
                added = self.store.add_new_content(
                    source, target, rel_type, dd.get("title"),
                    self._badge_ids_for(source),
                )
                if added:
                    detected.append({
                        "source_mal_id": source,
                        "target_mal_id": target,
                        "target_title": dd.get("title") or f"MAL #{target}",
                        "target_url": f"https://myanimelist.net/anime/{target}",
                    })
        return detected

    def _badge_ids_for(self, source: int) -> list[int]:
        """Known franchise cards that should carry the badge for a detection:
        walk the relation graph from the source and keep entries already in
        the local store (the source itself is always included)."""
        known = {r["mal_id"] for r in self.store.local_max_episodes()}
        ids = [i for i in self._franchise_ids(source) if i in known]
        if source not in ids:
            ids.append(source)
        return ids

    def _franchise_ids(self, root: int, depth: int = 2, cap: int = 25) -> set[int]:
        """Bounded BFS over the franchise relation graph."""
        found: set[int] = {root}
        frontier = [root]
        for _ in range(depth):
            nxt: list[int] = []
            for mid in frontier:
                if len(found) >= cap:
                    return found
                try:
                    d = self.mal.anime(mid, fields="id,related_anime")
                except Exception:
                    continue
                for rel in d.get("related_anime") or []:
                    if rel.get("relation_type") not in (
                        "sequel", "prequel", "side_story", "parent_story", "other"
                    ):
                        continue
                    nid = (rel.get("node") or {}).get("id")
                    if nid and nid not in found:
                        found.add(nid)
                        nxt.append(nid)
            frontier = nxt
            if not frontier:
                break
        return found
