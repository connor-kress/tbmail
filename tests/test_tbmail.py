from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tbmail.cli import _output, build_parser, run
from tbmail.config import DEFAULT_THUNDERBIRD_COMMAND, default_config_path, load_config
from tbmail.index import (
    default_cache_path,
    find_message,
    resolve_message_for_write,
)
from tbmail.profile import MailAccount, discover_accounts


class TbmailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.profile = self.root / "profile"
        self.mail_directory = self.profile / "ImapMail" / "imap.example.com"
        self.mail_directory.mkdir(parents=True)
        self.config = self.root / "config.toml"
        self.config.write_text(
            '[accounts]\npersonal = "person@example.com"\n', encoding="utf-8"
        )
        (self.profile / "prefs.js").write_text(
            "\n".join(
                [
                    'user_pref("mail.accountmanager.accounts", "account1");',
                    'user_pref("mail.account.account1.server", "server1");',
                    'user_pref("mail.server.server1.type", "imap");',
                    'user_pref("mail.server.server1.userName", "person@example.com");',
                    'user_pref("mail.server.server1.hostname", "imap.example.com");',
                    'user_pref("mail.server.server1.directory-rel", '
                    '"[ProfD]ImapMail/imap.example.com");',
                ]
            ),
            encoding="utf-8",
        )
        self.inbox = self.mail_directory / "INBOX"
        self.inbox.with_suffix(".msf").touch()
        self.inbox.write_bytes(
            b"".join(
                [
                    b"From - Sat Aug 22 10:00:00 2026\n",
                    # IMAP mbox status headers can remain read while the
                    # Thunderbird message index records the current state.
                    b"X-Mozilla-Status: 0001\n",
                    b"Message-ID: <unread@example.com>\n",
                    # Match the next message's date to exercise deterministic
                    # tie-breaking during paginated searches.
                    b"Date: Sun, 23 Aug 2026 11:00:00 -0400\n",
                    b"From: Sender One <sender@example.com>\n",
                    b"To: Person <person@example.com>\n",
                    b"Subject: Pending example\n",
                    b"Content-Type: text/plain; charset=utf-8\n",
                    b"\n",
                    b"This is the unread body.\n",
                    b"From - Sun Aug 23 11:00:00 2026\n",
                    b"X-Mozilla-Status: 0001\n",
                    b"Message-ID: <read@example.com>\n",
                    b"Date: Sun, 23 Aug 2026 11:00:00 -0400\n",
                    b"From: Sender Two <other@example.com>\n",
                    b"To: Person <person@example.com>\n",
                    b"Subject: Read example\n",
                    b"Content-Type: text/plain; charset=utf-8\n",
                    b"\n",
                    b"This is the read body.\n",
                ]
            )
        )
        (self.profile / "folderCache.json").write_text(
            json.dumps(
                {
                    "/old/profile/ImapMail/imap.example.com/INBOX.msf": {
                        "totalMsgs": 2,
                        "totalUnreadMsgs": 1,
                        "serverTotal": 2,
                        "serverUnseen": 1,
                        "lastSyncTimeInSec": 1787497200,
                    }
                }
            ),
            encoding="utf-8",
        )
        with closing(
            sqlite3.connect(self.profile / "global-messages-db.sqlite")
        ) as connection:
            connection.executescript(
                """
                CREATE TABLE folderLocations (
                    id INTEGER PRIMARY KEY,
                    folderURI TEXT NOT NULL
                );
                CREATE TABLE messages (
                    folderID INTEGER NOT NULL,
                    headerMessageID TEXT,
                    jsonAttributes TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE attributeDefinitions (
                    id INTEGER PRIMARY KEY,
                    extensionName TEXT NOT NULL,
                    name TEXT NOT NULL
                );
                INSERT INTO attributeDefinitions(id, extensionName, name)
                VALUES (59, 'built-in', 'read');
                INSERT INTO folderLocations(id, folderURI)
                VALUES (
                    1,
                    'imap://person%40example.com@imap.example.com/INBOX'
                );
                INSERT INTO messages(folderID, headerMessageID, jsonAttributes)
                VALUES
                    (1, 'unread@example.com', '{"59": false}'),
                    (1, 'read@example.com', '{"59": true}');
                """
            )
        self.parser = build_parser()
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.environment.start()
        ipc = self.profile / "tbmail-ipc"
        ipc.mkdir()
        (ipc / "last-sync.json").write_text(
            json.dumps(
                {
                    "protocolVersion": 1,
                    "accounts": {
                        "server1": datetime.now(UTC).isoformat(),
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def parse(self, *arguments: str):
        return self.parser.parse_args(
            ["--config", str(self.config), "--profile", str(self.profile), *arguments]
        )

    def test_accounts_lists_alias(self) -> None:
        result = run(self.parse("accounts"))

        self.assertEqual(result.accounts[0].alias, "personal")
        self.assertEqual(result.accounts[0].email, "person@example.com")

    def test_default_config_uses_xdg_config_home(self) -> None:
        config_home = self.root / "xdg-config"
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(config_home), "TBMAIL_CONFIG": ""},
        ):
            self.assertEqual(
                default_config_path(), config_home / "tbmail" / "config.toml"
            )

    def test_config_uses_argument_array_for_thunderbird(self) -> None:
        self.assertEqual(DEFAULT_THUNDERBIRD_COMMAND, ("thunderbird", "--headless"))
        self.assertEqual(
            load_config(self.config).thunderbird_command, DEFAULT_THUNDERBIRD_COMMAND
        )
        self.config.write_text(
            '[accounts]\npersonal = "person@example.com"\n'
            '[thunderbird]\ncommand = ["thunderbird", "--headless"]\n',
            encoding="utf-8",
        )
        self.assertEqual(
            load_config(self.config).thunderbird_command,
            ("thunderbird", "--headless"),
        )

    def test_config_rejects_visible_or_profile_selecting_commands(self) -> None:
        for command in (
            '["thunderbird"]',
            '["thunderbird", "--headless", "--profile", "/other"]',
        ):
            with self.subTest(command=command):
                self.config.write_text(
                    '[accounts]\npersonal = "person@example.com"\n'
                    f"[thunderbird]\ncommand = {command}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "thunderbird.command"):
                    load_config(self.config)

    def test_status_supports_alias_and_raw_address(self) -> None:
        alias_result = run(self.parse("status", "-a", "personal"))
        raw_result = run(self.parse("status", "--account-raw", "person@example.com"))

        self.assertEqual(alias_result.folders[0].unread, 1)
        self.assertEqual(raw_result.folders[0].total, 2)

    def test_search_and_show_downloaded_message(self) -> None:
        search_result = run(
            self.parse("search", "-a", "personal", "--unread", "Sender")
        )

        self.assertEqual(len(search_result.messages), 1)
        summary = search_result.messages[0]
        self.assertEqual(summary.subject, "Pending example")
        self.assertFalse(summary.is_read)

        show_result = run(self.parse("show", summary.public_id))
        self.assertEqual(show_result.account, "personal")
        self.assertEqual(show_result.body, "This is the unread body.\n")

    def test_search_result_keeps_cli_json_field_names(self) -> None:
        result = run(self.parse("search", "--limit", "1"))
        output = io.StringIO()

        with patch("sys.stdout", output):
            _output(result)

        message = json.loads(output.getvalue())["messages"][0]
        self.assertIn("id", message)
        self.assertIn("from", message)
        self.assertIn("to", message)
        self.assertIn("read", message)

    def test_search_filters_subject_and_date(self) -> None:
        result = run(
            self.parse(
                "search",
                "--account-raw",
                "person@example.com",
                "--subject",
                "Read example",
                "--since",
                "2026-08-23",
            )
        )

        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].subject, "Read example")
        self.assertTrue(result.messages[0].is_read)

    def test_search_uses_mbox_read_state_before_gloda_indexes_account(self) -> None:
        contents = self.inbox.read_bytes()
        self.inbox.write_bytes(
            contents.replace(b"X-Mozilla-Status: 0001", b"X-Mozilla-Status: 0000", 1)
        )
        with closing(
            sqlite3.connect(self.profile / "global-messages-db.sqlite")
        ) as connection:
            connection.execute("DELETE FROM messages")

        result = run(self.parse("search", "-a", "personal", "--unread"))

        self.assertEqual(
            [message.subject for message in result.messages],
            ["Pending example"],
        )

    def test_search_indexes_new_account_reusing_cached_mailbox(self) -> None:
        run(self.parse("search", "-a", "personal"))
        self.config.write_text(
            '[accounts]\nnew = "new@example.com"\n', encoding="utf-8"
        )
        prefs = (self.profile / "prefs.js").read_text(encoding="utf-8")
        (self.profile / "prefs.js").write_text(
            prefs.replace("person@example.com", "new@example.com"), encoding="utf-8"
        )

        result = run(self.parse("search", "-a", "new"))

        self.assertEqual(
            [message.subject for message in result.messages],
            ["Read example", "Pending example"],
        )
        self.assertTrue(all(message.account == "new" for message in result.messages))

    def test_search_supports_deterministic_index_offset(self) -> None:
        all_messages = run(self.parse("search"))
        first_page = run(self.parse("search", "--limit", "1"))
        second_page = run(self.parse("search", "--limit", "1", "--offset", "1"))

        self.assertEqual(
            [message.subject for message in all_messages.messages],
            ["Read example", "Pending example"],
        )
        self.assertEqual(first_page.messages[0].subject, "Read example")
        self.assertEqual(second_page.messages[0].subject, "Pending example")

    def test_search_rejects_negative_offset(self) -> None:
        with self.assertRaisesRegex(ValueError, "--offset must be nonnegative"):
            run(self.parse("search", "--offset", "-1"))

    def test_search_count_uses_search_filters(self) -> None:
        result = run(
            self.parse(
                "search",
                "--unread",
                "--subject",
                "Pending example",
                "--count",
            )
        )

        self.assertEqual(result.count, 1)

    def test_search_count_rejects_pagination(self) -> None:
        for pagination in (("--limit", "1"), ("--offset", "0")):
            with self.subTest(pagination=pagination):
                with self.assertRaisesRegex(
                    ValueError,
                    "--count cannot be combined with --limit or --offset",
                ):
                    run(self.parse("search", "--count", *pagination))

    def test_mark_read_updates_cache_and_survives_stale_gloda(self) -> None:
        search_result = run(self.parse("search", "--unread"))
        message = search_result.messages[0]
        with patch(
            "tbmail.cli.execute",
            return_value=(
                "request-1",
                {"matchedBy": "storeToken", "folderUri": "imap://example/INBOX"},
            ),
        ) as execute:
            result = run(self.parse("mark-read", message.public_id))

        self.assertTrue(result.is_read)
        self.assertEqual(execute.call_args.args[2], "mark-read")
        self.assertEqual(
            execute.call_args.args[3]["folderPath"],
            "ImapMail/imap.example.com/INBOX",
        )
        self.assertNotIn("folderUri", execute.call_args.args[3])
        self.assertTrue(find_message(message.public_id).is_read)
        repeated = run(self.parse("search", "--unread"))
        self.assertEqual(repeated.messages, [])

    def test_write_resolution_recovers_after_compaction(self) -> None:
        search_result = run(self.parse("search"))
        original = find_message(search_result.messages[0].public_id)
        self.inbox.write_bytes(b"From compaction padding\n" + self.inbox.read_bytes())
        accounts = discover_accounts(self.profile, load_config(self.config))

        current, account = resolve_message_for_write(
            original, accounts, self.profile, default_cache_path()
        )

        self.assertEqual(account.server_id, "server1")
        self.assertEqual(current.message_id, original.message_id)
        self.assertNotEqual(current.public_id, original.public_id)

    def test_sync_uses_selected_server_keys_and_timeout(self) -> None:
        response = {
            "accounts": [
                {
                    "serverKey": "server1",
                    "status": "success",
                    "folders": [],
                }
            ]
        }
        with patch(
            "tbmail.cli.execute", return_value=("request-2", response)
        ) as execute:
            result = run(
                self.parse("sync", "-a", "personal", "--force", "--timeout", "12")
            )

        self.assertEqual(result.accounts[0].account, "personal")
        self.assertEqual(execute.call_args.args[3], {"serverKeys": ["server1"]})
        self.assertEqual(execute.call_args.args[4], 12)

    def test_stale_warning_is_json_on_stderr(self) -> None:
        (self.profile / "tbmail-ipc" / "last-sync.json").unlink()
        errors = io.StringIO()

        with patch("sys.stderr", errors):
            result = run(self.parse("status"))

        self.assertEqual(result.folders[0].account, "personal")
        self.assertEqual(
            json.loads(errors.getvalue()),
            {"warning": "Local mail for personal has never been synchronized"},
        )

    def test_accounts_does_not_warn_when_sync_state_is_missing(self) -> None:
        (self.profile / "tbmail-ipc" / "last-sync.json").unlink()
        errors = io.StringIO()

        with patch("sys.stderr", errors):
            run(self.parse("accounts"))

        self.assertEqual(errors.getvalue(), "")

    def test_sync_check_and_fresh_skip_do_not_contact_thunderbird(self) -> None:
        with patch("tbmail.cli.execute") as execute:
            for arguments in (("sync", "--check"), ("sync",)):
                result = run(self.parse(*arguments))
                self.assertFalse(result.needs_sync)
            (self.profile / "tbmail-ipc/last-sync.json").unlink()
            with patch("tbmail.cli.profile_lock") as lock:
                result = run(self.parse("sync", "--check", "-a", "personal"))
                self.assertTrue(result.needs_sync)
                lock.assert_not_called()
        execute.assert_not_called()

    def test_check_and_force_are_exclusive(self) -> None:
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.parse("sync", "--check", "--force")

    def test_mixed_sync_only_schedules_stale_accounts(self) -> None:
        accounts = discover_accounts(self.profile, load_config(self.config))
        accounts.append(
            MailAccount(
                "other@example.com",
                "example.com",
                self.mail_directory,
                "server2",
                "other",
            )
        )
        response = {
            "accounts": [{"serverKey": "server2", "status": "success", "folders": []}]
        }
        with (
            patch("tbmail.cli.discover_accounts", return_value=accounts),
            patch("tbmail.cli.execute", return_value=("request", response)) as execute,
        ):
            result = run(
                self.parse(
                    "sync",
                    "--account-raw",
                    "person@example.com",
                    "--account-raw",
                    "other@example.com",
                )
            )
        self.assertEqual(execute.call_args.args[3], {"serverKeys": ["server2"]})
        self.assertEqual(
            [account.status for account in result.accounts],
            ["success", "skipped_fresh"],
        )

    def test_local_reads_do_not_acquire_lifecycle_lock(self) -> None:
        with patch(
            "tbmail.cli.profile_lock", side_effect=AssertionError("local read locked")
        ):
            run(self.parse("status"))
            message = run(self.parse("search")).messages[0]
            run(self.parse("show", message.public_id))

    def test_search_defaults_to_all_downloaded_folders(self) -> None:
        archive = self.mail_directory / "Archive"
        archive.write_bytes(self.inbox.read_bytes())
        archive.with_suffix(".msf").touch()
        self.assertEqual(run(self.parse("search", "--count")).count, 4)
        self.assertEqual(
            run(self.parse("search", "--folder", "inbox", "--count")).count, 2
        )


if __name__ == "__main__":
    unittest.main()
