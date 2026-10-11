"""Configuration loading. Recipients and secrets come only from here and the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: str
    name: str
    kindle_addresses: tuple[str, ...]
    daily_cap: int = 8


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
class Alerts:
    """Operator notices, sent through the same SMTP relay. Off while `to` is empty."""

    to: str = ""
    source_failures: int = 3  # consecutive failed runs before a source is reported; 0 = never
    on_empty: bool = True  # also report a run that had nothing new to deliver


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    type: str = "rss"
    tier: str = "clean"
    pinned: bool = False
    # auto: extract from the page, fall back to feed content. page / feed force one.
    extract: str = "auto"
    users: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    timezone: str = "UTC"
    delivery_time: str = "06:00"
    public_base_url: str = ""
    max_age_days: int = 3
    token_ttl_days: int = 60
    # "issue": one book a day with a contents page. "articles": one book per article.
    format: str = "issue"
    user_agent: str = "Forenoon/0.1 (self-hosted personal reader)"
    users: tuple[User, ...] = ()
    smtp: Smtp = field(default_factory=Smtp)
    images: Images = field(default_factory=Images)
    alerts: Alerts = field(default_factory=Alerts)
    sources: tuple[Source, ...] = ()

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
    sources = tuple(_build(Source, item, f"{path}: source #{i}") for i, item in enumerate(raw))
    names = [s.name for s in sources]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: source names must be unique")
    for source in sources:
        if source.type != "rss":
            raise ConfigError(f"{path}: {source.name}: only type 'rss' is supported in Phase 0")
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
            "sources": load_sources(Path(sources_file)),
        },
        str(path),
    )
