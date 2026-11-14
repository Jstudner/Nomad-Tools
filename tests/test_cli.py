"""Quick checks that the tools start, answer --help, and do a real job on a throwaway card.

    python3 -m unittest discover -s tests        (from the .venv for the map tools)
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def run(args, stdin="", timeout=120):
    return subprocess.run([sys.executable] + args, input=stdin, capture_output=True, text=True,
                          cwd=HERE, timeout=timeout)


def tools_with_main():
    return sorted(Path(f).name for f in glob.glob(str(HERE / "*.py"))
                  if "def main(" in Path(f).read_text(encoding="utf-8") and not f.endswith("nomad-tools.py"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.card = Path(self.tmp.name) / "card"
        for d in ("Movies", "Shows", "Music", "Books", "Gallery"):
            (self.card / d).mkdir(parents=True)
        self.src = Path(self.tmp.name) / "films"
        self.src.mkdir()
        (self.src / "Up (2009).mp4").write_bytes(b"\0" * 100_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_tool_answers_help(self):
        for tool in tools_with_main():
            with self.subTest(tool=tool):
                r = run([tool, "--help"])
                if r.returncode and "ModuleNotFoundError" in r.stderr:
                    self.skipTest(f"{tool} needs a library from requirements.txt (use the .venv)")
                self.assertEqual(r.returncode, 0, r.stderr[-500:])

    def test_text_a_user_sees_is_clean(self):
        # no em dashes, and Nomad is a name: never "the Nomad"
        for tool in tools_with_main() + ["nomad-tools.py"]:
            with self.subTest(tool=tool):
                text = run([tool, "--help"]).stdout
                self.assertNotIn("—", text)
                self.assertIsNone(re.search(r"\bthe Nomad\b", text), text)

    def test_the_menu_opens_and_quits(self):
        r = run(["nomad-tools.py"], stdin="0\n")
        self.assertIn("Nomad Card Tools", r.stdout)
        self.assertIn("Type where the card is", r.stdout)

    def test_add_media_copies_and_builds_the_index(self):
        r = run(["add_media.py", str(self.src), "--to", "Movies", "-o", str(self.card)], stdin="y\ny\n")
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-500:])
        self.assertTrue((self.card / "Movies" / "Up (2009).mp4").exists())
        self.assertTrue((self.card / ".system-index").is_dir())

    def test_check_the_card_changes_nothing(self):
        before = sorted(p.name for p in self.card.rglob("*"))
        r = run(["add_media.py", "--check", "-o", str(self.card)])
        self.assertEqual(r.returncode in (0, 1), True, r.stderr[-500:])
        self.assertEqual(before, sorted(p.name for p in self.card.rglob("*")))


if __name__ == "__main__":
    unittest.main()
