"""Configuration: defaults here, deploy settings and secrets from the environment, and the
rest from the settings an admin saves on the web pages (the `settings` table).

Recipients come only from the database, where an admin sets them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

RECIPE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
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


@dataclass(frozen=True)
class Smtp:
    host: str = ""
    port: int = 587
    starttls: bool = True
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
    """Ranking with Claude. Only used when FORENOON_ANTHROPIC_API_KEY is set."""

    enabled: bool = True
    model: str = "claude-haiku-5-5"
    daily_token_cap: int = 600_000  # input + output tokens per local day, all users
    max_candidates: int = 600  # ceiling on abstracts sent to the model per user per run
    batch_size: int = 80  # candidates per ranking call
    group_stories: bool = True  # one more call finds candidates that cover the same event
    follow_links: bool = True  # one more call finds articles that are about a page they link to
    # The reading pass: this many of the best-ranked candidates are fetched and read in full
    # by the model, which gives the final scores. 0 turns it off.
    read_candidates: int = 40
    read_words: int = 2500  # of each article; the rest is not sent
    explore_slots: int = 1  # 1 = the last slot of the day goes to a random lower-ranked article
    show_reasons: bool = True  # print the model's one-line reason in the book
    check_extraction: bool = True  # ask the model whether each article looks complete; log only
    learn: bool = True  # keep a note per reader of what their ratings show, for the prompts
    timeout: float = 60.0  # seconds
    # US dollars per million tokens for `model`, used only to show cost in `forenoon usage`.
    # The API does not report prices, so update these when the model or its price changes.
    input_price: float = 0.10
    output_price: float = 0.50


@dataclass(frozen=True)
class Discovery:
    """Front pages of link aggregators as extra candidates. Only used when ranking is on."""

    # The built-in sources switched on on the Sources page; filled in from the database.
    sites: tuple[str, ...] = ()
    slots: int = 2  # at most this many of the day's articles, and never more than half
    min_score: float = 6.0  # a found article needs at least this score to take a slot
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
    type: str = "rss"  # "rss", or "recipe" for issues written by the recipes container
    tier: str = "clean"
    pinned: bool = False
    # auto: extract from the page, fall back to feed content. page / feed force one.
    extract: str = "auto"
    users: tuple[str, ...] = ()
    recipe: str = ""  # for type "recipe": the folder under <data>/recipes holding its issues
    # For a feed of a site the reader subscribes to: the name of the session its article pages
    # are fetched with (see app/sessions.py). Empty: fetched like any other page.
    session: str = ""
    session_domain: str = ""  # the session is used for this site and its subdomains only


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
    # A failsafe, not the main control (the model choosing articles is): anything longer is a
    # manual or a book and is not delivered. A 77,000-word chapter was in the issue an older
    # Kindle rebooted on. 0 = no limit.
    max_article_words: int = 45_000
    # The most articles one story may bring. The daily cap counts stories, not articles.
    max_story_entries: int = 3
    # "issue": one book a day with a contents page. "articles": one book per article.
    format: str = "issue"
    user_agent: str = "Forenoon/0.1 (self-hosted personal reader)"
    users: tuple[User, ...] = ()
    smtp: Smtp = field(default_factory=Smtp)
    images: Images = field(default_factory=Images)
    alerts: Alerts = field(default_factory=Alerts)
    llm: Llm = field(default_factory=Llm)
    discovery: Discovery = field(default_factory=Discovery)
    sources: tuple[Source, ...] = ()

    @property
    def recipes_dir(self) -> Path:
        return self.data_dir / "recipes"

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

    def sources_for(self, user: User) -> list[Source]:
        return [s for s in self.sources if not s.users or user.id in s.users]


# Set by the deploy, not on the web pages.
ENVIRONMENT_ONLY = ("data_dir", "timezone", "public_base_url", "users", "sources", "smtp", "discovery.sites")
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
        starttls=os.environ.get("FORENOON_SMTP_STARTTLS", "true").strip().lower() not in ("0", "false", "no"),
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
            if key not in ENVIRONMENT_ONLY and key not in SECTIONS and type(spec.default) in (bool, int, float, str):
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
