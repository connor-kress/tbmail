from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "setup-thunderbird-addon"
LOADER = importlib.machinery.SourceFileLoader("setup_thunderbird_addon", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
setup = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(setup)


class SetupThunderbirdAddonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_discovers_install_default_profile(self) -> None:
        profile = self.root / "Profiles" / "default"
        profile.mkdir(parents=True)
        (profile / "prefs.js").write_text("", encoding="utf-8")
        (self.root / "profiles.ini").write_text(
            "[InstallABC]\nDefault=Profiles/default\nLocked=1\n",
            encoding="utf-8",
        )

        self.assertEqual(setup.discover_profile(None, self.root), profile)

    def test_refuses_locked_profile(self) -> None:
        profile = self.root / "profile"
        profile.mkdir()
        (profile / ".parentlock").symlink_to("12345")

        with self.assertRaises(setup.SetupError):
            setup.assert_profile_stopped(profile)

    def test_profile_guard_keeps_thunderbirds_persistent_lock_file(self) -> None:
        profile = self.root / "profile"
        profile.mkdir()

        with setup.profile_guard(profile):
            self.assertTrue((profile / ".parentlock").exists())

        self.assertTrue((profile / ".parentlock").exists())

    def test_packaging_has_only_source_with_manifest_at_root(self) -> None:
        xpi = self.root / "addon.xpi"

        setup.package_xpi(xpi)

        with zipfile.ZipFile(xpi) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("manifest.json"))
        self.assertIn("api/TbmailBridge/implementation.js", names)
        self.assertNotIn("thunderbird-addon/manifest.json", names)
        self.assertEqual(
            manifest["browser_specific_settings"]["gecko"]["strict_max_version"],
            "140.*",
        )

    def test_install_is_atomic_and_backs_up_outside_profile(self) -> None:
        profile = self.root / "profile"
        extensions = profile / "extensions"
        extensions.mkdir(parents=True)
        destination = extensions / f"{setup.ADDON_ID}.xpi"
        destination.write_bytes(b"old")
        source = self.root / "new.xpi"
        source.write_bytes(b"new")
        old_tmp = setup.TMP
        setup.TMP = self.root / "repo-tmp"
        try:
            installed, backup = setup.install_xpi(profile, source)
        finally:
            setup.TMP = old_tmp

        self.assertEqual(installed.read_bytes(), b"new")
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertEqual(backup.read_bytes(), b"old")
        self.assertFalse(backup.is_relative_to(profile))
        self.assertEqual(os.stat(installed).st_mode & 0o777, 0o600)

    def test_sideload_preferences_are_idempotent(self) -> None:
        profile = self.root / "profile"
        profile.mkdir()
        prefs = profile / "prefs.js"
        prefs.write_text(
            'user_pref("extensions.autoDisableScopes", 15);\n'
            'user_pref("extensions.startupScanScopes", 4);\n',
            encoding="utf-8",
        )

        setup.allow_profile_sideload(profile)
        first = prefs.read_text(encoding="utf-8")
        setup.allow_profile_sideload(profile)

        self.assertEqual(prefs.read_text(encoding="utf-8"), first)
        self.assertIn('user_pref("extensions.autoDisableScopes", 14);', first)
        self.assertIn('user_pref("extensions.startupScanScopes", 5);', first)

    def test_rejects_profile_outside_flatpak_app_data(self) -> None:
        with self.assertRaises(setup.SetupError):
            setup.sandbox_profile_path(self.root / "profile")

    def test_reads_version_from_supported_flatpak_list_output(self) -> None:
        completed = Mock(
            returncode=0,
            stdout="other.app\t1.0\norg.mozilla.thunderbird_esr\t140.15.0esr\n",
        )
        with (
            patch.object(setup.shutil, "which", return_value="/usr/bin/flatpak"),
            patch.object(setup.subprocess, "run", return_value=completed) as run,
        ):
            version = setup.flatpak_version()

        self.assertEqual(version, "140.15.0esr")
        self.assertEqual(
            run.call_args.args[0],
            ["flatpak", "list", "--app", "--columns=application,version"],
        )


if __name__ == "__main__":
    unittest.main()
