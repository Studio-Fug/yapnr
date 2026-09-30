"""A project's parts lock: written from part directories, materialized from a cache."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.frontends.atopile import parts, testing
from yapnr.partcache.client import LocalPartCache


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = testing.write_project(self.root / "project")
        testing.write_part(self.project / "elec/src/parts")
        self.cache = LocalPartCache(self.root / "cache", create=True)

    def test_lock_without_a_cache_uses_the_files(self):
        doc = parts.lock_directory(self.project)
        self.assertEqual(doc["parts_dir"], "elec/src/parts")
        self.assertEqual(doc["parts"][0]["name"], testing.SYNTHETIC_PART)
        self.assertEqual(doc["parts"][0]["lcsc"], testing.SYNTHETIC_LCSC)
        self.assertRegex(doc["parts"][0]["id"], r"^[0-9a-f]{64}$")

    def test_upload_then_materialize_elsewhere(self):
        doc = parts.lock_directory(self.project, cache=self.cache, upload=True)
        manifest = self.cache.manifest(doc["parts"][0]["id"])
        self.assertEqual(manifest["provenance"]["source"][:5], "hand-")
        (self.project / parts.LOCK_NAME).write_text(parts.dump(doc))
        loaded = parts.load(self.project)
        self.assertEqual(loaded, json.loads(parts.dump(doc)))
        target = testing.write_project(self.root / "other")
        written = parts.materialize_lock(target, loaded, self.cache)
        self.assertEqual([p.name for p in written], [testing.SYNTHETIC_PART])
        for name in ("SR1K.kicad_sym", "SYN_R0603.kicad_mod"):
            source = self.project / "elec/src/parts" / testing.SYNTHETIC_PART / name
            self.assertEqual((written[0] / name).read_bytes(), source.read_bytes())
        # Again: the same content is left alone.
        self.assertEqual(parts.materialize_lock(target, loaded, self.cache), written)
        self.assertEqual(parts.locked_lcsc(loaded, self.cache), [testing.SYNTHETIC_LCSC])

    def test_lock_against_a_cache_needs_the_parts_there(self):
        with self.assertRaises(KeyError):
            parts.lock_directory(self.project, cache=self.cache)

    def test_invalid_locks(self):
        good = {"schema": parts.LOCK_SCHEMA, "parts_dir": "elec/src/parts", "parts": []}
        parts.validate(good)
        for bad in (
            {**good, "schema": "x"},
            {**good, "parts_dir": "../outside"},
            {**good, "parts_dir": "/abs"},
            {**good, "parts": [{"name": "A", "id": "short"}]},
            {**good, "parts": [{"name": "a/b", "id": "0" * 64}]},
            {**good, "parts": [{"name": "A", "id": "0" * 64}, {"name": "A", "id": "1" * 64}]},
        ):
            with self.assertRaises(ValueError, msg=bad):
                parts.validate(bad)

    def test_parts_dir_from_ato_yaml(self):
        text = (self.project / "ato.yaml").read_text()
        (self.project / "ato.yaml").write_text(text + "paths:\n  src: hw/src\n")
        self.assertEqual(parts.parts_dir_of(self.project), "hw/src/parts")
        (self.project / "ato.yaml").write_text(text + "paths:\n  parts: lib/parts\n")
        self.assertEqual(parts.parts_dir_of(self.project), "lib/parts")


if __name__ == "__main__":
    unittest.main()
