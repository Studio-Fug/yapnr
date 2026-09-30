"""A real ``ato build`` (atopile 0.15.8) through ``yapnr atopile build``, twice per project.

Needs the atopile environment (``yapnr atopile setup``, or ``YAPNR_ATO_PYTHON``) and skips
without it. Everything else is synthetic and local: one self-drawn part in a temporary part
cache, and two small projects that keep no parts in their tree, only a parts lock.

- **No picks:** the parts are used directly; atopile sends no part query (the only request to
  the picker is its message of the day), and nothing is downloaded.
- **One pick:** a ``Resistor`` picked by LCSC id, or constrained to 1 kohm +/- 5 % in 0603. The
  loopback picker answers from the cache's catalog entry, and the hook attaches the part the
  runner materialized from the cache (no EasyEDA, no hosted service).

Each project is built twice and must give the same input id (the UUID-normalized board).
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from yapnr.frontends.atopile import parts, runner, testing, toolchain
from yapnr.partcache import importer
from yapnr.partcache.client import LocalPartCache


def atopile_available() -> str:
    try:
        toolchain.discover()
        return ""
    except toolchain.ToolchainError as err:
        return str(err)


SKIP = atopile_available()


@unittest.skipIf(SKIP, f"no atopile environment: {SKIP}")
class AtopileBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.root = root
        cls.cache = LocalPartCache(root / "cache", create=True)
        source = root / "authoring"
        testing.write_part(source)
        importer.import_part_dirs(cls.cache, [source / testing.SYNTHETIC_PART], "e2e test")
        cls.cache.put_catalog(testing.catalog_entry(), {"source": "e2e test (synthetic)"})
        part_id = cls.cache.find()[0]["id"]
        cls.projects = {}
        for pick in (None, "lcsc", "type"):
            project = testing.write_project(root / f"project-{pick or 'none'}", pick=pick)
            lock = {
                "schema": parts.LOCK_SCHEMA,
                "parts_dir": "elec/src/parts",
                "parts": [{"name": testing.SYNTHETIC_PART, "id": part_id}],
            }
            (project / parts.LOCK_NAME).write_text(parts.dump(lock))
            cls.projects[pick] = project

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build_twice(self, pick):
        results = []
        for run in (1, 2):
            options = runner.BuildOptions(
                project=self.projects[pick],
                out=self.root / f"out-{pick or 'none'}-{run}",
                cache=self.cache.location,
                timeout=900,
            )
            result = runner.build(options, log=lambda _text: None)
            log = (result.out / "ato.log").read_text(errors="replace")[-4000:]
            self.assertTrue(result.ok, f"build {run} failed:\n{log}")
            results.append(result)
        self.assertEqual(results[0].input_id, results[1].input_id)
        self.assertFalse((self.projects[pick] / "elec/src/parts").exists())
        return results[0]

    def bom(self, result):
        with open(result.out / result.outputs["bom_csv"], newline="") as handle:
            return list(csv.DictReader(handle))

    def test_no_picks(self):
        result = self.build_twice(None)
        summary = json.loads((result.out / "result.json").read_text())
        queries = [r for r in summary["picker_requests"] if r["path"].startswith("/v0/query")]
        self.assertEqual(queries, [])
        self.assertEqual(summary["local_parts"], [])
        self.assertEqual(summary["refused"], [])
        self.assertEqual(summary["atopile"], "0.15.8")
        rows = self.bom(result)
        self.assertEqual(sum(int(r["Quantity"]) for r in rows), 2)

    def test_one_lcsc_pick_from_the_cache(self):
        result = self.build_twice("lcsc")
        summary = json.loads((result.out / "result.json").read_text())
        queries = [r for r in summary["picker_requests"] if r["path"].startswith("/v0/query")]
        self.assertEqual(len(queries), 1)
        self.assertEqual(summary["local_parts"], [testing.SYNTHETIC_LCSC])
        self.assertEqual(sum(int(r["Quantity"]) for r in self.bom(result)), 3)

    def test_one_type_pick_from_the_cache(self):
        result = self.build_twice("type")
        summary = json.loads((result.out / "result.json").read_text())
        queries = [r for r in summary["picker_requests"] if r["path"].startswith("/v0/query")]
        self.assertEqual(len(queries), 1)
        self.assertEqual(summary["local_parts"], [testing.SYNTHETIC_LCSC])
        self.assertEqual(summary["refused"], [])
        rows = self.bom(result)
        lcsc = {r.get("LCSC Part #") or r.get("LCSC") or "" for r in rows}
        self.assertIn(testing.SYNTHETIC_LCSC, lcsc, rows)
        self.assertEqual(sum(int(r["Quantity"]) for r in rows), 3)
        board = (result.out / result.outputs["pcb"]).read_text()
        self.assertEqual(board.count(f"{testing.SYNTHETIC_PART}:SYN_R0603"), 3)


if __name__ == "__main__":
    unittest.main()
