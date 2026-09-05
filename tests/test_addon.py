import subprocess
import unittest
from pathlib import Path


class AddonTestCase(unittest.TestCase):
    def test_javascript_lifecycle(self):
        result = subprocess.run(
            ["node", "--test", "tests/addon.test.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
