from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from pathlib import Path

from .bridge import BridgeError, execute, stale_sync_warnings
from .config import ConfigError, load_config
from .index import (
    MessageContent,
    count_messages,
    find_message,
    parse_since,
    read_message,
    refresh_message,
    resolve_message_for_write,
    search_messages,
    update_cached_read_state,
)
from .profile import (
    MailAccount,
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


@dataclass
class FolderSyncResult:
    uri: str
    phase: str
    status: str
    result: str | None = None
    error: object | None = None


@dataclass
class AccountSyncResult:
    account: str
    email: str
    server_key: str
    status: str
    folders: list[FolderSyncResult]
    incomplete_phases: list[str] = field(default_factory=list)
    error: object | None = None


@dataclass
class SyncResult:
    request_id: str
    status: str
    accounts: list[AccountSyncResult]


@dataclass
class MarkResult:
    request_id: str
    status: str
    public_id: str = field(metadata={"json_name": "id"})
    is_read: bool = field(metadata={"json_name": "read"})
    matched_by: str
    folder_uri: str
    cache_updated: bool
    cache_error: str | None = None


CommandResult = (
    AccountsResult
    | StatusResult
    | CountResult
    | SearchResult
    | MessageContent
    | SyncResult
    | MarkResult
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
        prog="tbmail", description="Read and update Thunderbird mail"
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

    sync = subparsers.add_parser(
        "sync", help="refresh Thunderbird and download all message bodies"
    )
    _account_arguments(sync)
    sync.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="total timeout in seconds (default: 300)",
    )

    for command, help_text in (
        ("mark-read", "mark a message as read"),
        ("mark-unread", "mark a message as unread"),
    ):
        mark = subparsers.add_parser(command, help=help_text)
        mark.add_argument("message_id", help="tbmail message ID")
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


def _warn_if_stale(profile: Path, accounts: list[MailAccount]) -> None:
    for warning in stale_sync_warnings(profile, accounts):
        json.dump({"warning": warning}, sys.stderr, separators=(",", ":"))
        sys.stderr.write("\n")


def _sync_result(
    request_id: str,
    response: dict[str, object],
    selected: list[MailAccount],
    aliases: dict[str, str],
) -> SyncResult:
    by_server = {account.server_id: account for account in selected}
    raw_accounts = response.get("accounts")
    if not isinstance(raw_accounts, list):
        raise ValueError("Thunderbird returned an invalid sync result")
    accounts = []
    returned_servers: list[str] = []
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise ValueError("Thunderbird returned an invalid account sync result")
        server_key = str(raw_account.get("serverKey", ""))
        returned_servers.append(server_key)
        account = by_server.get(server_key)
        if not account:
            raise ValueError(
                f"Thunderbird returned an unknown server key: {server_key}"
            )
        raw_folders = raw_account.get("folders", [])
        if not isinstance(raw_folders, list):
            raise ValueError("Thunderbird returned invalid folder sync results")
        folders = [
            FolderSyncResult(
                uri=str(folder.get("uri", "")),
                phase=str(folder.get("phase", "")),
                status=str(folder.get("status", "")),
                result=folder.get("result"),
                error=folder.get("error"),
            )
            for folder in raw_folders
            if isinstance(folder, dict)
        ]
        incomplete_phases = raw_account.get("incompletePhases", [])
        if not isinstance(incomplete_phases, list):
            raise ValueError("Thunderbird returned invalid incomplete sync phases")
        accounts.append(
            AccountSyncResult(
                account=_account_name(account.email, aliases),
                email=account.email,
                server_key=server_key,
                status=str(raw_account.get("status", "")),
                folders=folders,
                incomplete_phases=[str(phase) for phase in incomplete_phases],
                error=raw_account.get("error"),
            )
        )
    expected_servers = [account.server_id for account in selected]
    if sorted(returned_servers) != sorted(expected_servers) or len(
        returned_servers
    ) != len(set(returned_servers)):
        raise ValueError("Thunderbird sync result did not contain every account once")
    return SyncResult(request_id=request_id, status="success", accounts=accounts)


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
        relevant = [
            account
            for account in accounts
            if account.email.casefold() == message.account_email
        ]
        _warn_if_stale(profile, relevant)
        return result

    if args.command in {"mark-read", "mark-unread"}:
        message = find_message(args.message_id)
        message, account = resolve_message_for_write(message, accounts, profile)
        if not message.message_id:
            raise ValueError(
                "Message has no RFC Message-ID and cannot be updated safely"
            )
        desired_read = args.command == "mark-read"
        request_id, response = execute(
            profile,
            config.thunderbird_command,
            args.command,
            {
                "serverKey": account.server_id,
                "folderPath": message.folder_path.relative_to(profile).as_posix(),
                "mboxOffset": message.offset,
                "messageId": message.message_id,
            },
            30,
        )
        cache_updated = True
        cache_error = None
        try:
            update_cached_read_state(message.public_id, desired_read)
        except (OSError, sqlite3.Error, ValueError) as exc:
            cache_updated = False
            cache_error = str(exc)
        return MarkResult(
            request_id=request_id,
            status="success",
            public_id=message.public_id,
            is_read=desired_read,
            matched_by=str(response.get("matchedBy", "")),
            folder_uri=str(response.get("folderUri", "")),
            cache_updated=cache_updated,
            cache_error=cache_error,
        )

    selected = select_accounts(accounts, config, args.account, args.account_raw)

    if args.command == "sync":
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        request_id, response = execute(
            profile,
            config.thunderbird_command,
            "sync",
            {"serverKeys": [account.server_id for account in selected]},
            args.timeout,
        )
        return _sync_result(request_id, response, selected, config.aliases)

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
        result = StatusResult(
            cache_updated=datetime.fromtimestamp(cache_mtime).astimezone().isoformat(),
            folders=results,
        )
        _warn_if_stale(profile, selected)
        return result

    if args.count and (args.limit is not None or args.offset is not None):
        raise ValueError("--count cannot be combined with --limit or --offset")
    if args.limit is not None and (args.limit < 1 or args.limit > 1000):
        raise ValueError("--limit must be between 1 and 1000")
    if args.offset is not None and args.offset < 0:
        raise ValueError("--offset must be nonnegative")
    since_epoch = parse_since(args.since) if args.since else None
    if args.count:
        count = count_messages(
            accounts_and_folders,
            profile=profile,
            query=args.query,
            sender=args.sender,
            subject=args.subject,
            unread=args.unread,
            since_epoch=since_epoch,
        )
        result = CountResult(count)
        _warn_if_stale(profile, selected)
        return result
    messages = search_messages(
        accounts_and_folders,
        profile=profile,
        query=args.query,
        sender=args.sender,
        subject=args.subject,
        unread=args.unread,
        since_epoch=since_epoch,
        limit=args.limit,
        offset=args.offset or 0,
    )
    result = SearchResult(
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
    _warn_if_stale(profile, selected)
    return result


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _output(run(args))
    except BridgeError as exc:
        json.dump(exc.json_value(), sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        raise SystemExit(1) from exc
    except (ConfigError, ProfileError, OSError, sqlite3.Error, ValueError) as exc:
        json.dump({"error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
