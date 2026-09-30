"""Tests for tools/release/notes_header.py: the install header of release notes."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest

from tools.release import notes_header

DIGEST = "sha256:" + "ab" * 32


class NotesHeaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = os.path.join(self._tmp.name, "yapnr-v0.3.1.tar.gz")
        with open(self.archive, "wb") as handle:
            handle.write(b"not really a tarball")
        self.wheel = os.path.join(self._tmp.name, "yapnr-0.3.1-py3-none-any.whl")

    def tearDown(self):
        self._tmp.cleanup()

    def _render(self, tag="v0.3.1", pep440="0.3.1", **kwargs):
        return notes_header.render(
            tag=tag,
            pep440=pep440,
            digest=DIGEST,
            kicad_version="10.0.6",
            archive=self.archive,
            wheel=self.wheel,
            **kwargs,
        )

    def test_integrity_is_sri(self):
        expected = base64.b64encode(hashlib.sha256(b"not really a tarball").digest()).decode()
        self.assertEqual(notes_header.sri_sha256(self.archive), "sha256-" + expected)

    def test_stable_release(self):
        text = self._render()
        self.assertIn("docker pull ghcr.io/studio-fug/yapnr:0.3.1\n", text)
        self.assertIn(f"docker pull ghcr.io/studio-fug/yapnr@{DIGEST}", text)
        self.assertIn("KiCad 10.0.6", text)
        base = "https://github.com/Studio-Fug/yapnr/releases/download/v0.3.1/"
        self.assertIn(base + "yapnr-0.3.1-py3-none-any.whl", text)
        self.assertIn(f'urls = ["{base}yapnr-v0.3.1.tar.gz"]', text)
        self.assertIn(f'integrity = "{notes_header.sri_sha256(self.archive)}"', text)
        self.assertIn('strip_prefix = "yapnr-0.3.1"', text)
        self.assertIn('bazel_dep(name = "yapnr", version = "0.3.1")', text)
        self.assertNotIn("Release candidate", text)
        self.assertNotIn("upgrade notes", text)

    def test_release_candidate(self):
        text = self._render(tag="v0.4.0-rc.2", pep440="0.4.0rc2")
        self.assertTrue(text.startswith("> Release candidate for 0.4.0"))
        self.assertIn("docker pull ghcr.io/studio-fug/yapnr:0.4.0-rc.2\n", text)
        self.assertIn('strip_prefix = "yapnr-0.4.0-rc.2"', text)

    def test_upgrade_notes(self):
        text = self._render(upgrade_notes="docs/releases/v0.3.md")
        self.assertIn("https://github.com/Studio-Fug/yapnr/blob/v0.3.1/docs/releases/v0.3.md", text)

    def test_rejects_a_non_release_tag(self):
        with self.assertRaises(ValueError):
            self._render(tag="v0.3")


if __name__ == "__main__":
    unittest.main()
