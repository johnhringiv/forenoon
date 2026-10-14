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

from . import prices
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
    link_only INTEGER NOT NULL DEFAULT 0,
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
    error TEXT NOT NULL DEFAULT '',
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL
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
CREATE TABLE IF NOT EXISTS article_routes (
    article_id TEXT NOT NULL,
    route TEXT NOT NULL,
    via TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (article_id, route, via)
);
CREATE TABLE IF NOT EXISTS assessments (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    lasting TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, article_id)
);
"""


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


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
    first_seen: str = ""


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
        delivered = {row["name"] for row in self.db.execute("PRAGMA table_info(deliveries)")}
        if "story" not in delivered:
            with self.db:
                self.db.execute("ALTER TABLE deliveries ADD COLUMN story TEXT NOT NULL DEFAULT ''")
        if "link_only" not in delivered:
            with self.db:
                self.db.execute("ALTER TABLE deliveries ADD COLUMN link_only INTEGER NOT NULL DEFAULT 0")
        calls = {row["name"] for row in self.db.execute("PRAGMA table_info(llm_calls)")}
        if "cost" not in calls:
            # Calls logged before cached tokens were kept apart: their input is priced as if
            # none of it was cached, the best that can be said of them now.
            with self.db:
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0")
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0")
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN cost REAL")
                old = self.db.execute("SELECT id, model, input_tokens, output_tokens FROM llm_calls").fetchall()
                self.db.executemany(
                    "UPDATE llm_calls SET cost = ? WHERE id = ?",
                    [(prices.cost(r["model"], r["input_tokens"], r["output_tokens"]), r["id"]) for r in old],
                )
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
        learned = "learned" if self.can_learn() else "''"  # older web app: no such column
        rows = self.db.execute(
            f"SELECT id, name, kindle_addresses, daily_stories, interests, {learned} AS learned"
            " FROM users ORDER BY created_at, id"
        ).fetchall()
        return tuple(
            User(
                id=row["id"],
                name=row["name"],
                kindle_addresses=tuple(line.strip() for line in row["kindle_addresses"].splitlines() if line.strip()),
                daily_cap=row["daily_stories"],
                interests=row["interests"],
                learned=row["learned"],
            )
            for row in rows
        )

    def can_learn(self) -> bool:
        """Whether the web app has made room for the notes kept on each reader."""
        return self._has_table("profile_history")

    def learned_at(self, user_id: str) -> datetime | None:
        """When the pipeline last looked at this reader's ratings for its notes."""
        row = self.db.execute("SELECT learned_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return datetime.fromisoformat(row["learned_at"]) if row and row["learned_at"] else None

    def set_learned(self, user_id: str, notes: str, when: datetime, changed: bool = True) -> None:
        """Store the notes and when they were written; a changed text also goes in the history."""
        stamp = when.astimezone(UTC).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("UPDATE users SET learned = ?, learned_at = ? WHERE id = ?", (notes, stamp, user_id))
            if changed:
                self.db.execute(
                    "INSERT INTO profile_history (user_id, notes, author, created_at) VALUES (?, ?, 'agent', ?)",
                    (user_id, notes, stamp),
                )

    def sources(self) -> tuple[Source, ...]:
        """The sources to read, as set on the web app's sources page; paused ones are left out."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'").fetchone():
            return ()
        rows = self.db.execute(
            "SELECT id, name, kind, url, recipe, extract, pinned FROM sources"
            " WHERE enabled = 1 AND kind != 'builtin'"
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

    def session_sites(self) -> tuple[str, ...]:
        """The sites a subscriber session is saved for, on the Sources page."""
        if not self._has_table("sessions"):
            return ()
        return tuple(row["domain"] for row in self.db.execute("SELECT domain FROM sessions ORDER BY domain"))

    def _has_table(self, name: str) -> bool:
        return bool(
            self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        )

    def builtin_sites(self, known: dict[str, str]) -> tuple[str, ...]:
        """The built-in sources switched on on the Sources page, by key.

        `known` is every built-in this version has, key to name. One the page has not listed
        yet is added switched off, so an upgrade never changes what is delivered by itself.
        """
        if not self._has_table("sources"):
            return ()
        if not any(column["name"] == "builtin" for column in self.db.execute("PRAGMA table_info(sources)")):
            return ()  # the web app has not been upgraded yet
        rows = {
            row["builtin"]: row["enabled"]
            for row in self.db.execute("SELECT builtin, enabled FROM sources WHERE kind = 'builtin'")
        }
        missing = [(name, key, now_iso()) for key, name in known.items() if key not in rows]
        if missing:
            with self.db:
                self.db.executemany(
                    "INSERT OR IGNORE INTO sources (name, kind, builtin, enabled, created_at)"
                    " VALUES (?, 'builtin', ?, 0, ?)",
                    missing,
                )
        return tuple(key for key in known if rows.get(key))

    def settings(self) -> dict[str, str]:
        """What an admin saved on the Settings page; empty until then."""
        if not self._has_table("settings"):
            return {}
        return {row["key"]: row["value"] for row in self.db.execute("SELECT key, value FROM settings")}

    def admin_emails(self) -> tuple[str, ...]:
        if not self._has_table("users"):
            return ()
        rows = self.db.execute("SELECT email FROM users WHERE role = 'admin' AND email != '' ORDER BY created_at, id")
        return tuple(row["email"] for row in rows)

    def pending_source_checks(self) -> list[sqlite3.Row]:
        """Sources added or saved on the web pages whose address has not been checked yet."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'").fetchone():
            return []
        return self.db.execute(
            "SELECT id, name, kind, url, recipe FROM sources WHERE check_state = 'pending' ORDER BY id"
        ).fetchall()

    def finish_source_check(self, source_id: int, state: str, note: str, url: str | None = None) -> None:
        """Record a check's outcome; `url` replaces the address when the feed was found elsewhere."""
        with self.db:
            self.db.execute(
                "UPDATE sources SET check_state = ?, check_note = ?, url = COALESCE(?, url) WHERE id = ?",
                (state, note, url, source_id),
            )

    # send requests (queued by the web app's "Send to Kindle" button)

    def send_requests(self) -> list[sqlite3.Row]:
        """Requests not yet handled, oldest first; none until the web app has made its table."""
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'send_requests'"
        ).fetchone():
            return []
        return self.db.execute(
            "SELECT id, user_id, article_id, story FROM send_requests WHERE status = 'queued' ORDER BY id"
        ).fetchall()

    def queue_send(self, user_id: str, article_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO send_requests (user_id, article_id, requested_at) VALUES (?, ?, ?)",
                (user_id, article_id, now_iso()),
            )

    # links added by hand (queued by the web app's Archive page)

    def add_requests(self) -> list[sqlite3.Row]:
        """Links not yet fetched, oldest first; none until the web app has made its table."""
        if not self._has_table("add_requests"):
            return []
        return self.db.execute(
            "SELECT id, user_id, url, action, rating FROM add_requests WHERE status = 'queued' ORDER BY id"
        ).fetchall()

    def finish_add(self, request_id: int, status: str, article_id: str = "", error: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE add_requests SET status = ?, article_id = ?, error = ?, done_at = ? WHERE id = ?",
                (status, article_id, error, now_iso(), request_id),
            )

    def added_by(self, user_id: str, article_id: str) -> bool:
        """Whether this reader added the article by hand."""
        if not self._has_table("add_requests"):
            return False
        return bool(
            self.db.execute(
                "SELECT 1 FROM add_requests WHERE user_id = ? AND article_id = ? AND status = 'done'",
                (user_id, article_id),
            ).fetchone()
        )

    def waiting_adds(self, user_id: str) -> list[str]:
        """Articles the reader added for their next issue that have not been delivered, oldest first."""
        if not self._has_table("add_requests"):
            return []
        rows = self.db.execute(
            "SELECT article_id, MIN(id) AS first FROM add_requests r"
            " WHERE user_id = ? AND action = 'next' AND status = 'done'"
            " AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.user_id = r.user_id AND d.article_id = r.article_id)"
            " AND NOT EXISTS (SELECT 1 FROM ratings g WHERE g.user_id = r.user_id AND g.article_id = r.article_id)"
            " GROUP BY article_id ORDER BY first",
            (user_id,),
        ).fetchall()
        return [row["article_id"] for row in rows]

    def story_of(self, user_id: str, article_id: str) -> list[str]:
        """The articles delivered to this reader in the same story as this one, lead first.

        Just the article itself when it was delivered on its own, and nothing when it was not
        delivered to them at all.
        """
        row = self.db.execute(
            "SELECT story, delivered_at FROM deliveries WHERE user_id = ? AND article_id = ?", (user_id, article_id)
        ).fetchone()
        if row is None:
            return []
        if not row["story"]:
            return [article_id]
        rows = self.db.execute(
            "SELECT article_id FROM deliveries WHERE user_id = ? AND story = ? AND delivered_at = ?"
            " ORDER BY article_id != story, rowid",
            (user_id, row["story"], row["delivered_at"]),
        ).fetchall()
        return [r["article_id"] for r in rows]

    def finish_send(self, request_id: int, status: str, error: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE send_requests SET status = ?, error = ?, done_at = ? WHERE id = ?",
                (status, error, now_iso(), request_id),
            )

    # articles

    def upsert_article(
        self,
        id: str,
        url: str,
        source: str,
        title: str,
        author: str,
        published: str | None,
        description: str = "",
        seen: datetime | None = None,
    ) -> None:
        """Recorded once, when first `seen` (default now). `description` is the feed's summary,
        until a page gives one."""
        stamp = seen.astimezone(UTC).isoformat(timespec="seconds") if seen else now_iso()
        with self.db:
            self.db.execute(
                "INSERT INTO articles (id, url, source, title, author, published, first_seen, description)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (id, url, source, title, author, published, stamp, description),
            )

    def unoffered(self, user_id: str, source: str, since: datetime, dated_after: datetime) -> list[ArticleRow]:
        """A source's articles first seen since `since` (and not dated before `dated_after`) that
        this reader has not had and the model has never weighed for them, newest first: what a
        missed run, or a feed that has since moved on, would otherwise lose."""
        rows = self.db.execute(
            "SELECT id, url, source, title, author, published, status, reason, word_count, image_url, description,"
            " first_seen FROM articles a WHERE source = ?1 AND status != 'failed' AND first_seen >= ?3"
            " AND (published IS NULL OR published >= ?4)"
            " AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.user_id = ?2 AND d.article_id = a.id)"
            " AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.user_id = ?2 AND r.article_id = a.id)"
            " AND NOT EXISTS (SELECT 1 FROM assessments s WHERE s.user_id = ?2 AND s.article_id = a.id)"
            " ORDER BY first_seen DESC, COALESCE(published, '') DESC",
            (source, user_id, _utc(since), _utc(dated_after)),
        ).fetchall()
        return [ArticleRow(**dict(row)) for row in rows]

    def has_articles_from(self, source: str) -> bool:
        """Whether this source has been read before."""
        return self.db.execute("SELECT 1 FROM articles WHERE source = ? LIMIT 1", (source,)).fetchone() is not None

    def set_extraction(self, id: str, status: str, reason: str, word_count: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE articles SET status = ?, reason = ?, word_count = ? WHERE id = ?",
                (status, reason, word_count, id),
            )

    def set_word_count(self, id: str, word_count: int) -> None:
        with self.db:
            self.db.execute("UPDATE articles SET word_count = ? WHERE id = ?", (word_count, id))

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
            " image_url, description, first_seen FROM articles WHERE id = ?",
            (id,),
        ).fetchone()
        return ArticleRow(**dict(row)) if row else None

    def add_route(self, article_id: str, route: str, via: str) -> None:
        """Record one way an article reached us. It can have several: a followed blog's post
        that also made a front page is both `feed` and `found`."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO article_routes (article_id, route, via, first_seen) VALUES (?, ?, ?, ?)",
                (article_id, route, via, now_iso()),
            )

    def routes(self, article_id: str) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT route, via FROM article_routes WHERE article_id = ? ORDER BY first_seen, rowid", (article_id,)
        )
        return [(row["route"], row["via"]) for row in rows]

    # assessments: what the model made of a candidate for a reader, delivered or not

    def record_assessments(self, user_id: str, rows: list[tuple[str, float, str, bool, str]]) -> None:
        """`rows` are (article id, score, reason, read in full, lasting).

        A judgement from the headline never replaces one made by reading the article.
        """
        stamp = now_iso()
        with self.db:
            self.db.executemany(
                "INSERT INTO assessments (user_id, article_id, assessed_at, score, reason, read, lasting)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id, article_id) DO UPDATE SET assessed_at = excluded.assessed_at,"
                " score = excluded.score, reason = excluded.reason, read = excluded.read, lasting = excluded.lasting"
                " WHERE excluded.read >= assessments.read",
                [(user_id, a, stamp, score, reason, int(read), lasting) for a, score, reason, read, lasting in rows],
            )

    def assessment(self, user_id: str, article_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT score, reason, read, lasting FROM assessments WHERE user_id = ? AND article_id = ?",
            (user_id, article_id),
        ).fetchone()

    # deliveries

    def is_delivered(self, user_id: str, article_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE user_id = ? AND article_id = ?",
                (user_id, article_id),
            ).fetchone()
            is not None
        )

    def has_had(self, user_id: str, article_id: str) -> bool:
        """Delivered to this reader, or already rated or marked read by them: not to be sent."""
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE user_id = ?1 AND article_id = ?2"
                " UNION ALL SELECT 1 FROM ratings WHERE user_id = ?1 AND article_id = ?2 LIMIT 1",
                (user_id, article_id),
            ).fetchone()
            is not None
        )

    def delivered_ids(self) -> list[str]:
        """Every article delivered to anyone, newest delivery first."""
        rows = self.db.execute(
            "SELECT article_id, MAX(delivered_at) AS at FROM deliveries GROUP BY article_id ORDER BY at DESC"
        )
        return [row["article_id"] for row in rows]

    def mark_delivered(
        self,
        user_id: str,
        article_ids: list[str],
        when: datetime,
        stories: dict[str, str] | None = None,
        link_only: bool = False,
    ) -> None:
        """`stories` maps an article to its story; articles sharing one count once against the cap.

        `link_only`: picked, but its page could not be had, so only the link is shown on the web
        pages and nothing is sent. Sending it later turns it into a delivery on that day.
        """
        stamp = when.astimezone(UTC).isoformat(timespec="seconds")
        with self.db:
            self.db.executemany(
                "INSERT INTO deliveries (user_id, article_id, delivered_at, story, link_only) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id, article_id) DO UPDATE SET delivered_at = excluded.delivered_at,"
                " story = excluded.story, link_only = 0"
                " WHERE deliveries.link_only = 1 AND excluded.link_only = 0",
                [(user_id, a, stamp, (stories or {}).get(a, ""), int(link_only)) for a in article_ids],
            )

    def links_waiting(self, user_id: str, since: str) -> list[ArticleRow]:
        """Picks shown only as links since `since` that the reader has not rated, oldest first:
        each run tries their pages again."""
        rows = self.db.execute(
            "SELECT a.id, a.url, a.source, a.title, a.author, a.published, a.status, a.reason, a.word_count,"
            " a.image_url, a.description, a.first_seen FROM deliveries d JOIN articles a ON a.id = d.article_id"
            " WHERE d.user_id = ?1 AND d.link_only = 1 AND d.delivered_at >= ?2"
            " AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.user_id = ?1 AND r.article_id = a.id)"
            " ORDER BY d.delivered_at, d.rowid",
            (user_id, since),
        ).fetchall()
        return [ArticleRow(**dict(row)) for row in rows]

    def delivered_on(self, user_id: str, utc_start: str, utc_end: str) -> int:
        """Stories delivered in the period: articles that share a story count once."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT CASE WHEN story = '' THEN article_id ELSE story END) FROM deliveries"
            " WHERE user_id = ? AND delivered_at >= ? AND delivered_at < ? AND link_only = 0"
            # A book the reader asked for by hand takes none of the day's places.
            " AND NOT EXISTS (SELECT 1 FROM selections s WHERE s.user_id = deliveries.user_id"
            " AND s.article_id = deliveries.article_id AND s.policy = 'requested')",
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
        self,
        purpose: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        status: str,
        error: str = "",
        cache_write: int = 0,
        cache_read: int = 0,
    ) -> None:
        """One call, with what it cost at the model's published prices (none when unknown)."""
        spent = prices.cost(model, input_tokens, output_tokens, cache_write, cache_read)
        with self.db:
            self.db.execute(
                "INSERT INTO llm_calls (created_at, purpose, model, input_tokens, output_tokens, status, error,"
                " cache_write_tokens, cache_read_tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), purpose, model, input_tokens, output_tokens, status, error, cache_write, cache_read, spent),
            )

    def log_llm_reply(self, purpose: str, model: str, reply, status: str) -> None:
        """A call that was answered: its tokens as the API counted them."""
        self.log_llm_call(
            purpose, model, reply.input_tokens, reply.output_tokens, status,
            cache_write=reply.cache_write_tokens, cache_read=reply.cache_read_tokens,
        )  # fmt: skip

    def llm_tokens_since(self, start: str) -> int:
        """Every token since `start`, cached ones too: what the daily cap counts."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(input_tokens + cache_write_tokens + cache_read_tokens + output_tokens), 0) AS n"
            " FROM llm_calls WHERE created_at >= ?",
            (start,),
        ).fetchone()
        return row["n"]

    def llm_usage_since(self, start: str) -> sqlite3.Row:
        """Tokens and cost since `start`. `unpriced` counts calls to a model with no known price."""
        return self.db.execute(
            "SELECT COALESCE(SUM(input_tokens + cache_write_tokens + cache_read_tokens + output_tokens), 0) AS tokens,"
            " COALESCE(SUM(cost), 0) AS cost, COUNT(*) FILTER (WHERE cost IS NULL AND status != 'error') AS unpriced"
            " FROM llm_calls WHERE created_at >= ?",
            (start,),
        ).fetchone()

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
