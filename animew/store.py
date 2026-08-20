"""SQLite persistence layer (PRD F6).

State lives in ~/.local/share/animew-widget/animew.db.
Tables: anime, watched, new_content, settings.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    mal_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    media_type TEXT,
    num_episodes INTEGER,
    synopsis TEXT,
    image_path TEXT,
    mal_url TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS watched (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mal_id INTEGER NOT NULL REFERENCES anime(mal_id),
    episode INTEGER,               -- NULL for movies/specials (PRD Q5)
    first_watched_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS new_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_mal_id INTEGER NOT NULL,
    target_mal_id INTEGER NOT NULL,
    relation TEXT,
    target_title TEXT,
    badge_ids TEXT,
    detected_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_mal_id, target_mal_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS mal_list (
    mal_id INTEGER PRIMARY KEY,
    status TEXT,
    num_watched_episodes INTEGER,
    score INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_sync (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mal_id INTEGER NOT NULL,
    episode INTEGER,
    num_episodes INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watched_mal ON watched(mal_id);
CREATE INDEX IF NOT EXISTS idx_watched_updated ON watched(updated_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around the SQLite database."""

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = Path(db_path) if db_path else config.DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        """Schema migrations for databases created by earlier versions."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(new_content)")}
        if "target_title" not in cols:
            self._db.execute("ALTER TABLE new_content ADD COLUMN target_title TEXT")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(new_content)")}
        if "badge_ids" not in cols:
            self._db.execute("ALTER TABLE new_content ADD COLUMN badge_ids TEXT")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(anime)")}
        if "details_attempted_at" not in cols:
            self._db.execute("ALTER TABLE anime ADD COLUMN details_attempted_at TEXT")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- anime -------------------------------------------------------------

    def upsert_anime(
        self,
        mal_id: int,
        title: str,
        media_type: str | None = None,
        num_episodes: int | None = None,
        synopsis: str | None = None,
        image_path: str | None = None,
        mal_url: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO anime
                       (mal_id, title, media_type, num_episodes, synopsis, image_path, mal_url, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mal_id) DO UPDATE SET
                       title = excluded.title,
                       media_type = COALESCE(excluded.media_type, anime.media_type),
                       num_episodes = COALESCE(excluded.num_episodes, anime.num_episodes),
                       synopsis = COALESCE(excluded.synopsis, anime.synopsis),
                       image_path = COALESCE(excluded.image_path, anime.image_path),
                       mal_url = COALESCE(excluded.mal_url, anime.mal_url),
                       updated_at = excluded.updated_at""",
                (mal_id, title, media_type, num_episodes, synopsis, image_path, mal_url, _utcnow()),
            )
            self._db.commit()

    def get_anime(self, mal_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM anime WHERE mal_id = ?", (mal_id,)
            ).fetchone()
            return dict(row) if row else None

    def remove_anime(self, mal_id: int) -> None:
        """Delete an anime and its watched rows (used when a mis-resolution
        is corrected via re-pick)."""
        with self._lock:
            self._db.execute("DELETE FROM watched WHERE mal_id = ?", (mal_id,))
            self._db.execute("DELETE FROM anime WHERE mal_id = ?", (mal_id,))
            self._db.commit()

    # -- watched -------------------------------------------------------------

    def is_watched(self, mal_id: int, episode: int | None = None) -> bool:
        """episode=None queries movie/special entries (watched with no episode)."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM watched WHERE mal_id = ? AND episode IS ? LIMIT 1",
                (mal_id, episode),
            ).fetchone()
            return row is not None

    def mark_watched(self, mal_id: int, episode: int | None = None) -> bool:
        """Record an episode (or a movie, episode=None) as watched.

        Idempotent: returns True only on the first marking.
        """
        now = _utcnow()
        with self._lock:
            if self.is_watched(mal_id, episode):
                return False
            self._db.execute(
                "INSERT INTO watched (mal_id, episode, first_watched_at, updated_at) VALUES (?, ?, ?, ?)",
                (mal_id, episode, now, now),
            )
            self._db.commit()
            return True

    def latest_watched(self, limit: int = 20) -> list[dict]:
        """Most recently watched anime, newest first — one row per anime with
        its latest watched episode. (Legacy helper; the widget uses
        ``collection()``.)"""
        with self._lock:
            rows = self._db.execute(
                """SELECT a.mal_id, a.title, a.media_type, a.image_path, a.mal_url, a.synopsis,
                          MAX(w.episode) AS episode, MAX(w.updated_at) AS updated_at
                   FROM watched w JOIN anime a ON a.mal_id = w.mal_id
                   GROUP BY a.mal_id
                   ORDER BY MAX(w.updated_at) DESC, MAX(w.id) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def collection(self, limit: int = 60) -> list[dict]:
        """The full card set: every anime that is either locally watched or on
        the MAL list (excluding plan-to-watch), one card per anime with its
        latest episode (local watch or MAL progress). Newest activity first."""
        with self._lock:
            rows = self._db.execute(
                """SELECT a.mal_id, a.title, a.media_type, a.image_path, a.mal_url, a.synopsis,
                          CASE WHEN MAX(w.episode) IS NOT NULL
                               THEN MAX(MAX(w.episode), COALESCE(m.num_watched_episodes, 0))
                               ELSE m.num_watched_episodes END AS episode,
                          COALESCE(MAX(w.updated_at), m.updated_at) AS updated_at,
                          m.status AS mal_status
                   FROM anime a
                   LEFT JOIN watched w ON w.mal_id = a.mal_id
                   LEFT JOIN mal_list m ON m.mal_id = a.mal_id
                   WHERE w.mal_id IS NOT NULL
                      OR (m.mal_id IS NOT NULL AND m.status != 'plan_to_watch')
                   GROUP BY a.mal_id
                   ORDER BY COALESCE(MAX(w.updated_at), m.updated_at) DESC, MAX(w.id) DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_mal(self, limit: int = 50) -> list[dict]:
        """MAL list mirror rows (for import logging/debugging)."""
        with self._lock:
            rows = self._db.execute(
                """SELECT m.mal_id, a.title, m.status, m.num_watched_episodes
                   FROM mal_list m LEFT JOIN anime a ON a.mal_id = m.mal_id
                   ORDER BY m.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_anime_image(self, mal_id: int, image_path: str) -> None:
        with self._lock:
            self._db.execute("UPDATE anime SET image_path = ? WHERE mal_id = ?", (image_path, mal_id))
            self._db.commit()

    def remote_image_rows(self) -> list[dict]:
        """Anime rows whose image_path is still a remote URL (healed by the
        startup image fixup)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT mal_id, image_path FROM anime WHERE image_path LIKE 'http%'"
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_episodes_watched(self, mal_id: int, upto: int, at: str | None = None) -> None:
        """Insert watched rows for episodes 1..upto (skipping existing).
        Used by the MAL list import and franchise catch-up."""
        now = at or _utcnow()
        with self._lock:
            have = {r[0] for r in self._db.execute(
                "SELECT episode FROM watched WHERE mal_id = ?", (mal_id,)).fetchall()}
            missing = [(mal_id, ep, now, now) for ep in range(1, upto + 1) if ep not in have]
            if missing:
                self._db.executemany(
                    "INSERT INTO watched (mal_id, episode, first_watched_at, updated_at) VALUES (?, ?, ?, ?)",
                    missing,
                )
                self._db.commit()

    def local_max_episodes(self) -> list[dict]:
        """Latest locally-confirmed episode per anime (episode may be NULL
        for movies). Used by sync reconciliation."""
        with self._lock:
            rows = self._db.execute(
                "SELECT w.mal_id, MAX(w.episode) AS episode FROM watched w GROUP BY w.mal_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def rows_needing_details(self, min_synopsis_len: int = 200,
                             retry_after_days: float = 7.0) -> list[dict]:
        """Anime rows that would benefit from a details backfill:

        - known rows whose synopsis is missing or a MAL stub ("Second season
          of X."), or whose episode count is unknown — but only if we have
          not already attempted within ``retry_after_days``. A brand-new
          anime may legitimately have a short synopsis with no prequel to
          fall back on; re-fetching it on every startup would waste API
          calls, so after one attempt it waits out the cooldown.
        - orphan ids that are watched or on the MAL list but have no
          ``anime`` row yet (e.g. prequels completed by franchise sync) —
          they need a full detail fetch to become visible cards, and are
          always retried (there is no row to record an attempt on).

        Returns ``[{"mal_id": int, "title": str | None}]``.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_after_days))\
            .isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute(
                """SELECT mal_id, title FROM anime
                   WHERE (synopsis IS NULL OR length(trim(synopsis)) < ?
                          OR num_episodes IS NULL)
                     AND (details_attempted_at IS NULL OR details_attempted_at < ?)
                   UNION
                   SELECT DISTINCT w.mal_id AS mal_id, NULL AS title FROM watched w
                   LEFT JOIN anime a ON a.mal_id = w.mal_id WHERE a.mal_id IS NULL
                   UNION
                   SELECT DISTINCT m.mal_id AS mal_id, NULL AS title FROM mal_list m
                   LEFT JOIN anime a ON a.mal_id = m.mal_id WHERE a.mal_id IS NULL
                   ORDER BY mal_id""",
                (min_synopsis_len, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_details_attempted(self, mal_id: int, at: str | None = None) -> None:
        """Record that a details backfill was attempted for this anime, so the
        startup backfill throttles re-attempts (e.g. for a genuinely short
        synopsis with no prequel to fall back on)."""
        with self._lock:
            self._db.execute(
                "UPDATE anime SET details_attempted_at = ? WHERE mal_id = ?",
                (at or _utcnow(), mal_id),
            )
            self._db.commit()

    # -- settings -------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def has_data(self) -> bool:
        """True if any anime is known (used for upgrade defaults)."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM anime LIMIT 1").fetchone() is not None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    # -- MAL list mirror ----------------------------------------------------------

    def get_mal_status(self, mal_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM mal_list WHERE mal_id = ?", (mal_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_mal_status(self, mal_id: int, status: str | None, num_watched_episodes: int | None,
                       score: int | None, updated_at: str | None) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO mal_list (mal_id, status, num_watched_episodes, score, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(mal_id) DO UPDATE SET
                       status = COALESCE(excluded.status, mal_list.status),
                       num_watched_episodes = COALESCE(excluded.num_watched_episodes, mal_list.num_watched_episodes),
                       score = COALESCE(excluded.score, mal_list.score),
                       updated_at = COALESCE(excluded.updated_at, mal_list.updated_at)""",
                (mal_id, status, num_watched_episodes, score, updated_at),
            )
            self._db.commit()

    # -- pending sync queue ----------------------------------------------------------

    def add_pending(self, mal_id: int, episode: int | None, num_episodes: int | None) -> None:
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM pending_sync WHERE mal_id = ? AND episode IS ? LIMIT 1",
                (mal_id, episode),
            ).fetchone()
            if exists:
                return
            self._db.execute(
                "INSERT INTO pending_sync (mal_id, episode, num_episodes, created_at) VALUES (?, ?, ?, ?)",
                (mal_id, episode, num_episodes, _utcnow()),
            )
            self._db.commit()

    def list_pending(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_sync ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def remove_pending(self, pending_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending_sync WHERE id = ?", (pending_id,))
            self._db.commit()

    # -- new-content detection (M4) ----------------------------------------------

    def add_new_content(self, source_mal_id: int, target_mal_id: int, relation: str,
                        target_title: str | None = None,
                        badge_ids: list[int] | None = None) -> bool:
        """Record a detected item; returns True only on first detection.
        badge_ids = the set of (known) franchise cards that should show the
        badge, e.g. both Youjo Senki seasons when a movie is detected."""
        with self._lock:
            cur = self._db.execute(
                """INSERT OR IGNORE INTO new_content
                       (source_mal_id, target_mal_id, relation, target_title, badge_ids, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_mal_id, target_mal_id, relation, target_title,
                 json.dumps(badge_ids or []), _utcnow()),
            )
            self._db.commit()
            return cur.rowcount > 0

    def active_new_targets(self, source_mal_id: int) -> list[int]:
        """Undismissed, unwatched detected target ids for a source anime."""
        with self._lock:
            rows = self._db.execute(
                """SELECT target_mal_id FROM new_content
                   WHERE source_mal_id = ? AND dismissed = 0
                     AND target_mal_id NOT IN (SELECT mal_id FROM watched)
                   ORDER BY detected_at""",
                (source_mal_id,),
            ).fetchall()
            return [r["target_mal_id"] for r in rows]

    def badge_map(self) -> dict[int, list[int]]:
        """mal_id -> target ids of active (undismissed, unwatched) detections.

        Badges are shown on every card in the detection's franchise set
        (badge_ids) and clear once the target is actually watched.
        """
        with self._lock:
            watched = {r[0] for r in self._db.execute("SELECT mal_id FROM watched")}
            rows = self._db.execute(
                "SELECT * FROM new_content WHERE dismissed = 0 ORDER BY detected_at"
            ).fetchall()
            out: dict[int, list[int]] = {}
            for r in rows:
                if r["target_mal_id"] in watched:
                    continue  # new content is now being watched
                try:
                    ids = json.loads(r["badge_ids"] or "[]")
                except ValueError:
                    ids = []
                if not ids:
                    ids = [r["source_mal_id"]]  # legacy rows: badge the source only
                for mid in ids:
                    out.setdefault(mid, []).append(r["target_mal_id"])
            return out

    def unnotified_new_content(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM new_content WHERE notified = 0 AND dismissed = 0 ORDER BY detected_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def set_new_content_notified(self, source_mal_id: int, target_mal_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE new_content SET notified = 1 WHERE source_mal_id = ? AND target_mal_id = ?",
                (source_mal_id, target_mal_id),
            )
            self._db.commit()

    def dismiss_new_content(self, source_mal_id: int) -> None:
        """Dismiss all active new-content items for a source anime."""
        with self._lock:
            self._db.execute(
                "UPDATE new_content SET dismissed = 1 WHERE source_mal_id = ?", (source_mal_id,)
            )
            self._db.commit()


# -- typed settings helpers (F14) --------------------------------------------------

TAGS_KEY = "release_tags"
THRESHOLD_KEY = "watch_threshold_minutes"
COLUMNS_KEY = "grid_columns"
ROWS_KEY = "grid_rows"
IMAGE_WIDTH_KEY = "card_image_width"


def get_release_tags(store: Store) -> list[str]:
    """Configured release-group tags; empty list = track nothing (F2)."""
    raw = store.get_setting(TAGS_KEY, "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def set_release_tags(store: Store, tags: list[str]) -> None:
    store.set_setting(TAGS_KEY, ",".join(t for t in tags if t.strip()))


def get_watch_threshold(store: Store) -> float:
    """Watched-confirmation threshold in seconds (default 10 minutes)."""
    raw = store.get_setting(THRESHOLD_KEY, "")
    try:
        minutes = max(1, int(float(raw)))
    except (TypeError, ValueError):
        minutes = 10
    return minutes * 60.0


def _int_setting(store: Store, key: str, default: int, lo: int, hi: int) -> int:
    raw = store.get_setting(key, "")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def get_grid_columns(store: Store) -> int:
    return _int_setting(store, COLUMNS_KEY, 2, 1, 4)


def get_grid_rows(store: Store) -> int:
    return _int_setting(store, ROWS_KEY, 3, 1, 8)


def get_card_image_width(store: Store) -> int:
    return _int_setting(store, IMAGE_WIDTH_KEY, 140, 100, 450)
