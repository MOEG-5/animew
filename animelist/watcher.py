"""F3: watch mpv events, resolve release titles, and confirm episodes as
watched once a threshold of accumulated (non-paused) playback time is reached.

Runs in a background thread. Emits UI messages on ``out_queue`` and accepts
re-pick commands on ``cmd_queue``.
"""

from __future__ import annotations

import queue
import re
import time
from dataclasses import dataclass

from .images import ensure_image
from .ipc import watch_socket
from .mal import MALClient, is_real_synopsis
from .parser import parse_title
from .store import Store

DEFAULT_THRESHOLD = 600.0  # seconds of actual playback (PRD F3: 10 minutes)

# Season markers commonly found in release filenames, e.g. "Show S2",
# "Show Season 2", "Show 2nd Season".
SEASON_RE = re.compile(r"(?i)\b(?:s\d{1,2}|season\s*\d{1,2}|\d{1,2}(?:st|nd|rd|th)\s*season)\b")


@dataclass
class Current:
    filename: str
    mal_id: int | None = None
    episode: int | None = None
    title: str = ""
    media_type: str | None = None
    mal_url: str | None = None
    image_path: str | None = None
    synopsis: str | None = None
    accrued: float = 0.0
    last_update: float = 0.0
    paused: bool = True
    confirmed: bool = False


class WatchWorker:
    """Consumes mpv events and drives the watched-confirmation state machine.

    ``handle_event`` can be driven directly (tests) or fed by ``run()``
    from the mpv socket.
    """

    def __init__(
        self,
        sockets,
        out_queue: queue.Queue,
        cmd_queue: queue.Queue | None = None,
        store: Store | None = None,
        mal: MALClient | None = None,
        sync=None,
        tags: list[str] | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        clock=time.monotonic,
    ):
        self.sockets = sockets
        self.out = out_queue
        self.cmd = cmd_queue or queue.Queue()
        self.store = store or Store()
        self.mal = mal or MALClient()
        self.sync = sync
        self.tags = list(tags) if tags else []
        self.threshold = threshold
        self.clock = clock
        self.current: Current | None = None

    # -- events ---------------------------------------------------------------

    def handle_event(self, ev: dict) -> None:
        kind = ev["type"]
        if kind == "file":
            self._on_file(ev["filename"])
        elif kind == "property":
            self._on_property(ev["name"], ev["data"])
        elif kind == "idle":
            self.current = None
        elif kind == "connected":
            self.out.put({"type": "connected", "socket": ev.get("socket")})
        elif kind == "disconnected":
            self.out.put({"type": "disconnected"})

    def _on_property(self, name: str, data) -> None:
        cur = self.current
        if cur is None:
            return
        now = self.clock()
        if name == "pause":
            if data is True and not cur.paused:
                cur.accrued += now - cur.last_update
                cur.paused = True
            elif data is False:
                cur.paused = False
                cur.last_update = now
            self._check_threshold()
        elif name == "time-pos" and not cur.paused:
            cur.accrued += now - cur.last_update
            cur.last_update = now
            self._check_threshold()

    def _on_file(self, filename: str) -> None:
        cur = self.current
        if cur is not None and cur.filename == filename:
            return  # duplicate event for the same file
        parsed = parse_title(filename, self.tags)
        if parsed is None:
            self.current = None
            self.out.put({"type": "ignored", "filename": filename})
            return
        try:
            top = self._resolve(parsed.anime_name)
        except Exception as exc:
            self.current = None
            self.out.put({"type": "resolve_failed", "filename": filename, "reason": str(exc)})
            return
        if top is None:
            self.current = None
            self.out.put({"type": "resolve_failed", "filename": filename, "reason": "no MAL results"})
            return
        cur = Current(
            filename=filename,
            mal_id=top["id"],
            episode=parsed.episode,
            title=top["title"],
            media_type=top["media_type"],
            mal_url=f"https://myanimelist.net/anime/{top['id']}",
        )
        self.current = cur
        synopsis, num_eps = self._fetch_details_if_needed(cur.mal_id)
        cur.synopsis = synopsis
        cur.image_path = ensure_image(top["image_url"], cur.mal_id)
        self.store.upsert_anime(
            cur.mal_id, cur.title, cur.media_type, num_eps,
            synopsis, cur.image_path, cur.mal_url,
        )
        now = self.clock()
        cur.last_update = now
        cur.paused = False
        self.out.put({
            "type": "resolved",
            "filename": filename,
            "kind": parsed.kind,
            "anime_name": parsed.anime_name,
            "mal_id": cur.mal_id,
            "episode": cur.episode,
            "title": cur.title,
            "media_type": cur.media_type,
            "image_path": cur.image_path,
            "mal_url": cur.mal_url,
            "synopsis": synopsis,
        })

    def _resolve(self, anime_name: str) -> dict | None:
        """Search MAL with season-marker awareness.

        - No marker: top-1 result wins.
        - Marker ("S2", "Season 2", ...): prefer a candidate whose own title
          carries the marker; otherwise search the base name and follow the
          top result's sequel relation ("Show S2" -> S1's sequel entry).
        """
        results = self.mal.search(anime_name, limit=5)
        if not results:
            return None
        m = SEASON_RE.search(anime_name)
        if not m:
            return results[0]
        for r in results:
            if SEASON_RE.search(r.get("title", "")):
                return r
        base = SEASON_RE.sub(" ", anime_name).strip()
        if base and base != anime_name:
            try:
                base_results = self.mal.search(base, limit=1)
                if base_results:
                    d = self.mal.anime(
                        base_results[0]["id"],
                        fields="id,main_picture,media_type,related_anime",
                    )
                    for rel in d.get("related_anime") or []:
                        if rel.get("relation_type") not in ("sequel", "other"):
                            continue
                        node = rel.get("node") or {}
                        if not node.get("id"):
                            continue
                        pic = node.get("main_picture") or {}
                        return {
                            "id": node["id"],
                            "title": node.get("title", ""),
                            "image_url": pic.get("large") or pic.get("medium"),
                            "media_type": node.get("media_type"),
                        }
            except Exception:
                pass
        return results[0]

    def _fetch_details_if_needed(self, mal_id: int) -> tuple[str | None, int | None]:
        row = self.store.get_anime(mal_id)
        if (row and is_real_synopsis(row.get("synopsis"))
                and row.get("num_episodes") is not None):
            return row["synopsis"], row["num_episodes"]
        try:
            d = self.mal.details_with_synopsis(mal_id)
            return (d.get("synopsis") or ""), d.get("num_episodes")
        except Exception:
            return None, None

    def _check_threshold(self) -> None:
        cur = self.current
        if cur is None or cur.confirmed or cur.mal_id is None:
            return
        if cur.accrued >= self.threshold:
            first = self.store.mark_watched(cur.mal_id, cur.episode)
            cur.confirmed = True
            self.out.put({
                "type": "confirmed",
                "mal_id": cur.mal_id,
                "episode": cur.episode,
                "title": cur.title,
                "first": first,
                "accrued": round(cur.accrued, 1),
            })
            if first and self.sync is not None:
                try:
                    self.sync.push_episode(
                        cur.mal_id, cur.episode, self._num_episodes(cur.mal_id))
                except Exception:
                    pass

    def _num_episodes(self, mal_id: int) -> int | None:
        row = self.store.get_anime(mal_id)
        return row.get("num_episodes") if row else None

    # -- commands (UI -> worker) ----------------------------------------------

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self.cmd.get_nowait()
            except queue.Empty:
                return
            kind = cmd.get("cmd")
            if kind == "repick":
                self._repick(cmd)
            elif kind == "set_tags":
                self.tags = list(cmd.get("tags") or [])
            elif kind == "set_threshold":
                self.threshold = float(cmd.get("threshold", self.threshold))

    def _repick(self, cmd: dict) -> None:
        old = cmd.get("old_mal_id")
        new = cmd.get("new_mal_id")
        if old == new:
            return
        try:
            d = self.mal.details_with_synopsis(new)
        except Exception as exc:
            self.out.put({"type": "resolve_failed", "reason": f"repick: {exc}"})
            return
        title = d.get("title") or cmd.get("title", "")
        pic = d.get("main_picture") or {}
        image_url = pic.get("large") or pic.get("medium")
        synopsis = d.get("synopsis") or ""
        num = d.get("num_episodes")
        image_path = ensure_image(image_url, new)
        self.store.upsert_anime(
            new, title, d.get("media_type"), num,
            synopsis, image_path, f"https://myanimelist.net/anime/{new}",
        )
        if old is not None and old != new:
            self.store.remove_anime(old)  # drop the mis-resolved rows

        cur = self.current
        if cur is not None and cur.mal_id == old:
            was_confirmed = cur.confirmed
            cur.mal_id = new
            cur.title = title
            cur.media_type = d.get("media_type")
            cur.mal_url = f"https://myanimelist.net/anime/{new}"
            cur.image_path = image_path
            cur.synopsis = synopsis
            cur.confirmed = self.store.is_watched(new, cur.episode)
            if was_confirmed and not cur.confirmed:
                self.store.mark_watched(new, cur.episode)
                cur.confirmed = True
            if cur.confirmed and self.sync is not None:
                # The episode was already confirmed (possibly under the wrong
                # id) — make sure MAL knows about the corrected entry.
                try:
                    self.sync.push_episode(new, cur.episode, self._num_episodes(new))
                except Exception:
                    pass
        self.out.put({
            "type": "repicked",
            "mal_id": cur.mal_id if cur is not None else new,
            "title": title,
            "media_type": d.get("media_type"),
            "image_path": image_path,
            "mal_url": f"https://myanimelist.net/anime/{new}",
            "synopsis": synopsis,
            "episode": cur.episode if cur is not None else None,
            "confirmed": bool(cur and cur.confirmed),
        })

    # -- main loop ---------------------------------------------------------------

    def run(self) -> None:
        for ev in watch_socket(self.sockets, reconnect=True):
            self._drain_commands()
            self.handle_event(ev)
