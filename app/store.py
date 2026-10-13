"""SQLite store behind a small repository interface, shared by the pipeline and feedback.

The web pages (`web/`, in Rust) read the same database. `web/pipeline-schema.sql` is a copy of
SCHEMA that their queries are checked against; a test keeps the two equal, so after changing
SCHEMA regenerate the copy and run `web/dev-db.sh`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Source, User
from .tokens import PROBLEM, REASON_SIGNALS, meaning, with_reason

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
    story TEXT NOT NULL DEFAULT '',
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
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS selections (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    policy TEXT NOT NULL,
    score REAL,
    reason TEXT NOT NULL DEFAULT '',
    rank INTEGER,
    explore INTEGER NOT NULL DEFAULT 0,
    propensity REAL,
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS extraction_reviews (
    id INTEGER PRIMARY KEY,
    article_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT ''
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
    reason: str = ""  # keys of tokens.REASONS, comma-separated, when the reader said why


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
        if "reason" not in {row["name"] for row in self.db.execute("PRAGMA table_info(ratings)")}:
            with self.db:
                self.db.execute("ALTER TABLE ratings ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        if "story" not in {row["name"] for row in self.db.execute("PRAGMA table_info(deliveries)")}:
            with self.db:
                self.db.execute("ALTER TABLE deliveries ADD COLUMN story TEXT NOT NULL DEFAULT ''")
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

    # readers (owned and edited by the web app; see web/migrations)

    def readers(self) -> tuple[User, ...]:
        """Every reader with their delivery settings; none until the web app has made an account."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone():
            return ()
        rows = self.db.execute(
            "SELECT id, name, kindle_addresses, daily_stories, interests FROM users ORDER BY created_at, id"
        ).fetchall()
        return tuple(
            User(
                id=row["id"],
                name=row["name"],
                kindle_addresses=tuple(line.strip() for line in row["kindle_addresses"].splitlines() if line.strip()),
                daily_cap=row["daily_stories"],
                interests=row["interests"],
            )
            for row in rows
        )

    def sources(self) -> tuple[Source, ...]:
        """The sources to read, as set on the web app's sources page; paused ones are left out."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'").fetchone():
            return ()
        rows = self.db.execute(
            "SELECT name, kind, url, recipe, extract, pinned FROM sources WHERE enabled = 1"
            " ORDER BY pinned DESC, name COLLATE NOCASE"
        ).fetchall()
        return tuple(
            Source(
                name=row["name"],
                url=row["url"],
                type=row["kind"],
                recipe=row["recipe"],
                extract=row["extract"],
                pinned=bool(row["pinned"]),
            )
            for row in rows
        )

    # send requests (queued by the web app's "Send to Kindle" button)

    def send_requests(self) -> list[sqlite3.Row]:
        """Requests not yet handled, oldest first; none until the web app has made its table."""
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'send_requests'"
        ).fetchone():
            return []
        return self.db.execute(
            "SELECT id, user_id, article_id FROM send_requests WHERE status = 'queued' ORDER BY id"
        ).fetchall()

    def finish_send(self, request_id: int, status: str, error: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE send_requests SET status = ?, error = ?, done_at = ? WHERE id = ?",
                (status, error, now_iso(), request_id),
            )

    # articles

    def upsert_article(self, id: str, url: str, source: str, title: str, author: str, published: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO articles (id, url, source, title, author, published, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (id, url, source, title, author, published, now_iso()),
            )

    def has_articles_from(self, source: str) -> bool:
        """Whether this source has been read before."""
        return self.db.execute("SELECT 1 FROM articles WHERE source = ? LIMIT 1", (source,)).fetchone() is not None

    def set_extraction(self, id: str, status: str, reason: str, word_count: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE articles SET status = ?, reason = ?, word_count = ? WHERE id = ?",
                (status, reason, word_count, id),
            )

    def set_title(self, id: str, title: str) -> None:
        with self.db:
            self.db.execute("UPDATE articles SET title = ? WHERE id = ?", (title, id))

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

    def mark_delivered(
        self, user_id: str, article_ids: list[str], when: datetime, stories: dict[str, str] | None = None
    ) -> None:
        """`stories` maps an article to its story; articles sharing one count once against the cap."""
        stamp = when.astimezone(UTC).isoformat(timespec="seconds")
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO deliveries (user_id, article_id, delivered_at, story) VALUES (?, ?, ?, ?)",
                [(user_id, a, stamp, (stories or {}).get(a, "")) for a in article_ids],
            )

    def delivered_on(self, user_id: str, utc_start: str, utc_end: str) -> int:
        """Stories delivered in the period: articles that share a story count once."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT CASE WHEN story = '' THEN article_id ELSE story END) FROM deliveries"
            " WHERE user_id = ? AND delivered_at >= ? AND delivered_at < ?",
            (user_id, utc_start, utc_end),
        ).fetchone()[0]

    # feedback

    def record_feedback(
        self, user_id: str, article_id: str, signal: str, source: str, when: str | None = None
    ) -> Rating | None:
        """Log the tap and apply it: the latest tap decides the stored rating and finished flag.

        A reason tap sets only the reason. A rating tap drops a stored reason that no longer
        fits it (a "too thin" on an article now rated 5). A problem report is logged, and noted
        in the extraction log for whoever runs the install; it leaves the rating as it was.
        """
        if signal == PROBLEM:
            with self.db:
                self.db.execute(
                    "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (user_id, article_id, signal, source, when or now_iso()),
                )
            self.record_review(article_id, "bad", "reader: broken or incomplete", "reader")
            return self.get_rating(user_id, article_id)
        rating, finished = meaning(signal)
        reason = signal.removeprefix("why-") if signal in REASON_SIGNALS else None
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
            now = self.get_rating(user_id, article_id)
            # A reason tap adds, replaces or takes back; any tap drops reasons that no longer
            # fit the rating. An empty key leaves the choice alone and only drops the unfit.
            kept = with_reason(with_reason(now.reason, "", now.rating), reason or "", now.rating)
            if kept != now.reason:
                self.db.execute(
                    "UPDATE ratings SET reason = ? WHERE user_id = ? AND article_id = ?", (kept, user_id, article_id)
                )
        return self.get_rating(user_id, article_id)

    def get_rating(self, user_id: str, article_id: str) -> Rating | None:
        row = self.db.execute(
            "SELECT rating, finished, reason FROM ratings WHERE user_id = ? AND article_id = ?",
            (user_id, article_id),
        ).fetchone()
        return Rating(row["rating"], bool(row["finished"]), row["reason"]) if row else None

    def feedback_rows(self) -> list[sqlite3.Row]:
        """Every tap, oldest first."""
        return self.db.execute(
            "SELECT f.created_at, f.user_id, f.signal, f.source, f.article_id,"
            " COALESCE(a.title, '') AS title, COALESCE(a.source, '') AS article_source"
            " FROM feedback_events f LEFT JOIN articles a ON a.id = f.article_id ORDER BY f.id"
        ).fetchall()

    def rating_rows(self, user_id: str | None = None, latest: int | None = None) -> list[sqlite3.Row]:
        """The current rating per user and article, oldest first; optionally one user's latest."""
        rows = self.db.execute(
            "SELECT r.updated_at, r.user_id, r.rating, r.finished, r.reason, r.article_id,"
            " COALESCE(a.title, '') AS title, COALESCE(a.source, '') AS article_source,"
            " COALESCE(a.word_count, 0) AS word_count, COALESCE(s.reason, '') AS picked_because"
            " FROM ratings r LEFT JOIN articles a ON a.id = r.article_id"
            " LEFT JOIN selections s ON s.user_id = r.user_id AND s.article_id = r.article_id"
            " WHERE (? IS NULL OR r.user_id = ?) ORDER BY r.updated_at",
            (user_id, user_id),
        ).fetchall()
        return rows[-latest:] if latest else rows

    # llm use and selection records

    def log_llm_call(
        self, purpose: str, model: str, input_tokens: int, output_tokens: int, status: str, error: str = ""
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO llm_calls (created_at, purpose, model, input_tokens, output_tokens, status, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), purpose, model, input_tokens, output_tokens, status, error),
            )

    def llm_tokens_since(self, start: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM llm_calls WHERE created_at >= ?",
            (start,),
        ).fetchone()
        return row["n"]

    def llm_tokens_split_since(self, start: str) -> tuple[int, int]:
        """Input and output tokens logged since `start`."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) FROM llm_calls"
            " WHERE created_at >= ?",
            (start,),
        ).fetchone()
        return row[0], row[1]

    def llm_call_rows(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]

    def record_selection(
        self,
        user_id: str,
        article_id: str,
        when: datetime,
        policy: str,
        score: float | None,
        reason: str,
        rank: int | None,
        explore: bool,
        propensity: float | None,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO selections"
                " (user_id, article_id, selected_at, policy, score, reason, rank, explore, propensity)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    article_id,
                    when.astimezone(UTC).isoformat(timespec="seconds"),
                    policy,
                    score,
                    reason,
                    rank,
                    int(explore),
                    propensity,
                ),
            )

    def selection_rows(self, user_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM selections WHERE user_id = ? ORDER BY selected_at, rank", (user_id,)
        ).fetchall()

    # extraction reviews (the model's second opinion; log only)

    def record_review(self, article_id: str, verdict: str, problem: str, model: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO extraction_reviews (article_id, checked_at, verdict, problem, model)"
                " VALUES (?, ?, ?, ?, ?)",
                (article_id, now_iso(), verdict, problem, model),
            )

    def review_rows(self, limit: int = 200) -> list[sqlite3.Row]:
        """Newest first, with the article and the extractor's own verdict beside the model's."""
        return self.db.execute(
            "SELECT r.checked_at, r.verdict, r.problem, a.title, a.url, a.source, a.status, a.reason, a.word_count"
            " FROM extraction_reviews r LEFT JOIN articles a ON a.id = r.article_id"
            " ORDER BY r.id DESC LIMIT ?",
            (limit,),
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
