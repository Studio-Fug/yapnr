"""Deterministic zips, the privacy pass and relative command lines."""

from __future__ import annotations

import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from yapnr.fab import bundle


class ZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_two_builds_give_identical_bytes(self):
        (self.dir / "a.gtl").write_text("A\n")
        members = [("z.txt", b"last"), ("a.gtl", self.dir / "a.gtl"), ("m/b.drl", b"drill")]
        first = bundle.write_zip(self.dir / "one.zip", members)
        time.sleep(1.1)  # a different wall-clock second must not matter
        (self.dir / "a.gtl").touch()
        second = bundle.write_zip(self.dir / "two.zip", list(reversed(members)))
        self.assertEqual((self.dir / "one.zip").read_bytes(), (self.dir / "two.zip").read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])

    def test_entries_are_sorted_stored_dated_1980_and_mode_0644(self):
        bundle.write_zip(self.dir / "z.zip", [("b", b"2"), ("a", b"1")])
        with zipfile.ZipFile(self.dir / "z.zip") as zf:
            infos = zf.infolist()
            self.assertEqual([i.filename for i in infos], ["a", "b"])
            for i in infos:
                self.assertEqual(i.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(i.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(i.external_attr >> 16, 0o100644)
                self.assertEqual(i.create_system, 3)
                self.assertEqual(i.extra, b"")
            self.assertEqual(zf.comment, b"")

    def test_member_hashes(self):
        info = bundle.write_zip(self.dir / "z.zip", [("a", b"1")])
        self.assertEqual(
            info["members"], [{"name": "a", "sha256": bundle.sha256_bytes(b"1"), "bytes": 1}]
        )
        self.assertEqual(info["sha256"], bundle.sha256_file(self.dir / "z.zip"))

    def test_duplicate_members_are_refused(self):
        with self.assertRaises(ValueError):
            bundle.write_zip(self.dir / "z.zip", [("a", b"1"), ("a", b"2")])


class PrivacyTest(unittest.TestCase):
    def test_paths_homes_and_addresses_are_found(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "README.md"
            p.write_text(
                "board at /" + "Users/someone/x.kicad_pcb\nsee ~" + "/boards\n"
                "mail someone" + "@" + "example.org or a" + "@" + "corp.io\n"
                "fine: docs/fab.md, 1.5 mm, user" + "@" + "users.noreply.github.com\n"
            )
            kinds = [kind for _, kind, _ in bundle.privacy_findings([p])]
        self.assertEqual(kinds, ["absolute path", "home directory", "e-mail address"])

    def test_relative_command_line(self):
        base = Path("/tmp/work")
        argv = [
            "yapnr",
            "fab",
            "build",
            "/tmp/work/boards/b.kicad_pcb",
            "--out=/elsewhere/out",
            "--vendor",
            "oshpark",
        ]
        self.assertEqual(
            bundle.relative_argv(argv, base),
            ["yapnr", "fab", "build", "boards/b.kicad_pcb", "--out=out", "--vendor", "oshpark"],
        )


if __name__ == "__main__":
    unittest.main()
