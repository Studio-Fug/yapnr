"""Calibration ingest: the reference's load is recorded, and a busy reference is flagged."""

from __future__ import annotations

import datetime as _dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from yapnr.exp import calibration, cli


def record(wall, machine=None, load=None, vcpus=10, seed="0"):
    machine_info = {"machine_type": machine} if machine else {"platform": "darwin-arm64"}
    machine_info["vcpus"] = vcpus
    return {
        "kind": "ladder-cell",
        "labels": {"case": "05-timer-led-10", "seed": seed},
        "verdict": "pass",
        "wall_s": wall,
        "machine": machine_info,
        "load_avg_start": [load, load, load] if load is not None else None,
    }


class ReferenceLoadTest(unittest.TestCase):
    def test_idle_reference(self):
        reference = (r for r in [record(30.0, load=2.5), record(30.0, load=3.5, seed="1")])
        cloud = [record(40.0, "c4d-highcpu-8", vcpus=8), record(40.0, "c4d-highcpu-8", seed="1")]
        data = calibration.ingest(reference, cloud, _dt.date(2026, 10, 2))
        # A generator works: the reference records are read twice (pairs, then load).
        self.assertEqual(data["speed"]["c4d-highcpu-8"], 0.75)
        self.assertEqual(
            data["reference_load"], {"median_load_1m": 3.0, "vcpus": 10, "busy": False}
        )

    def test_busy_reference_is_flagged(self):
        data = calibration.ingest(
            [record(30.0, load=10.9), record(30.0, load=9.8, seed="1")],
            [record(40.0, "c4d-highcpu-8", vcpus=8)],
        )
        self.assertTrue(data["reference_load"]["busy"])

    def test_no_load_figures(self):
        data = calibration.ingest([record(30.0)], [record(40.0, "c4d-highcpu-8", vcpus=8)])
        self.assertIsNone(data["reference_load"])

    def test_slurm_records_are_keyed_by_cpu_model(self):
        site = dict(record(60.0), backend="slurm")
        site["machine"] = {"cpu_model": "AMD EPYC 7763 64-Core Processor", "vcpus": 128}
        data = calibration.ingest([record(30.0)], [site])
        self.assertEqual(data["speed"], {"slurm/AMD EPYC 7763 64-Core Processor": 0.5})

    def test_cli_warns_about_a_busy_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, rec in (("ref", record(30.0, load=10.0)), ("cloud", record(40.0, "c4d-x-8"))):
                path = Path(tmp, name, "tasks", "t0", "record.json")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(rec))
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "calibration",
                        "ingest",
                        "--reference",
                        str(Path(tmp, "ref")),
                        "--cloud",
                        str(Path(tmp, "cloud")),
                        "--out",
                        str(Path(tmp, "calibration.json")),
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn("reference load 10.00", out.getvalue())
        self.assertIn("busy machine", out.getvalue())


if __name__ == "__main__":
    unittest.main()
