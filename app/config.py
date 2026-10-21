"""Configuration: defaults here, deploy settings and secrets from the environment, the
settings an admin saves on the web pages (the `settings` table), and each reader's own (the
`users` table), applied for their run by `for_reader`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

DISCOVERY_SITES = ("hackernews", "lobsters")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: str
    name: str
    kindle_addresses: tuple[str, ...]
    daily_cap: int = 8  # stories a day; a story is one article, or a few on one subject
    interests: str = ""  # in the user's own words; given to the ranking model
    learned: str = ""  # what their ratings have shown; kept by the pipeline (app/learn.py)
    # Their own settings, from the Reading and Account pages; `for_reader` applies them.
    format: str = "issue"
    grayscale: bool = True
    daily_to_kindle: bool = True  # off: the daily issue is read on the web pages only
    explore: bool = True
    most_per_subject: int = 2
    show_reasons: bool = True
    learn: bool = True


@dataclass(frozen=True)
class Smtp:
    host: str = ""
    port: int = 587
    # TLS from the start (port 465) instead of upgrading with STARTTLS (587); never plain text.
    # The web app reads the same FORENOON_SMTP_IMPLICIT_TLS.
    implicit_tls: bool = False
    username: str = ""
    sender: str = ""
    # Send to Kindle limits; see docs/setup-email.md.
    max_message_bytes: int = 45 * 1024 * 1024
    max_attachments: int = 25

    @property
    def password(self) -> str:
        return os.environ.get("FORENOON_SMTP_PASSWORD", "")


@dataclass(frozen=True)
class Images:
    max_width: int = 1200
    max_height: int = 1600
    grayscale: bool = True
    jpeg_quality: int = 70
    max_bytes_per_article: int = 8 * 1024 * 1024
    max_count_per_article: int = 40


@dataclass(frozen=True)
class Llm:
    """Ranking with a model, through the connection its name starts with; that connection's key
    (FORENOON_ANTHROPIC_API_KEY) is required. Without one, or when a call fails or the day's
    tokens are spent, the run falls back to newest first and the alert says why."""

    # Named with its connection (app/models.py). It does all the routine work: ranking, the
    # reading pass, grouping stories, following links, the notes, and the extraction check.
    model: str = "anthropic/claude-haiku-5-5"
    daily_token_cap: int = 600_000  # input + output tokens per local day, all users
    max_candidates: int = 600  # ceiling on abstracts sent to the model per user per run
    batch_size: int = 80  # candidates per ranking call
    # The reading pass: this many of the best-ranked candidates are fetched and read in full
    # by the model, which gives the final scores. 0 turns it off.
    read_candidates: int = 40
    read_words: int = 2500  # of each article; the rest is not sent
    choose_model: str = ""  # for choosing the day's set; "" for `model`
    # Used for the whole run when a model's connection cannot be reached at its start; "" for none.
    fallback_model: str = ""
    # The book check (app/bookcheck.py): an agent compares every article going in a book with its
    # saved page and fixes what extraction got wrong, logging each fix as a problem. It needs tool
    # use with pictures, so only an Anthropic model; off by default, as it costs cents an article.
    book_check: bool = False
    check_model: str = "anthropic/claude-sonnet-5-5"
    # The book check's own budget, apart from `daily_token_cap`: a busy morning of checks must
    # never stop the next run's ranking.
    check_daily_tokens: int = 3_000_000
    # Each reader's own (User), set for their run by `for_reader`:
    most_per_subject: int = 2  # when choosing the day's set: at most this many on one broad subject; 0 for no limit
    explore_slots: int = 1  # 1 = the last slot of the day goes to a random lower-ranked article
    show_reasons: bool = True  # print the model's one-line reason in the book
    learn: bool = True  # keep a note on what the reader's ratings show, and give it to the model
    timeout: float = 60.0  # seconds


@dataclass(frozen=True)
class Discovery:
    """Front pages of link aggregators as extra candidates, ranked with the feeds' articles on
    their score alone. Skipped when there is no model to rank them."""

    # The built-in sources switched on on the Sources page; filled in from the database.
    sites: tuple[str, ...] = ()
    # The most of a day's places each may take, by key, as set there; 0 or missing, no limit.
    daily_max: dict[str, int] = field(default_factory=dict)
    # The parts of each a reader reads, by key, as they chose them there; missing, the front page.
    sections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    per_site: int = 100  # candidates sent to the model per site


@dataclass(frozen=True)
class Alerts:
    """Operator notices, sent through the same SMTP relay."""

    enabled: bool = True
    to: str = ""  # empty: the admins' own sign-in addresses
    source_failures: int = 3  # consecutive failed runs before a source is reported; 0 = never
    on_empty: bool = True  # also report a run that had nothing new to deliver


@dataclass(frozen=True)
class Source:
    name: str
    url: str = ""
    tier: str = "clean"
    pinned: bool = False
    daily_max: int = 0  # at most this many of a day's places; 0 for no limit
    # auto: extract from the page, fall back to feed content. page / feed force one.
    extract: str = "auto"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    timezone: str = "UTC"
    delivery_time: str = "06:00"
    public_base_url: str = ""
    max_age_days: int = 3
    token_ttl_days: int = 60
    # Each article's fetched page and a copy of each book sent are kept for good by default:
    # they are the only copies of what was read, for re-sending, for `forenoon replay`, and for
    # learning from later. A number of days prunes older ones; 0 keeps none; -1 keeps all.
    keep_pages_days: int = -1
    keep_issues_days: int = -1
    # "issue": one book a day with a contents page. "articles": one book per article. The
    # reader's own (User), set for their run by `for_reader`.
    format: str = "issue"
    user_agent: str = "Forenoon/0.1 (self-hosted personal reader)"
    users: tuple[User, ...] = ()
    smtp: Smtp = field(default_factory=Smtp)
    images: Images = field(default_factory=Images)
    alerts: Alerts = field(default_factory=Alerts)
    llm: Llm = field(default_factory=Llm)
    discovery: Discovery = field(default_factory=Discovery)
    # The feeds to read: for a delivery, the ones the reader follows (Store.sources).
    sources: tuple[Source, ...] = ()
    # Sites the reader subscribes to and saved a session for (app/sessions.py): their pages,
    # from any source or added by hand, are fetched with it.
    sessions: tuple[str, ...] = ()

    @property
    def sessions_dir(self) -> Path:
        """Shared with the browser container: page requests for subscriber sessions, and how
        each session's last page went (app/sessions.py)."""
        return self.data_dir / "sessions"

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def issues_dir(self) -> Path:
        return self.data_dir / "issues"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "forenoon.sqlite3"

    @property
    def feedback_secret(self) -> bytes:
        secret = os.environ.get("FORENOON_FEEDBACK_SECRET", "")
        if len(secret) < 32:
            raise ConfigError("FORENOON_FEEDBACK_SECRET must be set to at least 32 characters")
        return secret.encode()

    def user(self, user_id: str) -> User:
        for user in self.users:
            if user.id == user_id:
                return user
        raise ConfigError(f"unknown user: {user_id}")


# Set by the deploy, not on the web pages.
ENVIRONMENT_ONLY = ("data_dir", "timezone", "public_base_url", "users", "sources", "smtp", "discovery.sites")
# Each reader's own, not an admin's: set from User by `for_reader`.
READER_SET = (
    "format",
    "images.grayscale",
    "llm.explore_slots",
    "llm.most_per_subject",
    "llm.show_reasons",
    "llm.learn",
)


def for_reader(cfg: Config, user: User) -> Config:
    """`cfg` with this reader's own settings, for their run and anything sent to them."""
    return replace(
        cfg,
        format=user.format,
        images=replace(cfg.images, grayscale=user.grayscale),
        llm=replace(
            cfg.llm,
            explore_slots=int(user.explore),
            most_per_subject=user.most_per_subject,
            show_reasons=user.show_reasons,
            learn=user.learn,
        ),
    )


SECTIONS = ("images", "alerts", "llm", "discovery")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number") from exc


def from_env() -> Config:
    """The defaults, with the deploy's own settings read from the environment."""
    defaults = Smtp()
    smtp = Smtp(
        host=os.environ.get("FORENOON_SMTP_HOST", ""),
        port=_env_int("FORENOON_SMTP_PORT", defaults.port),
        implicit_tls=os.environ.get("FORENOON_SMTP_IMPLICIT_TLS", "").strip().lower() in ("1", "true", "yes", "on"),
        username=os.environ.get("FORENOON_SMTP_USERNAME", ""),
        sender=os.environ.get("FORENOON_SMTP_SENDER", ""),
        max_message_bytes=_env_int("FORENOON_SMTP_MAX_MESSAGE_BYTES", defaults.max_message_bytes),
    )
    return Config(
        data_dir=Path(os.environ.get("FORENOON_DATA_DIR") or "data").resolve(),
        timezone=os.environ.get("TZ") or "UTC",
        public_base_url=os.environ.get("FORENOON_PUBLIC_URL", "").strip(),
        smtp=smtp,
    )


def _parse(kind: type, raw: str) -> bool | int | float | str:
    if kind is bool:
        if raw.strip().lower() in ("1", "true", "yes", "on"):
            return True
        if raw.strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError("not yes or no")
    if kind is int:
        return int(raw.strip())
    if kind is float:
        return float(raw.strip())
    return raw.strip()


def _sensible(key: str, value: object) -> bool:
    if key == "delivery_time":
        return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value)))
    if key == "format":
        return value in ("issue", "articles")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value >= (-1 if key in ("keep_pages_days", "keep_issues_days") else 0)
    return True


def setting_keys() -> dict[str, type]:
    """Every setting an admin may save, by its key (`delivery_time`, `llm.model`), with its type."""
    keys: dict[str, type] = {}
    for section, cls in (
        ("", Config),
        *((name, Config.__dataclass_fields__[name].default_factory) for name in SECTIONS),
    ):
        for spec in fields(cls):
            key = f"{section}.{spec.name}" if section else spec.name
            if (
                key not in ENVIRONMENT_ONLY
                and key not in READER_SET
                and key not in SECTIONS
                and type(spec.default) in (bool, int, float, str)
            ):
                keys[key] = type(spec.default)
    return keys


def with_settings(cfg: Config, saved: dict[str, str]) -> tuple[Config, list[str]]:
    """`cfg` with the saved settings applied, and what was wrong with any that were left out.

    A setting that is unknown or does not parse is skipped, never fatal: the web pages check
    what they save, and one bad row must not stop the morning's delivery.
    """
    known = setting_keys()
    problems: list[str] = []
    changes: dict[str, dict[str, object]] = {}
    for key, raw in saved.items():
        if key not in known:
            problems.append(f"{key}: not a setting")
            continue
        try:
            value = _parse(known[key], raw)
            if not _sensible(key, value):
                raise ValueError(raw)
        except ValueError:
            problems.append(f"{key}: cannot use {raw[:40]!r}")
            continue
        section, _, name = key.rpartition(".")
        changes.setdefault(section, {})[name] = value
    for section, values in changes.items():
        cfg = replace(cfg, **{section: replace(getattr(cfg, section), **values)}) if section else replace(cfg, **values)
    return cfg, problems
