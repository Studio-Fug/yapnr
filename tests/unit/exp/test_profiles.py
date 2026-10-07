"""``[profile]``: the wrapper's PNR_PROFILE_DIR and profile upload, and ``yapnr exp profile``'s
aggregation of pnr.profile records into stages, spans and hot functions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import profiles, spec, testing


def profile_record(label, wall, functions, spans=None):
    return {
        "schema": "pnr-profile-v1",
        "label": label,
        "wall_seconds": wall,
        "cpu_seconds": wall * 0.9,
        "spans": spans or {},
        "hot_functions": [
            {
                "file": "/work/src/hardware/pnr/pnr/%s.py" % module,
                "line": 10,
                "function": name,
                "calls": 3,
                "self_seconds": seconds,
            }
            for module, name, seconds in functions
        ],
    }


class AggregateTest(unittest.TestCase):
    def test_stages_spans_functions_and_the_uncovered_rest(self):
        tasks = [
            (
                "t1",
                [
                    profile_record(
                        "route",
                        60.0,
                        [("maze", "expand", 30.0), ("drc", "check", 10.0)],
                        {"visibility_tree_search": {"calls": 5, "wall_seconds": 20.0}},
                    ),
                    profile_record("place", 20.0, [("place", "anneal", 15.0)]),
                ],
                100.0,
            ),
            (
                "t2",
                [profile_record("route", 40.0, [("maze", "expand", 20.0)])],
                50.0,
            ),
            ("t3", [], 50.0),
        ]
        report = profiles.aggregate(tasks, {"vm_hours": 0.2, "usd": 0.03})
        self.assertEqual(
            (report["tasks"], report["tasks_profiled"], report["processes"]), (3, 2, 3)
        )
        route = report["stages"][0]
        self.assertEqual((route["label"], route["processes"]), ("route", 2))
        self.assertAlmostEqual(route["share"], 100.0 / 200.0)
        self.assertAlmostEqual(route["vm_hours"], 0.1)
        top = report["functions"][0]
        self.assertEqual((top["file"], top["function"]), ("pnr/maze.py", "expand:10"))
        self.assertAlmostEqual(top["self_h"], 50.0 / 3600, places=4)
        self.assertEqual(top["processes"], 2)
        self.assertEqual(report["spans"][0]["name"], "visibility_tree_search")
        # 200 s of task wall time, 120 s inside profiled processes.
        self.assertAlmostEqual(report["unprofiled"]["share"], 0.4)
        self.assertIn("hot functions", profiles.table(report))

    def test_runner_stage_timing_from_fetched_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, place in (("a", 30.0), ("b", 50.0)):
                task = root / "tasks" / name
                (task / "summary" / "run" / "case-seed-0").mkdir(parents=True)
                (task / "record.json").write_text(json.dumps({"wall_s": 100.0}))
                (task / "summary" / "run" / "case-seed-0" / "result.json").write_text(
                    json.dumps(
                        {
                            "stages": {"place-route": place, "gloss": 20.0},
                            "cpu_stages": {"place-route": place, "gloss": 50.0},
                        }
                    )
                )
            report = profiles.aggregate(profiles.load_task_profiles(root))
        self.assertEqual(report["processes"], 0)
        first = report["runner_stages"][0]
        self.assertEqual((first["stage"], first["cells"]), ("place-route", 2))
        self.assertAlmostEqual(first["share"], 80.0 / 200.0)
        gloss = report["runner_stages"][1]
        self.assertAlmostEqual(gloss["cpu_h"], 100.0 / 3600, places=4)
        self.assertIn("runner stages", profiles.table(report))

    def test_short_paths(self):
        self.assertEqual(profiles.short_path("/a/src/hardware/pnr/pnr/x.py"), "pnr/x.py")
        self.assertEqual(profiles.short_path("/v/lib/site-packages/numpy/core.py"), "numpy/core.py")
        self.assertEqual(profiles.short_path("~"), "~")
        self.assertEqual(profiles.short_path("/a/b/c/d.py"), "c/d.py")


class ProfiledTaskTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self.toolchain = testing.host_toolchain(self.tmp / "toolchain.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_wrapper_sets_the_profile_dir_and_uploads_the_records(self):
        script = (
            "import json, os\n"
            "d = os.environ['PNR_PROFILE_DIR']\n"
            "os.makedirs(d, exist_ok=True)\n"
            "json.dump({'schema': 'pnr-profile-v1', 'label': 'x'}, open(d + '/k.json', 'w'))\n"
            "open(d + '/k.pstats', 'w').write('raw')\n"
            "os.makedirs('out', exist_ok=True)\n"
            "json.dump({'complete': True}, open('out/done.json', 'w'))\n"
        )
        task = testing.python_task("t/p", script)
        base = testing.write_store_campaign(self.store, [task])
        meta = json.loads((base / "campaign.json").read_text())
        meta["profile"] = {"enabled": True}
        (base / "campaign.json").write_text(json.dumps(meta))
        result = testing.run_wrapper(self.store, 0, self.toolchain)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        attempt = next((base / "tasks" / "t~p").glob("s1r0*"))
        self.assertEqual(sorted(p.name for p in (attempt / "profiles").iterdir()), ["k.json"])
        # The outputs are untouched: the profile directory is outside them.
        import tarfile

        with tarfile.open(attempt / "result.tar.gz") as tar:
            self.assertFalse([n for n in tar.getnames() if "profiles" in n])

    def test_the_campaign_table_is_checked(self):
        base = {"schema": "yapnr-campaign-v1", "kind": "smoke", "image": "edge"}
        self.assertEqual(spec.campaign_errors(dict(base, profile={"enabled": True})), [])
        self.assertTrue(spec.campaign_errors(dict(base, profile={"enabled": "yes"})))
        self.assertTrue(spec.campaign_errors(dict(base, profile={"dir": "x"})))


if __name__ == "__main__":
    unittest.main()
