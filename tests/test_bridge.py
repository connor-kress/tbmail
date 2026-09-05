from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tbmail import bridge
from tbmail.profile import MailAccount


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.root / "state")},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def write_heartbeat(self) -> None:
        paths = bridge.ipc_paths(self.profile)
        bridge._prepare_paths(paths)
        bridge._atomic_json(
            paths.heartbeat,
            {
                "protocolVersion": bridge.PROTOCOL_VERSION,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    def test_execute_publishes_request_before_launch(self) -> None:
        paths = bridge.ipc_paths(self.profile)

        def launch(command, profile, actual_paths):
            requests = list(actual_paths.requests.glob("*.json"))
            self.assertEqual(len(requests), 1)
            request = json.loads(requests[0].read_text(encoding="utf-8"))
            bridge._atomic_json(
                actual_paths.responses / f"{request['requestId']}.json",
                {
                    "protocolVersion": bridge.PROTOCOL_VERSION,
                    "requestId": request["requestId"],
                    "status": "success",
                },
            )
            return 123

        with patch("tbmail.bridge._launch", side_effect=launch) as mocked_launch:
            request_id, response = bridge.execute(
                self.profile, ("thunderbird", "--headless"), "sync", {}, 1
            )

        mocked_launch.assert_called_once()
        self.assertEqual(response["requestId"], request_id)
        self.assertFalse(list(paths.requests.glob("*.json")))
        self.assertEqual(os.stat(paths.root).st_mode & 0o777, 0o700)

    def test_fresh_heartbeat_avoids_launch(self) -> None:
        self.write_heartbeat()
        paths = bridge.ipc_paths(self.profile)
        request_id = "a" * 32
        bridge._atomic_json(
            paths.responses / f"{request_id}.json",
            {
                "protocolVersion": bridge.PROTOCOL_VERSION,
                "requestId": request_id,
                "status": "success",
            },
        )
        fixed_uuid = type("FixedUuid", (), {"hex": request_id})()

        with (
            patch("tbmail.bridge.uuid.uuid4", return_value=fixed_uuid),
            patch("tbmail.bridge._launch") as mocked_launch,
        ):
            bridge.execute(self.profile, ("thunderbird",), "sync", {}, 1)

        mocked_launch.assert_not_called()

    def test_stale_sync_warnings_are_per_server(self) -> None:
        account = MailAccount(
            email="person@example.com",
            hostname="imap.example.com",
            directory=self.profile / "ImapMail/example",
            server_id="server1",
            alias="personal",
        )
        self.assertEqual(
            bridge.stale_sync_warnings(self.profile, [account]),
            ["Local mail for personal has never been synchronized"],
        )
        paths = bridge.ipc_paths(self.profile)
        bridge._prepare_paths(paths)
        bridge._atomic_json(
            paths.last_sync,
            {
                "protocolVersion": bridge.PROTOCOL_VERSION,
                "accounts": {
                    "server1": datetime.now(UTC).isoformat(),
                },
            },
        )
        self.assertEqual(bridge.stale_sync_warnings(self.profile, [account]), [])

    def test_future_sync_timestamp_is_not_fresh(self) -> None:
        account = MailAccount(
            email="person@example.com",
            hostname="imap.example.com",
            directory=self.profile / "ImapMail/example",
            server_id="server1",
            alias="personal",
        )
        paths = bridge.ipc_paths(self.profile)
        bridge._prepare_paths(paths)
        bridge._atomic_json(
            paths.last_sync,
            {
                "protocolVersion": bridge.PROTOCOL_VERSION,
                "accounts": {"server1": "2999-01-01T00:00:00+00:00"},
            },
        )

        self.assertEqual(
            bridge.stale_sync_warnings(self.profile, [account]),
            ["Local mail for personal has never been synchronized"],
        )


if __name__ == "__main__":
    unittest.main()
