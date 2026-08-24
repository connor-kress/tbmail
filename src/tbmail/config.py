from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    path: Path
    aliases: dict[str, str]


def default_config_path() -> Path:
    configured = os.environ.get("TBMAIL_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "tbmail" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    config_path = (path or default_config_path()).expanduser().resolve()
    try:
        with config_path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ConfigError("Config must contain a non-empty [accounts] table")

    aliases: dict[str, str] = {}
    emails: set[str] = set()
    for alias, email in accounts.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ConfigError("Account aliases must be non-empty strings")
        if not isinstance(email, str) or "@" not in email:
            raise ConfigError(f"Invalid email address for account alias {alias!r}")
        normalized_alias = alias.strip().casefold()
        normalized_email = email.strip().casefold()
        if normalized_alias == "all":
            raise ConfigError("The account alias 'all' is reserved")
        if normalized_alias in aliases:
            raise ConfigError(f"Account alias differs only by case: {alias}")
        if normalized_email in emails:
            raise ConfigError(f"Email address is configured more than once: {email}")
        aliases[normalized_alias] = email.strip()
        emails.add(normalized_email)

    return Config(config_path, aliases)
