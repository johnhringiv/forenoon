"""Configuration loading. Recipients and secrets come only from here and the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RECIPE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
DISCOVERY_SITES = ("hackernews", "lobsters")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: str
    name: str
    kindle_addresses: tuple[str, ...]
    daily_cap: int = 8
    interests: str = ""  # in the user's own words; given to the ranking model


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
    """Ranking with Claude. Off by default; the key comes from FORENOON_ANTHROPIC_API_KEY."""

    enabled: bool = False
    model: str = "claude-haiku-5-5"
    daily_token_cap: int = 200_000  # input + output tokens per local day, all users
    max_candidates: int = 80  # abstracts sent to the model per user per run
    explore_slots: int = 1  # 1 = the last slot of the day goes to a random lower-ranked article
    show_reasons: bool = True  # print the model's one-line reason in the book
    check_extraction: bool = True  # ask the model whether each article looks complete; log only
    timeout: float = 60.0  # seconds


@dataclass(frozen=True)
class Discovery:
    """Front pages of link aggregators as extra candidates. Only used when ranking is on."""

    enabled: bool = False
    sites: tuple[str, ...] = ("hackernews", "lobsters")
    slots: int = 2  # at most this many of the day's articles, and never more than half
    min_score: float = 6.0  # a found article needs at least this score to take a slot
    per_site: int = 25  # candidates sent to the model per site


@dataclass(frozen=True)
class Alerts:
    """Operator notices, sent through the same SMTP relay. Off while `to` is empty."""

    to: str = ""
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


@dataclass(frozen=True)
class Config:
    data_dir: Path
    timezone: str = "UTC"
    delivery_time: str = "06:00"
    public_base_url: str = ""
    max_age_days: int = 3
    token_ttl_days: int = 60
    # Keep each article's fetched page for this many days, for `forenoon replay`. 0 = keep none.
    keep_pages_days: int = 90
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


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _build(cls: type, raw: dict[str, Any], where: str) -> Any:
    known = cls.__dataclass_fields__
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    values = {k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()}
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def load_sources(path: Path) -> tuple[Source, ...]:
    raw = _load_yaml(path).get("sources") or []
    # A recipe's issue is the only copy we may have (subscriber content), so it is not re-fetched.
    recipe_defaults = {"extract": "feed"}
    sources = tuple(
        _build(Source, {**(recipe_defaults if item.get("type") == "recipe" else {}), **item}, f"{path}: source #{i}")
        for i, item in enumerate(raw)
    )
    names = [s.name for s in sources]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: source names must be unique")
    for source in sources:
        if source.type not in ("rss", "recipe"):
            raise ConfigError(f"{path}: {source.name}: type must be rss or recipe")
        if source.type == "rss" and not source.url:
            raise ConfigError(f"{path}: {source.name}: url is required")
        if source.type == "recipe" and not RECIPE_SLUG.fullmatch(source.recipe):
            raise ConfigError(f"{path}: {source.name}: recipe must be a folder name (a-z, 0-9, -)")
        if source.extract not in ("auto", "page", "feed"):
            raise ConfigError(f"{path}: {source.name}: extract must be auto, page or feed")
    return sources


def load_config(path: Path, sources_path: Path | None = None) -> Config:
    raw = _load_yaml(path)
    base = path.parent
    users = tuple(_build(User, u, f"{path}: user #{i}") for i, u in enumerate(raw.pop("users", None) or []))
    if len({u.id for u in users}) != len(users):
        raise ConfigError(f"{path}: user ids must be unique")
    smtp = _build(Smtp, raw.pop("smtp", None) or {}, f"{path}: smtp")
    images = _build(Images, raw.pop("images", None) or {}, f"{path}: images")
    alerts = _build(Alerts, raw.pop("alerts", None) or {}, f"{path}: alerts")
    llm = _build(Llm, raw.pop("llm", None) or {}, f"{path}: llm")
    discovery = _build(Discovery, raw.pop("discovery", None) or {}, f"{path}: discovery")
    unknown = set(discovery.sites) - set(DISCOVERY_SITES)
    if unknown:
        raise ConfigError(f"{path}: discovery: unknown sites {sorted(unknown)}")
    data_dir = Path(os.environ.get("FORENOON_DATA_DIR") or raw.pop("data_dir", "data"))
    raw.pop("data_dir", None)
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    sources_file = sources_path or base / raw.pop("sources_file", "sources.yaml")
    raw.pop("sources_file", None)
    return _build(
        Config,
        {
            **raw,
            "data_dir": data_dir,
            "users": users,
            "smtp": smtp,
            "images": images,
            "alerts": alerts,
            "llm": llm,
            "discovery": discovery,
            "sources": load_sources(Path(sources_file)),
        },
        str(path),
    )
