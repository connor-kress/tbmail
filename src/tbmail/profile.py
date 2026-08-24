from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config


class ProfileError(ValueError):
    pass


PREF_RE = re.compile(
    r'^user_pref\("(?P<name>(?:[^"\\]|\\.)*)",\s*'
    r'(?P<value>"(?:[^"\\]|\\.)*"|-?\d+|true|false)\s*\);$'
)


@dataclass(frozen=True)
class MailAccount:
    email: str
    hostname: str
    directory: Path
    server_id: str
    alias: str | None = None

    @property
    def name(self) -> str:
        return self.alias or self.email


def _profile_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".thunderbird",
        home / ".var/app/org.mozilla.Thunderbird/.thunderbird",
        home / ".var/app/org.mozilla.thunderbird_esr/.thunderbird",
        home / "snap/thunderbird/common/.thunderbird",
    ]


def discover_profile(override: Path | None = None) -> Path:
    configured = override or (
        Path(os.environ["TBMAIL_PROFILE"]).expanduser()
        if os.environ.get("TBMAIL_PROFILE")
        else None
    )
    if configured:
        profile = configured.expanduser().resolve()
        if not (profile / "prefs.js").is_file():
            raise ProfileError(f"Not a Thunderbird profile: {profile}")
        return profile

    for root in _profile_roots():
        profiles_ini = root / "profiles.ini"
        if not profiles_ini.is_file():
            continue
        parser = configparser.ConfigParser()
        parser.read(profiles_ini)

        install_profiles = [
            section.get("Default")
            for name, section in parser.items()
            if name.startswith("Install") and section.get("Default")
        ]
        default_profiles = [
            section.get("Path")
            for name, section in parser.items()
            if name.startswith("Profile") and section.get("Default") == "1"
        ]
        other_profiles = [
            section.get("Path")
            for name, section in parser.items()
            if name.startswith("Profile") and section.get("Path")
        ]
        for relative_path in install_profiles + default_profiles + other_profiles:
            profile = (root / relative_path).resolve()
            if (profile / "prefs.js").is_file():
                return profile

    raise ProfileError("No Thunderbird profile was found")


def _decode_pref_value(raw_value: str) -> object:
    if raw_value.startswith('"'):
        return json.loads(raw_value)
    if raw_value == "true":
        return True
    if raw_value == "false":
        return False
    return int(raw_value)


def read_preferences(profile: Path) -> dict[str, object]:
    preferences: dict[str, object] = {}
    try:
        lines = (
            (profile / "prefs.js")
            .read_text(encoding="utf-8", errors="surrogateescape")
            .splitlines()
        )
    except OSError as exc:
        raise ProfileError(f"Could not read Thunderbird preferences: {exc}") from exc

    for line in lines:
        match = PREF_RE.match(line)
        if match:
            preferences[match.group("name")] = _decode_pref_value(match.group("value"))
    return preferences


def discover_accounts(profile: Path, config: Config) -> list[MailAccount]:
    preferences = read_preferences(profile)
    aliases_by_email = {
        email.casefold(): alias for alias, email in config.aliases.items()
    }
    accounts: list[MailAccount] = []
    configured_accounts = preferences.get("mail.accountmanager.accounts", "")
    active_servers = {
        preferences.get(f"mail.account.{account_id}.server")
        for account_id in str(configured_accounts).split(",")
    }

    for key, value in preferences.items():
        match = re.fullmatch(r"mail\.server\.(server\d+)\.type", key)
        if not match or value != "imap" or match.group(1) not in active_servers:
            continue
        server_id = match.group(1)
        prefix = f"mail.server.{server_id}."
        email = preferences.get(prefix + "userName")
        hostname = preferences.get(prefix + "hostname")
        relative_directory = preferences.get(prefix + "directory-rel")
        if not all(
            isinstance(item, str) for item in (email, hostname, relative_directory)
        ):
            continue
        if not relative_directory.startswith("[ProfD]"):
            continue
        directory = profile / relative_directory.removeprefix("[ProfD]")
        accounts.append(
            MailAccount(
                email=email,
                hostname=hostname,
                directory=directory,
                server_id=server_id,
                alias=aliases_by_email.get(email.casefold()),
            )
        )

    if not accounts:
        raise ProfileError("No IMAP accounts were found in the Thunderbird profile")
    return sorted(accounts, key=lambda account: account.name)


def select_accounts(
    accounts: list[MailAccount],
    config: Config,
    aliases: list[str] | None,
    raw_addresses: list[str] | None,
) -> list[MailAccount]:
    by_email = {account.email.casefold(): account for account in accounts}

    if raw_addresses:
        selected: list[MailAccount] = []
        for address in raw_addresses:
            account = by_email.get(address.casefold())
            if not account:
                raise ProfileError(f"Thunderbird account not found: {address}")
            selected.append(account)
        return selected

    requested = aliases or ["all"]
    if "all" in [alias.casefold() for alias in requested]:
        if len(requested) != 1:
            raise ProfileError("The account name 'all' cannot be combined with aliases")
        configured_emails = {email.casefold() for email in config.aliases.values()}
        selected = [
            account
            for account in accounts
            if account.email.casefold() in configured_emails
        ]
        found_emails = {account.email.casefold() for account in selected}
        missing = [
            alias
            for alias, email in config.aliases.items()
            if email.casefold() not in found_emails
        ]
        if missing:
            raise ProfileError(
                "Configured accounts not found in Thunderbird: " + ", ".join(missing)
            )
        return selected

    selected = []
    for alias in requested:
        email = config.aliases.get(alias.casefold())
        if not email:
            raise ProfileError(f"Unknown account alias: {alias}")
        account = by_email.get(email.casefold())
        if not account:
            raise ProfileError(
                f"Account alias {alias!r} is not present in the Thunderbird profile"
            )
        selected.append(account)
    return selected


def resolve_folder(account: MailAccount, folder_name: str) -> Path:
    normalized = folder_name.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise ProfileError("Folder name cannot be empty")
    if normalized.casefold() == "inbox":
        relative = Path("INBOX")
    else:
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ProfileError(f"Invalid folder name: {folder_name}")
        relative_parts: list[str] = []
        for index, part in enumerate(parts):
            relative_parts.append(part)
            if index < len(parts) - 1:
                relative_parts[-1] += ".sbd"
        relative = Path(*relative_parts)

    folder = account.directory / relative
    try:
        folder.resolve().relative_to(account.directory.resolve())
    except ValueError as exc:
        raise ProfileError(f"Folder escapes account directory: {folder_name}") from exc
    if not folder.is_file():
        raise ProfileError(
            f"Downloaded folder not found for {account.name}: {folder_name}"
        )
    return folder


def folder_counts(profile: Path, folder: Path) -> dict[str, int | None]:
    cache_path = profile / "folderCache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Could not read Thunderbird folder cache: {exc}") from exc

    profile_relative = folder.relative_to(profile).as_posix() + ".msf"
    matches = [
        value
        for key, value in cache.items()
        if key.replace("\\", "/").endswith(profile_relative)
    ]
    if len(matches) != 1:
        raise ProfileError(f"Folder cache entry not found for {folder}")
    entry = matches[0]
    if not isinstance(entry, dict):
        raise ProfileError(f"Invalid folder cache entry for {folder}")

    def count(name: str) -> int | None:
        value = entry.get(name)
        return value if isinstance(value, int) and value >= 0 else None

    return {
        "total": count("totalMsgs"),
        "unread": count("totalUnreadMsgs"),
        "server_total": count("serverTotal"),
        "server_unread": count("serverUnseen"),
        "last_sync": count("lastSyncTimeInSec"),
    }
