from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from .profile import MailAccount

SCHEMA_VERSION = 2


def default_cache_path() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return cache_home / "tbmail" / "index.sqlite3"


@dataclass(frozen=True)
class IndexedMessage:
    public_id: str
    account_email: str
    folder_path: Path
    offset: int
    size: int
    message_id: str | None
    subject: str
    sender: str
    recipients: str
    date: str | None
    date_epoch: int | None
    is_read: bool | None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mailboxes (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            public_id TEXT PRIMARY KEY,
            account_email TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            offset INTEGER NOT NULL,
            size INTEGER NOT NULL,
            message_id TEXT,
            subject TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipients TEXT NOT NULL,
            date TEXT,
            date_epoch INTEGER,
            is_read INTEGER
        );
        CREATE INDEX IF NOT EXISTS messages_location
            ON messages(account_email, folder_path, date_epoch DESC);
        CREATE INDEX IF NOT EXISTS messages_message_id
            ON messages(account_email, folder_path, message_id);
        """
    )
    version = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if version and int(version["value"]) != SCHEMA_VERSION:
        connection.executescript("DELETE FROM messages; DELETE FROM mailboxes;")
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def _header_text(message: Message, name: str) -> str:
    value = message.get(name)
    return str(value) if value is not None else ""


def _date_epoch(value: str) -> int | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _public_id(
    account: MailAccount,
    folder: Path,
    message_id: str,
    date: str,
    subject: str,
    sender: str,
    offset: int,
) -> str:
    identity = "\0".join(
        [
            account.email.casefold(),
            str(folder),
            message_id,
            date,
            subject,
            sender,
            str(offset),
        ]
    ).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(identity).hexdigest()[:24]


def _parse_headers(
    account: MailAccount, folder: Path, header_bytes: bytes
) -> dict[str, object]:
    message = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
    subject = _header_text(message, "Subject")
    sender = _header_text(message, "From")
    recipients = ", ".join(
        value
        for value in (
            _header_text(message, "To"),
            _header_text(message, "Cc"),
            _header_text(message, "Bcc"),
        )
        if value
    )
    date = _header_text(message, "Date")
    message_id = _header_text(message, "Message-ID").strip("<>")
    status = _header_text(message, "X-Mozilla-Status")
    try:
        is_read: bool | None = bool(int(status, 16) & 0x0001) if status else None
    except ValueError:
        is_read = None
    return {
        "message_id": message_id or None,
        "subject": subject,
        "sender": sender,
        "recipients": recipients,
        "date": date or None,
        "date_epoch": _date_epoch(date),
        "is_read": None if is_read is None else int(is_read),
    }


def _scan_mbox(account: MailAccount, folder: Path) -> list[tuple[object, ...]]:
    records: list[tuple[object, ...]] = []
    with folder.open("rb") as mailbox:
        message_start: int | None = None
        header_lines: list[bytes] = []
        reading_headers = False

        while True:
            line_start = mailbox.tell()
            line = mailbox.readline()
            if not line:
                end = mailbox.tell()
                if message_start is not None:
                    headers = _parse_headers(account, folder, b"".join(header_lines))
                    records.append(
                        _record(account, folder, message_start, end, headers)
                    )
                break

            if line.startswith(b"From "):
                if message_start is not None:
                    headers = _parse_headers(account, folder, b"".join(header_lines))
                    records.append(
                        _record(account, folder, message_start, line_start, headers)
                    )
                message_start = line_start
                header_lines = []
                reading_headers = True
                continue

            if reading_headers:
                if line in {b"\n", b"\r\n"}:
                    reading_headers = False
                else:
                    header_lines.append(line)

    return records


def _record(
    account: MailAccount,
    folder: Path,
    start: int,
    end: int,
    headers: dict[str, object],
) -> tuple[object, ...]:
    return (
        _public_id(
            account,
            folder,
            str(headers["message_id"] or ""),
            str(headers["date"] or ""),
            str(headers["subject"]),
            str(headers["sender"]),
            start,
        ),
        account.email.casefold(),
        str(folder),
        start,
        end - start,
        headers["message_id"],
        headers["subject"],
        headers["sender"],
        headers["recipients"],
        headers["date"],
        headers["date_epoch"],
        headers["is_read"],
    )


def ensure_indexed(
    account: MailAccount, folder: Path, cache_path: Path | None = None
) -> None:
    cache = cache_path or default_cache_path()
    stat = folder.stat()
    with closing(_connect(cache)) as connection:
        current = connection.execute(
            "SELECT size, mtime_ns FROM mailboxes WHERE path = ?", (str(folder),)
        ).fetchone()
        if (
            current
            and current["size"] == stat.st_size
            and current["mtime_ns"] == stat.st_mtime_ns
        ):
            return

        records = _scan_mbox(account, folder)
        scanned_stat = folder.stat()
        if (
            scanned_stat.st_size != stat.st_size
            or scanned_stat.st_mtime_ns != stat.st_mtime_ns
        ):
            raise OSError(f"Mailbox changed while it was being indexed: {folder}")
        with connection:
            connection.execute(
                "DELETE FROM messages WHERE folder_path = ?", (str(folder),)
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO messages(
                    public_id, account_email, folder_path, offset, size,
                    message_id, subject, sender, recipients, date, date_epoch,
                    is_read
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            connection.execute(
                "INSERT OR REPLACE INTO mailboxes(path, size, mtime_ns) "
                "VALUES(?, ?, ?)",
                (str(folder), stat.st_size, stat.st_mtime_ns),
            )


def sync_read_state(
    profile: Path,
    account: MailAccount,
    folder: Path,
    cache_path: Path | None = None,
) -> None:
    gloda_path = profile / "global-messages-db.sqlite"
    if not gloda_path.is_file():
        return

    relative_folder = folder.relative_to(account.directory).as_posix()
    folder_parts = [part.removesuffix(".sbd") for part in relative_folder.split("/")]
    encoded_folder = "/".join(quote(part, safe="[]") for part in folder_parts)
    folder_uri = (
        f"imap://{quote(account.email, safe='')}@{account.hostname}/{encoded_folder}"
    )

    try:
        with closing(
            sqlite3.connect(f"{gloda_path.as_uri()}?mode=ro", uri=True)
        ) as gloda:
            attribute = gloda.execute(
                """
                SELECT id FROM attributeDefinitions
                WHERE extensionName = 'built-in' AND name = 'read'
                """
            ).fetchone()
            if not attribute:
                return
            read_key = f'$."{attribute[0]}"'
            rows = gloda.execute(
                """
                SELECT m.headerMessageID,
                       json_extract(m.jsonAttributes, ?) AS is_read
                FROM messages AS m
                JOIN folderLocations AS f ON f.id = m.folderID
                WHERE f.folderURI = ? AND m.deleted = 0
                """,
                (read_key, folder_uri),
            ).fetchall()
    except sqlite3.OperationalError:
        return

    updates = [
        (int(bool(is_read)), account.email.casefold(), str(folder), message_id)
        for message_id, is_read in rows
        if message_id and is_read is not None
    ]
    cache = cache_path or default_cache_path()
    with closing(_connect(cache)) as connection:
        with connection:
            connection.execute(
                "UPDATE messages SET is_read = NULL "
                "WHERE account_email = ? AND folder_path = ?",
                (account.email.casefold(), str(folder)),
            )
            connection.executemany(
                """
                UPDATE messages SET is_read = ?
                WHERE account_email = ? AND folder_path = ? AND message_id = ?
                """,
                updates,
            )


def search_messages(
    accounts_and_folders: list[tuple[MailAccount, Path]],
    profile: Path,
    query: str | None,
    sender: str | None,
    subject: str | None,
    unread: bool,
    since_epoch: int | None,
    limit: int | None,
    offset: int,
    cache_path: Path | None = None,
) -> list[IndexedMessage]:
    cache = cache_path or default_cache_path()
    for account, folder in accounts_and_folders:
        ensure_indexed(account, folder, cache)
        sync_read_state(profile, account, folder, cache)

    clauses: list[str] = []
    parameters: list[object] = []
    locations: list[str] = []
    for account, folder in accounts_and_folders:
        locations.append("(account_email = ? AND folder_path = ?)")
        parameters.extend([account.email.casefold(), str(folder)])
    clauses.append("(" + " OR ".join(locations) + ")")
    if query:
        clauses.append(
            "(subject LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' "
            "OR recipients LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{_escape_like(query)}%"
        parameters.extend([pattern, pattern, pattern])
    if sender:
        clauses.append("sender LIKE ? ESCAPE '\\'")
        parameters.append(f"%{_escape_like(sender)}%")
    if subject:
        clauses.append("subject LIKE ? ESCAPE '\\'")
        parameters.append(f"%{_escape_like(subject)}%")
    if unread:
        clauses.append("is_read = 0")
    if since_epoch is not None:
        clauses.append("date_epoch >= ?")
        parameters.append(since_epoch)
    parameters.extend([limit if limit is not None else -1, offset])

    with closing(_connect(cache)) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM messages
            WHERE {" AND ".join(clauses)}
            ORDER BY COALESCE(date_epoch, 0) DESC,
                     account_email ASC,
                     folder_path ASC,
                     offset DESC,
                     public_id ASC
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
    return [_row_to_message(row) for row in rows]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_message(row: sqlite3.Row) -> IndexedMessage:
    return IndexedMessage(
        public_id=row["public_id"],
        account_email=row["account_email"],
        folder_path=Path(row["folder_path"]),
        offset=row["offset"],
        size=row["size"],
        message_id=row["message_id"],
        subject=row["subject"],
        sender=row["sender"],
        recipients=row["recipients"],
        date=row["date"],
        date_epoch=row["date_epoch"],
        is_read=None if row["is_read"] is None else bool(row["is_read"]),
    )


def find_message(public_id: str, cache_path: Path | None = None) -> IndexedMessage:
    cache = cache_path or default_cache_path()
    with closing(_connect(cache)) as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE public_id = ?", (public_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"Message not found in the local index: {public_id}")
    return _row_to_message(row)


def refresh_message(
    message: IndexedMessage,
    accounts: list[MailAccount],
    profile: Path,
    cache_path: Path | None = None,
) -> IndexedMessage:
    account = next(
        (
            candidate
            for candidate in accounts
            if candidate.email.casefold() == message.account_email
        ),
        None,
    )
    if not account:
        raise ValueError(
            f"Message account is no longer in Thunderbird: {message.account_email}"
        )
    try:
        message.folder_path.resolve().relative_to(account.directory.resolve())
    except ValueError as exc:
        raise ValueError("Message does not belong to the selected profile") from exc
    ensure_indexed(account, message.folder_path, cache_path)
    sync_read_state(profile, account, message.folder_path, cache_path)
    return find_message(message.public_id, cache_path)


def read_message(message: IndexedMessage, max_body_chars: int) -> dict[str, object]:
    with message.folder_path.open("rb") as mailbox:
        mailbox.seek(message.offset)
        raw_message = mailbox.read(message.size)
    parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
    parsed_message_id = _header_text(parsed, "Message-ID").strip("<>") or None
    if parsed_message_id != message.message_id:
        raise OSError(
            "Mailbox changed before the message could be read; run search again"
        )

    body = ""
    body_type: str | None = None
    attachments: list[dict[str, object]] = []
    parts = parsed.walk() if parsed.is_multipart() else [parsed]
    html_body = ""
    for part in parts:
        if part.is_multipart():
            continue
        content_disposition = part.get_content_disposition()
        filename = part.get_filename()
        if content_disposition == "attachment" or filename:
            payload = part.get_payload(decode=True)
            attachments.append(
                {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "size": len(payload) if isinstance(payload, bytes) else None,
                }
            )
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            continue
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain" and not body:
            body = content
            body_type = "text/plain"
        elif part.get_content_type() == "text/html" and not html_body:
            html_body = content

    if not body and html_body:
        extractor = _HTMLTextExtractor()
        extractor.feed(html_body)
        body = extractor.text()
        body_type = "text/html"

    truncated = len(body) > max_body_chars
    if truncated:
        body = body[:max_body_chars]
    return {
        "id": message.public_id,
        "account": message.account_email,
        "folder": message.folder_path.name,
        "message_id": message.message_id,
        "subject": message.subject,
        "from": message.sender,
        "to": message.recipients,
        "date": message.date,
        "read": message.is_read,
        "body_type": body_type,
        "body": body,
        "body_truncated": truncated,
        "attachments": attachments,
    }


def parse_since(value: str) -> int:
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").astimezone().timestamp())
    except ValueError as exc:
        raise ValueError("--since must use YYYY-MM-DD") from exc
