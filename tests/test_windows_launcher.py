import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


@unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def test_reads_relative_path_from_outside_checkout(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            sample = Path(directory) / "my transcript.jsonl"
            sample.write_bytes((root / "tests/fixtures/claude/resumed.jsonl").read_bytes())
            result = subprocess.run(
                [root / "codex-human-transcript.bat", "--file",
                 sample.name, "--once", "--no-ledger"],
                cwd=directory, shell=True, capture_output=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("text-0", result.stdout)


if __name__ == "__main__":
    unittest.main()
