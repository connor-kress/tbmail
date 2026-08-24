from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config
from .index import (
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

    count = subparsers.add_parser("count", help="show folder message counts")
    _account_arguments(count)
    count.add_argument("--folder", default="inbox", help="folder name (default: inbox)")

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
    search.add_argument("--limit", type=int, default=20, help="maximum results")

    show = subparsers.add_parser("show", help="display a message returned by search")
    show.add_argument("message_id", help="tbmail message ID")
    show.add_argument(
        "--max-body-chars", type=int, default=100_000, help="maximum body characters"
    )
    return parser


def _output(value: object) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False)
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


def run(args: argparse.Namespace) -> object:
    config = load_config(args.config)
    profile = discover_profile(args.profile)
    accounts = discover_accounts(profile, config)

    if args.command == "accounts":
        return {
            "profile": str(profile),
            "accounts": [
                {
                    "alias": account.alias,
                    "email": account.email,
                    "hostname": account.hostname,
                }
                for account in accounts
            ],
        }

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
        result["account"] = _account_name(message.account_email, config.aliases)
        return result

    selected = select_accounts(accounts, config, args.account, args.account_raw)
    accounts_and_folders = [
        (account, resolve_folder(account, args.folder)) for account in selected
    ]

    if args.command == "count":
        cache_mtime = (profile / "folderCache.json").stat().st_mtime
        results = []
        for account, folder in accounts_and_folders:
            results.append(
                {
                    "account": account.name,
                    "email": account.email,
                    "folder": args.folder,
                    **folder_counts(profile, folder),
                }
            )
        return {
            "cache_updated": datetime.fromtimestamp(cache_mtime)
            .astimezone()
            .isoformat(),
            "folders": results,
        }

    if args.limit < 1 or args.limit > 1000:
        raise ValueError("--limit must be between 1 and 1000")
    since_epoch = parse_since(args.since) if args.since else None
    messages = search_messages(
        accounts_and_folders,
        profile=profile,
        query=args.query,
        sender=args.sender,
        subject=args.subject,
        unread=args.unread,
        since_epoch=since_epoch,
        limit=args.limit,
    )
    return {
        "messages": [
            {
                "id": message.public_id,
                "account": _account_name(message.account_email, config.aliases),
                "folder": args.folder,
                "message_id": message.message_id,
                "subject": message.subject,
                "from": message.sender,
                "to": message.recipients,
                "date": message.date,
                "read": message.is_read,
            }
            for message in messages
        ]
    }


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
