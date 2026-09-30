"""The on-disk part cache: content addressing, catalog, takedowns, reindex and verify."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.frontends.atopile import testing
from yapnr.partcache import importer, model
from yapnr.partcache.client import CacheError, LocalPartCache, materialize, read_part_dir
from yapnr.partcache.store import NotFound, Store, TakenDown

PROVENANCE = {"source": "self", "notes": "unit test"}
LICENCE = {"spdx": "CC0-1.0"}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = LocalPartCache(self.root / "cache", create=True)
        self.store = self.cache.store
        self.part_dir = testing.write_part(self.root / "parts")

    def upload(self, part_dir=None):
        request, blobs = read_part_dir(part_dir or self.part_dir, PROVENANCE, LICENCE)
        return self.cache.upload_part(request, blobs)

    def test_open_requires_a_cache(self):
        with self.assertRaises(NotFound):
            Store.open(self.root / "nothing")
        (self.root / "busy").mkdir()
        (self.root / "busy/file").write_text("x")
        with self.assertRaises(FileExistsError):
            Store.open(self.root / "busy", create=True)

    def test_upload_is_content_addressed_and_idempotent(self):
        first = self.upload()
        self.assertEqual(first["lcsc"], testing.SYNTHETIC_LCSC)
        self.assertEqual(first["licence"], LICENCE)
        second = self.upload()
        self.assertEqual(first, second)
        for entry in first["files"]:
            path = self.store.blob_path(entry["sha256"])
            self.assertEqual(model.sha256_bytes(path.read_bytes()), entry["sha256"])
        self.assertEqual(self.store.stats()["parts"], 1)

    def test_blob_hash_is_checked(self):
        with self.assertRaises(model.InvalidPart):
            self.store.put_blob(b"data", expected="0" * 64)

    def test_part_needs_its_blobs(self):
        request, _blobs = read_part_dir(self.part_dir, PROVENANCE, LICENCE)
        with self.assertRaisesRegex(model.InvalidPart, "missing"):
            self.store.put_part(request)

    def test_find_and_current(self):
        first = self.upload()
        (self.part_dir / "NOTES.md").write_text("second version\n")
        second = self.upload()
        self.assertNotEqual(first["id"], second["id"])
        latest = self.cache.find(lcsc=testing.SYNTHETIC_LCSC)
        self.assertEqual(len(latest), 1)
        every = self.cache.find(lcsc=testing.SYNTHETIC_LCSC, latest_only=False)
        self.assertEqual({p["id"] for p in every}, {first["id"], second["id"]})
        self.assertEqual(len(self.cache.find(mpn="sr1k", manufacturer="YAPNR-SYNTHETIC")), 1)
        self.assertEqual(len(self.cache.find(text="Synthetic")), 1)
        self.assertEqual(self.cache.find(lcsc="C1"), [])
        self.assertIn(self.cache.current(testing.SYNTHETIC_LCSC)["id"], {first["id"], second["id"]})

    def test_takedown_removes_unshared_files_and_blocks_reupload(self):
        first = self.upload()
        (self.part_dir / "NOTES.md").write_text("another version\n")
        second = self.upload()
        shared = {f["sha256"] for f in first["files"]} & {f["sha256"] for f in second["files"]}
        result = self.cache.delete_part(second["id"], "unit test takedown")
        self.assertEqual(result["removed_blobs"], 1)  # NOTES.md only
        for sha in shared:
            self.assertTrue(self.store.has_blob(sha))
        with self.assertRaises(NotFound):
            self.store.manifest(second["id"])
        with self.assertRaises(TakenDown):
            self.upload()
        log = (self.root / "cache/takedowns.jsonl").read_text()
        self.assertIn("unit test takedown", log)
        # The takedown survives a rebuilt index.
        self.store.reindex()
        with self.assertRaises(TakenDown):
            self.upload()
        self.assertEqual(
            self.store.stats(),
            {"parts": 1, "catalog": 0, "takedowns": 1, "blocked_files": 1, "local_only": 0},
        )

    def renamed_copy(self, source, name):
        """The files of ``source`` as a part named ``name`` (the same model file, byte for byte)."""
        target = self.root / "renamed" / name
        target.mkdir(parents=True)
        for path in source.iterdir():
            data = path.read_bytes()
            if path.suffix != ".step":
                data = data.replace(source.name.encode(), name.encode())
            (target / path.name.replace(source.name, name)).write_bytes(data)
        return target

    def test_takedown_blocks_its_files_under_any_name(self):
        (self.part_dir / "MODEL.step").write_bytes(b"ISO-10303-21; synthetic model\n")
        manifest = self.upload()
        step = next(f["sha256"] for f in manifest["files"] if f["path"] == "MODEL.step")
        result = self.cache.delete_part(manifest["id"], "vendor request")
        self.assertIn(step, result["blocked_files"])
        self.assertFalse(self.store.has_blob(step))
        with self.assertRaises(TakenDown):
            self.upload(self.renamed_copy(self.part_dir, "Renamed_Part"))
        self.assertFalse(self.store.has_blob(step))
        with self.assertRaises(TakenDown):
            self.store.put_blob((self.part_dir / "MODEL.step").read_bytes())
        # The block survives a rebuilt index.
        self.store.reindex()
        with self.assertRaises(TakenDown):
            self.store.put_blob((self.part_dir / "MODEL.step").read_bytes())

    def test_takedown_block_choices(self):
        (self.part_dir / "MODEL.step").write_bytes(b"ISO-10303-21; another model\n")
        first = self.upload()
        other = self.upload(self.renamed_copy(self.part_dir, "Other_Part"))
        with self.assertRaisesRegex(model.InvalidPart, "other parts use"):
            self.cache.delete_part(first["id"], "x", block=["MODEL.step"])
        with self.assertRaisesRegex(model.InvalidPart, "no files"):
            self.cache.delete_part(first["id"], "x", block=["missing.md"])
        result = self.cache.delete_part(first["id"], "wrong .ato only", block="none")
        self.assertEqual(result["blocked_files"], [])
        self.assertTrue(self.store.has_blob(other["files"][0]["sha256"]))
        again = self.cache.delete_part(other["id"], "the model", block=["MODEL.step"])
        self.assertEqual(len(again["blocked_files"]), 1)

    def test_unused_blobs_are_not_published_and_are_collected(self):
        data = b"<html><script>not a part file</script>"
        sha = self.store.put_blob(data)
        with self.assertRaises(NotFound):
            self.store.read_published_blob(sha)
        self.assertEqual(self.store.gc(min_age=3600), 0)  # an upload in progress is spared
        old = time.time() - 7200
        os.utime(self.store.blob_path(sha), (old, old))
        self.assertEqual(self.store.gc(min_age=3600), 1)
        manifest = self.upload()
        used = manifest["files"][0]["sha256"]
        self.assertEqual(self.store.read_published_blob(used), self.store.read_blob(used))
        os.utime(self.store.blob_path(used), (old, old))
        self.assertEqual(self.store.gc(min_age=3600), 0)

    def test_part_files_must_match_their_types(self):
        (self.part_dir / "MODEL.step").write_bytes(b"<html>not a model</html>")
        with self.assertRaisesRegex(model.InvalidPart, "MODEL.step"):
            self.upload()
        (self.part_dir / "MODEL.step").unlink()
        (self.part_dir / "NOTES.md").write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaisesRegex(model.InvalidPart, "UTF-8"):
            self.upload()

    def test_local_only_parts(self):
        records = importer.import_part_dirs(
            self.cache, [self.part_dir], "unit test", local_only=True
        )
        manifest = self.store.manifest(records[0]["id"])
        self.assertEqual(manifest["licence"]["distribution"], "local-only")
        self.assertEqual(self.store.local_only_parts(), [manifest["id"]])
        self.store.set_distribution(manifest["id"], "shareable")
        self.assertEqual(self.store.local_only_parts(), [])
        self.assertEqual(
            model.check_manifest(self.store.manifest(manifest["id"]))["id"], manifest["id"]
        )
        self.store.set_distribution(manifest["id"], "local-only")
        self.store.reindex()
        self.assertEqual(self.store.stats()["local_only"], 1)

    def test_materialize_refuses_another_part_for_the_id(self):
        first = self.upload()
        (self.part_dir / "NOTES.md").write_text("a self-consistent other version\n")
        second = self.upload()
        store = self.store

        class Substituting(LocalPartCache):
            def __init__(self):
                self.store, self.location = store, "substituting"

            def manifest(self, part_id):
                return store.manifest(second["id"])

        with self.assertRaisesRegex(CacheError, "returned"):
            materialize(Substituting(), first["id"], self.root / "project/parts")
        self.assertFalse((self.root / "project/parts" / first["name"]).exists())

    def test_reindex_and_verify(self):
        manifest = self.upload()
        (self.root / "cache/index.sqlite").unlink()
        fresh = Store.open(self.root / "cache")
        self.assertEqual(fresh.stats()["parts"], 1)
        self.assertEqual(fresh.verify(), [])
        blob = fresh.blob_path(manifest["files"][0]["sha256"])
        blob.write_bytes(b"tampered")
        problems = fresh.verify()
        self.assertTrue(any("does not match" in p for p in problems), problems)

    def test_materialize_verifies_and_refuses_to_clobber(self):
        manifest = self.upload()
        target = self.root / "project/parts"
        written = materialize(self.cache, manifest["id"], target)
        self.assertEqual(
            sorted(p.name for p in written.iterdir()), sorted(f["path"] for f in manifest["files"])
        )
        (written / "NOTES.md").write_text("local edit")
        with self.assertRaisesRegex(Exception, "other content"):
            materialize(self.cache, manifest["id"], target)
        materialize(self.cache, manifest["id"], target, replace=True)
        self.assertFalse((written / "NOTES.md").exists())
        self.store.blob_path(manifest["files"][0]["sha256"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(Exception, "does not match"):
            materialize(self.cache, manifest["id"], self.root / "elsewhere")
        self.assertFalse((self.root / "elsewhere" / manifest["name"]).exists())

    def test_catalog_entries(self):
        entry = self.store.put_catalog(testing.catalog_entry(), {"source": "unit test"})
        self.assertEqual(entry["part"]["lcsc"], testing.SYNTHETIC_LCSC)
        doc = self.cache.catalog()
        self.assertEqual([p["lcsc"] for p in doc["parts"]], [testing.SYNTHETIC_LCSC])
        self.assertEqual(self.cache.catalog(["C900002"])["parts"], [])
        self.store.delete_catalog(testing.SYNTHETIC_LCSC, "unit test")
        with self.assertRaises(TakenDown):
            self.store.put_catalog(testing.catalog_entry(), {"source": "unit test"})
        with self.assertRaises(ValueError):
            self.store.put_catalog({"lcsc": "C5", "mpn": 1}, {"source": "x"})

    def test_importer_records_provenance_from_the_files(self):
        generated = testing.write_part(self.root / "gen", name="Generated_Part", lcsc="C900005")
        ato = generated / "Generated_Part.ato"
        ato.write_text(
            ato.read_text().replace(
                "    trait is_atomic_part",
                '    trait is_auto_generated<system="ato_part", source="easyeda:C900005",'
                ' date="2026-01-02T03:04:05+00:00", checksum="00">\n    trait is_atomic_part',
            )
        )
        records = importer.import_part_dirs(
            self.cache, [generated, self.part_dir], "unit test", licence_note="Note."
        )
        by_name = {r["name"]: r for r in records}
        manifest = self.store.manifest(by_name["Generated_Part"]["id"])
        self.assertEqual(manifest["provenance"]["source"], "easyeda:C900005")
        self.assertEqual(manifest["provenance"]["created"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(manifest["licence"]["spdx"], "NOASSERTION")
        self.assertIn("terms_url", manifest["licence"])
        self.assertTrue(manifest["licence"]["notes"].endswith("Note."))
        hand = self.store.manifest(by_name[testing.SYNTHETIC_PART]["id"])
        self.assertTrue(hand["provenance"]["source"].startswith("hand-authored"))
        self.assertEqual(hand["provenance"]["imported_from"], f"unit test/{testing.SYNTHETIC_PART}")
        self.assertEqual(json.loads(json.dumps(hand)), hand)


if __name__ == "__main__":
    unittest.main()
