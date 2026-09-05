from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_execute_requires_explicit_start(self) -> None:
        paths = bridge.ipc_paths(self.profile)
        with patch("tbmail.bridge._launch") as launch:
            for operation in ("sync", "mark-read", "mark-unread"):
                with self.assertRaises(bridge.BridgeError) as error:
                    bridge.execute(self.profile, ("thunderbird",), operation, {}, 1)
                self.assertEqual(error.exception.status, "absent_instance")
                self.assertIn("Run 'tbmail start' and retry.", str(error.exception))
        launch.assert_not_called()
        self.assertFalse(list(paths.requests.glob("*.json")))
        self.assertEqual(os.stat(paths.root).st_mode & 0o777, 0o700)

    def test_lock_contention_is_bounded_and_inode_persists(self) -> None:
        with bridge.profile_lock(self.profile):
            path = bridge.state_directory(self.profile) / "operation.lock"
            inode = path.stat().st_ino
            metadata = json.loads(path.read_text())
            self.assertEqual(metadata, bridge.process_identity(os.getpid()))
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; from tbmail.bridge import profile_lock; "
                    f"\nwith profile_lock(Path({str(self.profile)!r}), 0.1): pass",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Timed out waiting", result.stderr)
            self.assertEqual(json.loads(path.read_text()), metadata)
        with bridge.profile_lock(self.profile):
            self.assertEqual(path.stat().st_ino, inode)

    def test_start_does_not_claim_existing_unknown_or_gui(self) -> None:
        self.write_heartbeat()
        with (
            patch("tbmail.bridge.profile_active", return_value=True),
            patch("tbmail.bridge._launch") as launch,
        ):
            result = bridge.start(self.profile, ("thunderbird", "--headless"))
            self.assertFalse(result["managed"])
            self.assertEqual(result["status"], "already_running")
            self.assertEqual(bridge.stop(self.profile)["status"], "not_owned")
        launch.assert_not_called()
        self.assertFalse(
            (bridge.state_directory(self.profile) / "managed.json").exists()
        )

    def test_token_proves_ownership_not_headless_alone(self) -> None:
        self.write_heartbeat()
        paths = bridge.ipc_paths(self.profile)
        with bridge.profile_lock(self.profile):
            bridge._atomic_json(
                bridge.state_directory(self.profile) / "managed.json",
                {"token": "a" * 32},
            )
        heartbeat = bridge._read_json(paths.heartbeat)
        for token, headless, expected in (
            (None, True, False),
            ("b" * 32, True, False),
            ("a" * 32, False, False),
            ("a" * 32, True, True),
        ):
            bridge._atomic_json(
                paths.heartbeat,
                {**heartbeat, "startupToken": token, "headless": headless},
            )
            self.assertEqual(bridge._owned(self.profile, paths), expected)

    def test_flatpak_launch_passes_token_explicitly(self) -> None:
        paths = bridge.ipc_paths(self.profile)
        with patch("tbmail.bridge.os.posix_spawnp", return_value=123) as spawn:
            bridge._launch(
                ("flatpak", "run", "org.mozilla.thunderbird_esr", "--headless"),
                self.profile,
                paths,
                "a" * 32,
            )
        arguments = spawn.call_args.args[1]
        self.assertEqual(arguments[2], "--env=TBMAIL_STARTUP_TOKEN=" + "a" * 32)
        self.assertTrue(arguments[3].startswith("--env=TBMAIL_SAFETY_DEADLINE="))

    def test_stop_signals_drain_before_waiting_for_lock(self) -> None:
        self.write_heartbeat()
        paths = bridge.ipc_paths(self.profile)
        with bridge.profile_lock(self.profile):
            bridge._atomic_json(
                bridge.state_directory(self.profile) / "managed.json",
                {"token": "a" * 32},
            )
            results = []
            thread = threading.Thread(
                target=lambda: results.append(bridge.stop(self.profile, 2))
            )
            thread.start()
            deadline = time.monotonic() + 1
            while (
                not (paths.root / "drain.json").exists() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                bridge._read_json(paths.root / "drain.json")["startupToken"], "a" * 32
            )
            self.assertTrue(thread.is_alive())
        thread.join(3)
        self.assertEqual(results[0]["status"], "stopped")
        self.assertEqual(bridge.stop(self.profile)["status"], "stopped")

    def test_stop_timeout_retains_ownership_for_retry(self) -> None:
        self.write_heartbeat()
        with bridge.profile_lock(self.profile):
            bridge._atomic_json(
                bridge.state_directory(self.profile) / "managed.json",
                {"token": "a" * 32},
            )
        with (
            patch("tbmail.bridge.profile_active", return_value=True),
            patch("tbmail.bridge._owned", return_value=True),
        ):
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.stop(self.profile, 0.01)
        self.assertEqual(error.exception.status, "timeout")
        self.assertTrue(bridge._owner(self.profile))

    def test_freshness_invalid_future_missing_and_boundary(self) -> None:
        account = MailAccount(
            "a@example.com", "example.com", self.profile, "server1", "a"
        )
        paths = bridge.ipc_paths(self.profile)
        bridge._prepare_paths(paths)
        for raw, expected in (
            (None, True),
            (123, True),
            ("invalid", True),
            ("2026-01-01T00:00:00", True),
            (datetime.fromtimestamp(1001, UTC).isoformat(), True),
            (datetime.fromtimestamp(700, UTC).isoformat(), True),
            (datetime.fromtimestamp(701, UTC).isoformat(), False),
        ):
            with (
                self.subTest(raw=raw),
                patch("tbmail.bridge.time.time", return_value=1000),
            ):
                bridge._atomic_json(
                    paths.last_sync,
                    {"protocolVersion": 1, "accounts": {"server1": raw}},
                )
                self.assertEqual(
                    bridge.sync_freshness(self.profile, [account]).needs_sync, expected
                )

    def test_concurrent_starts_launch_only_once(self) -> None:
        active = threading.Event()

        def launch(command, profile, paths, token):
            bridge._atomic_json(
                paths.heartbeat,
                {
                    "protocolVersion": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "startupToken": token,
                    "headless": True,
                },
            )
            active.set()
            return os.getpid()

        def identify(*args):
            return "request", {
                "startupToken": bridge._owner(self.profile)["token"],
                "headless": True,
            }

        with (
            patch(
                "tbmail.bridge.profile_active", side_effect=lambda _: active.is_set()
            ),
            patch("tbmail.bridge._launch", side_effect=launch) as launched,
            patch("tbmail.bridge.execute_locked", side_effect=identify),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(
                pool.map(
                    lambda _: bridge.start(
                        self.profile, ("thunderbird", "--headless"), 2
                    ),
                    range(2),
                )
            )
        launched.assert_called_once()
        self.assertEqual(
            sorted(item["status"] for item in results), ["already_running", "started"]
        )
        self.assertTrue(all(item["managed"] for item in results))

    def test_stale_heartbeat_cannot_claim_a_replacement_gui(self) -> None:
        with (
            patch("tbmail.bridge.profile_active", return_value=True),
            patch("tbmail.bridge._owned", return_value=True),
            patch(
                "tbmail.bridge.execute_locked",
                return_value=("id", {"headless": False, "startupToken": None}),
            ),
        ):
            result = bridge.start(self.profile, ("thunderbird", "--headless"), 1)
        self.assertFalse(result["managed"])

    def test_launcher_identity_checks_boot_and_start_time(self) -> None:
        identity = bridge.process_identity(os.getpid())
        self.assertTrue(bridge._launcher_alive({"launcher": identity}))
        for field in ("boot_id", "starttime"):
            self.assertFalse(
                bridge._launcher_alive({"launcher": {**identity, field: "wrong"}})
            )

    def test_interrupted_pending_launch_is_not_overwritten(self) -> None:
        with bridge.profile_lock(self.profile):
            bridge._atomic_json(
                bridge.state_directory(self.profile) / "managed.json",
                {
                    "token": "a" * 32,
                    "launcher": None,
                    "pending_until": time.time() + 60,
                },
            )
        with (
            patch("tbmail.bridge._launch") as launch,
            self.assertRaises(bridge.BridgeError),
        ):
            bridge.start(self.profile, ("thunderbird", "--headless"), 0.1)
        launch.assert_not_called()
        self.assertEqual(bridge._owner(self.profile)["token"], "a" * 32)

    def test_stale_metadata_is_replaced_on_same_locked_inode(self) -> None:
        directory = bridge.state_directory(self.profile)
        directory.mkdir(parents=True)
        path = directory / "operation.lock"
        path.write_text("stale or corrupt metadata")
        inode = path.stat().st_ino
        with bridge.profile_lock(self.profile):
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(
                json.loads(path.read_text()), bridge.process_identity(os.getpid())
            )

    def test_nonfinite_timeouts_are_rejected(self) -> None:
        for timeout in (float("nan"), float("inf"), -1, 0):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(ValueError),
                bridge.profile_lock(self.profile, timeout),
            ):
                self.fail("invalid timeout acquired lock")

    def test_profile_release_uses_kernel_lock_not_file_existence(self) -> None:
        path = self.profile / ".parentlock"
        script = (
            "import fcntl,sys; f=open(sys.argv[1], 'w'); "
            "fcntl.lockf(f, fcntl.LOCK_EX); "
            "print('ready', flush=True); input(); f.close()"
        )
        with subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertTrue(bridge.profile_active(self.profile))
            process.communicate("release\n", timeout=3)
        self.assertTrue(path.exists())
        self.assertFalse(bridge.profile_active(self.profile))

    def test_execute_propagates_errors_and_cleans_request(self) -> None:
        self.write_heartbeat()
        paths = bridge.ipc_paths(self.profile)
        request_id = "a" * 32
        fixed_uuid = type("FixedUuid", (), {"hex": request_id})()
        for status in ("busy", "partial", "timeout", "invalid"):
            with self.subTest(status=status):
                bridge._atomic_json(
                    paths.responses / f"{request_id}.json",
                    {
                        "protocolVersion": 1,
                        "requestId": request_id,
                        "status": status,
                        "error": "failure",
                    },
                )
                with (
                    patch("tbmail.bridge.uuid.uuid4", return_value=fixed_uuid),
                    patch("tbmail.bridge.profile_active", return_value=True),
                    self.assertRaises(bridge.BridgeError) as error,
                ):
                    bridge.execute(self.profile, (), "sync", {}, 1)
                self.assertEqual(error.exception.status, status)
                self.assertFalse(list(paths.requests.glob("*.json")))

    def test_freshness_permission_error_is_an_error_not_stale(self) -> None:
        with (
            patch("tbmail.bridge._read_json", side_effect=PermissionError("denied")),
            self.assertRaises(PermissionError),
        ):
            bridge.sync_freshness(self.profile, [])

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
            patch("tbmail.bridge.profile_active", return_value=True),
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
