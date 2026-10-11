"""SQLite store behind a small repository interface, shared by the pipeline and feedback."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .tokens import meaning

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    published TEXT,
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    image_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deliveries (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    rating INTEGER,
    finished INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ArticleRow:
    id: str
    url: str
    source: str
    title: str
    author: str
    published: str | None
    status: str
    reason: str
    word_count: int
    image_url: str = ""
    description: str = ""


@dataclass(frozen=True)
class Rating:
    rating: int | None  # 1-5, None when only "finished" was recorded
    finished: bool


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {row["name"] for row in self.db.execute("PRAGMA table_info(articles)")}
        for column in ("image_url", "description"):
            if column not in have:
                with self.db:
                    self.db.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        # The first schema kept one row per signal; replay it into the event log and ratings.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'feedback'").fetchone():
            old = self.db.execute(
                "SELECT user_id, article_id, signal, source, created_at FROM feedback ORDER BY created_at"
            ).fetchall()
            for row in old:
                self.record_feedback(*row)
            with self.db:
                self.db.execute("DROP TABLE feedback")

    def close(self) -> None:
        self.db.close()

    # articles

    def upsert_article(self, id: str, url: str, source: str, title: str, author: str, published: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO articles (id, url, source, title, author, published, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (id, url, source, title, author, published, now_iso()),
            )

    def set_extraction(self, id: str, status: str, reason: str, word_count: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE articles SET status = ?, reason = ?, word_count = ? WHERE id = ?",
                (status, reason, word_count, id),
            )

    def set_preview(self, id: str, image_url: str, description: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE articles SET image_url = ?, description = ? WHERE id = ?",
                (image_url, description, id),
            )

    def get_article(self, id: str) -> ArticleRow | None:
        row = self.db.execute(
            "SELECT id, url, source, title, author, published, status, reason, word_count,"
            " image_url, description FROM articles WHERE id = ?",
            (id,),
        ).fetchone()
        return ArticleRow(**dict(row)) if row else None

    # deliveries

    def is_delivered(self, user_id: str, article_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE user_id = ? AND article_id = ?",
                (user_id, article_id),
            ).fetchone()
            is not None
        )

    def mark_delivered(self, user_id: str, article_ids: list[str], when: datetime) -> None:
        stamp = when.astimezone(UTC).isoformat(timespec="seconds")
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO deliveries (user_id, article_id, delivered_at) VALUES (?, ?, ?)",
                [(user_id, a, stamp) for a in article_ids],
            )

    def delivered_on(self, user_id: str, utc_start: str, utc_end: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE user_id = ? AND delivered_at >= ? AND delivered_at < ?",
            (user_id, utc_start, utc_end),
        ).fetchone()[0]

    # feedback

    def record_feedback(
        self, user_id: str, article_id: str, signal: str, source: str, when: str | None = None
    ) -> Rating:
        """Log the tap and apply it: the latest tap decides the stored rating and finished flag."""
        rating, finished = meaning(signal)
        when = when or now_iso()
        with self.db:
            self.db.execute(
                "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, article_id, signal, source, when),
            )
            self.db.execute(
                "INSERT INTO ratings (user_id, article_id, rating, finished, updated_at)"
                " VALUES (?, ?, ?, COALESCE(?, 0), ?)"
                " ON CONFLICT(user_id, article_id) DO UPDATE SET"
                " rating = COALESCE(excluded.rating, rating),"
                " finished = COALESCE(?, finished), updated_at = excluded.updated_at",
                (user_id, article_id, rating, finished, when, finished),
            )
        return self.get_rating(user_id, article_id)

    def get_rating(self, user_id: str, article_id: str) -> Rating | None:
        row = self.db.execute(
            "SELECT rating, finished FROM ratings WHERE user_id = ? AND article_id = ?",
            (user_id, article_id),
        ).fetchone()
        return Rating(row["rating"], bool(row["finished"])) if row else None

    def feedback_rows(self) -> list[sqlite3.Row]:
        """Every tap, oldest first."""
        return self.db.execute(
            "SELECT f.created_at, f.user_id, f.signal, f.source, f.article_id,"
            " COALESCE(a.title, '') AS title, COALESCE(a.source, '') AS article_source"
            " FROM feedback_events f LEFT JOIN articles a ON a.id = f.article_id ORDER BY f.id"
        ).fetchall()

    def rating_rows(self) -> list[sqlite3.Row]:
        """The current rating per user and article."""
        return self.db.execute(
            "SELECT r.updated_at, r.user_id, r.rating, r.finished, r.article_id,"
            " COALESCE(a.title, '') AS title, COALESCE(a.source, '') AS article_source"
            " FROM ratings r LEFT JOIN articles a ON a.id = r.article_id ORDER BY r.updated_at"
        ).fetchall()

    # source health

    def track_source(self, source: str, error: str = "") -> int:
        """Record one run's outcome for a source. Returns its consecutive failures (0 = fine)."""
        with self.db:
            self.db.execute(
                "INSERT INTO source_health (source, failures, last_error, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(source) DO UPDATE SET"
                " failures = CASE WHEN excluded.failures = 0 THEN 0 ELSE failures + 1 END,"
                " last_error = excluded.last_error, updated_at = excluded.updated_at",
                (source, 1 if error else 0, error, now_iso()),
            )
        row = self.db.execute("SELECT failures FROM source_health WHERE source = ?", (source,)).fetchone()
        return row["failures"]

    # http cache validators

    def get_validators(self, url: str) -> tuple[str | None, str | None]:
        row = self.db.execute("SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)).fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    def set_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO http_cache (url, etag, last_modified) VALUES (?, ?, ?)"
                " ON CONFLICT(url) DO UPDATE SET etag = excluded.etag,"
                " last_modified = excluded.last_modified",
                (url, etag, last_modified),
            )
