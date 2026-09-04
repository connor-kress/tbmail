from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config
from .index import (
    MessageContent,
    count_messages,
    find_message,
    parse_since,
    read_message,
    refresh_message,
    search_messages,
)
from .profile import (
    ProfileError,
    discover_accounts,
    discover_profile,
    folder_counts,
    resolve_folder,
    select_accounts,
)


@dataclass
class AccountResult:
    alias: str | None
    email: str
    hostname: str


@dataclass
class AccountsResult:
    profile: Path
    accounts: list[AccountResult]


@dataclass
class FolderResult:
    account: str
    email: str
    folder: str
    total: int | None
    unread: int | None
    server_total: int | None
    server_unread: int | None
    last_sync: int | None


@dataclass
class StatusResult:
    cache_updated: str
    folders: list[FolderResult]


@dataclass
class CountResult:
    count: int


@dataclass
class MessageResult:
    public_id: str = field(metadata={"json_name": "id"})
    account: str
    folder: str
    message_id: str | None
    subject: str
    sender: str = field(metadata={"json_name": "from"})
    recipients: str = field(metadata={"json_name": "to"})
    date: str | None
    is_read: bool | None = field(metadata={"json_name": "read"})


@dataclass
class SearchResult:
    messages: list[MessageResult]


CommandResult = (
    AccountsResult | StatusResult | CountResult | SearchResult | MessageContent
)


def _account_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-a",
        "--account",
        action="append",
        metavar="ALIAS",
        help="configured account alias; repeatable, or use 'all'",
    )
    group.add_argument(
        "--account-raw",
        action="append",
        metavar="EMAIL",
        help="Thunderbird account email address; repeatable",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbmail", description="Read locally downloaded Thunderbird mail"
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--profile", type=Path, help="path to Thunderbird profile")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("accounts", help="list discovered IMAP accounts")

    status = subparsers.add_parser("status", help="show folder message status")
    _account_arguments(status)
    status.add_argument(
        "--folder", default="inbox", help="folder name (default: inbox)"
    )

    search = subparsers.add_parser("search", help="search downloaded message headers")
    _account_arguments(search)
    search.add_argument("query", nargs="?", help="text in subject or address headers")
    search.add_argument(
        "--folder", default="inbox", help="folder name (default: inbox)"
    )
    search.add_argument("--from", dest="sender", help="sender text")
    search.add_argument("--subject", help="subject text")
    search.add_argument("--unread", action="store_true", help="only unread messages")
    search.add_argument("--since", metavar="YYYY-MM-DD", help="earliest message date")
    search.add_argument(
        "--count", action="store_true", help="return the number of matching messages"
    )
    search.add_argument("--limit", type=int, help="maximum results")
    search.add_argument(
        "--offset",
        type=int,
        default=None,
        help="number of sorted results to skip (default: 0)",
    )

    show = subparsers.add_parser("show", help="display a message returned by search")
    show.add_argument("message_id", help="tbmail message ID")
    show.add_argument(
        "--max-body-chars", type=int, default=100_000, help="maximum body characters"
    )
    return parser


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.metadata.get("json_name", item.name): _json_value(
                getattr(value, item.name)
            )
            for item in fields(value)
        }
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _output(value: CommandResult) -> None:
    json.dump(_json_value(value), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _account_name(email: str, aliases: dict[str, str]) -> str:
    return next(
        (
            alias
            for alias, address in aliases.items()
            if address.casefold() == email.casefold()
        ),
        email,
    )


def run(args: argparse.Namespace) -> CommandResult:
    config = load_config(args.config)
    profile = discover_profile(args.profile)
    accounts = discover_accounts(profile, config)

    if args.command == "accounts":
        return AccountsResult(
            profile=profile,
            accounts=[
                AccountResult(
                    alias=account.alias,
                    email=account.email,
                    hostname=account.hostname,
                )
                for account in accounts
            ],
        )

    if args.command == "show":
        message = find_message(args.message_id)
        try:
            message.folder_path.resolve().relative_to(profile.resolve())
        except ValueError as exc:
            raise ValueError("Message does not belong to the selected profile") from exc
        message = refresh_message(message, accounts, profile)
        if args.max_body_chars < 1:
            raise ValueError("--max-body-chars must be positive")
        result = read_message(message, args.max_body_chars)
        result.account = _account_name(message.account_email, config.aliases)
        return result

    selected = select_accounts(accounts, config, args.account, args.account_raw)
    accounts_and_folders = [
        (account, resolve_folder(account, args.folder)) for account in selected
    ]

    if args.command == "status":
        cache_mtime = (profile / "folderCache.json").stat().st_mtime
        results: list[FolderResult] = []
        for account, folder in accounts_and_folders:
            counts = folder_counts(profile, folder)
            results.append(
                FolderResult(
                    account=account.name,
                    email=account.email,
                    folder=args.folder,
                    total=counts.total,
                    unread=counts.unread,
                    server_total=counts.server_total,
                    server_unread=counts.server_unread,
                    last_sync=counts.last_sync,
                )
            )
        return StatusResult(
            cache_updated=datetime.fromtimestamp(cache_mtime).astimezone().isoformat(),
            folders=results,
        )

    if args.count and (args.limit is not None or args.offset is not None):
        raise ValueError("--count cannot be combined with --limit or --offset")
    if args.limit is not None and (args.limit < 1 or args.limit > 1000):
        raise ValueError("--limit must be between 1 and 1000")
    if args.offset is not None and args.offset < 0:
        raise ValueError("--offset must be nonnegative")
    since_epoch = parse_since(args.since) if args.since else None
    if args.count:
        return CountResult(
            count=count_messages(
                accounts_and_folders,
                profile,
                args.query,
                args.sender,
                args.subject,
                args.unread,
                since_epoch,
            )
        )
    messages = search_messages(
        accounts_and_folders,
        profile,
        args.query,
        args.sender,
        args.subject,
        args.unread,
        since_epoch,
        args.limit,
        args.offset or 0,
    )
    return SearchResult(
        messages=[
            MessageResult(
                public_id=message.public_id,
                account=_account_name(message.account_email, config.aliases),
                folder=args.folder,
                message_id=message.message_id,
                subject=message.subject,
                sender=message.sender,
                recipients=message.recipients,
                date=message.date,
                is_read=message.is_read,
            )
            for message in messages
        ]
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _output(run(args))
    except (ConfigError, ProfileError, OSError, sqlite3.Error, ValueError) as exc:
        json.dump({"error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
