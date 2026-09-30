import copy, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pnr import transaction_cleanup as c


class CleanupTest(unittest.TestCase):
    def test_new_native_only(self):
        def r(*ids):
            return dict(violations=[dict(type="via_dangling", items=[dict(uuid=x)]) for x in ids])

        self.assertEqual(c.newly_dangling(r("old"), r("old", "new")), ["new"])

    def test_protection_and_ports(self):
        item = dict(uuid="v", kind="PCB_VIA", locked=False, net="s")
        self.assertTrue(c.eligible(item, ["v"], ["s"], [], "signal", [0]))
        self.assertFalse(c.eligible(item, ["v"], ["s"], [], "signal", [0, 2]))
        self.assertFalse(c.eligible(item, ["v"], ["s"], ["s"], "signal", [0]))
        self.assertFalse(c.eligible(item, ["v"], ["s"], [], "power", [0]))
        self.assertFalse(c.eligible(dict(item, locked=True), ["v"], ["s"], [], "signal", [0]))
        self.assertFalse(c.eligible(dict(item, kind="PCB_TRACK"), ["v"], ["s"], [], "signal", [0]))
        self.assertFalse(c.eligible(item, ["unrelated"], ["s"], [], "signal", [0]))
        self.assertFalse(c.eligible(item, ["v"], ["foreign"], [], "signal", [0]))

    def fixtures(self):
        report = lambda n: dict(violations=[], unconnected_items=[{}] * n)
        audit = dict(
            partition=[["a", "b"]],
            entries={"a": True, "b": True},
            reference_failures=[],
            electrical=dict(subwidth_track_count=2),
        )
        return [
            report(2),
            report(1),
            report(1),
            copy.deepcopy(audit),
            copy.deepcopy(audit),
            copy.deepcopy(audit),
        ]

    def test_original_and_routed_partition(self):
        args = self.fixtures()
        self.assertTrue(c.guards(*args)[0])
        args[-1]["partition"] = [["a"], ["b"]]
        self.assertFalse(c.guards(*args)[0])

    def test_entry_preserved(self):
        args = self.fixtures()
        args[-1]["entries"]["b"] = False
        self.assertFalse(c.guards(*args)[0])

    def test_reference(self):
        args = self.fixtures()
        args[-1]["reference_failures"] = ["lost"]
        self.assertFalse(c.guards(*args)[0])

    def test_width(self):
        args = self.fixtures()
        args[-1]["electrical"]["subwidth_track_count"] = 3
        self.assertFalse(c.guards(*args)[0])

    def test_no_weaker_native_acceptance(self):
        args = self.fixtures()
        args[2]["violations"] = [dict(type="clearance")]
        self.assertFalse(c.guards(*args)[0])

    def test_no_new_opens(self):
        args = self.fixtures()
        args[2]["unconnected_items"] = [{}, {}]
        self.assertFalse(c.guards(*args)[0])

    def test_worker_failure_retains_original(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "candidate.kicad_pcb"
            src.write_text("candidate")
            base = root / "base.kicad_pcb"
            base.write_text("baseline")
            a = SimpleNamespace(
                out_dir=root / "out",
                baseline=base,
                board=src,
                rules=root / "rules",
                kicad_python="fake",
                kicad_cli="fake",
                annotation_source=[],
                net=["s"],
            )
            with patch(
                "pnr.native_drc.run_drc", return_value=dict(violations=[], unconnected_items=[{}])
            ), patch("pnr.proc.run_status", return_value=(23, False)):
                c.run(a)
            result = c.read(a.out_dir / "result.json")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "worker_error")
            self.assertTrue(result["inputs_unchanged"])
            self.assertFalse((a.out_dir / "candidate.kicad_pcb").exists())

    def test_worker_timeout_is_a_worker_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "candidate.kicad_pcb"
            src.write_text("candidate")
            base = root / "base.kicad_pcb"
            base.write_text("baseline")
            a = SimpleNamespace(
                out_dir=root / "out",
                baseline=base,
                board=src,
                rules=root / "rules",
                kicad_python="fake",
                kicad_cli="fake",
                annotation_source=[],
                net=["s"],
            )
            calls = []

            def bounded(cmd, timeout=None, session=True, **kwargs):
                calls.append((timeout, session))
                return -9, True

            with patch(
                "pnr.native_drc.run_drc", return_value=dict(violations=[], unconnected_items=[{}])
            ), patch("pnr.proc.run_status", bounded):
                c.run(a)
            result = c.read(a.out_dir / "result.json")
            self.assertEqual((result["status"], result["returncode"]), ("worker_error", -9))
            self.assertEqual(
                calls, [(None, False)]
            )  # the worker default, PNR_WORKER_TIMEOUT, in this process group
            self.assertEqual(
                c.read(a.out_dir / "worker-exits.json"),
                [dict(mode="audit", returncode=-9, timed_out=True)],
            )


if __name__ == "__main__":
    unittest.main()
