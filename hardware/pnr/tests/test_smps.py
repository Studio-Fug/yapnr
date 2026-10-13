"""Fault injection against guidance-derived SMPS checks (pure copper fixtures)."""

import copy
import json
import unittest

from pnr.smps import audit, parse_checks


def pad(ref, net, x, y):
    return dict(
        ref=ref,
        name="1",
        net=net,
        x=x,
        y=y,
        bbox=[x - 0.1, y - 0.1, x + 0.1, y + 0.1],
        layers=["F.Cu"],
        zone_hits=[],
    )


def track(net, a, b, width=0.2):
    import math

    return dict(
        net=net,
        layer="F.Cu",
        x1=a[0],
        y1=a[1],
        x2=b[0],
        y2=b[1],
        w=width,
        length=math.dist(a, b),
        arc=False,
        pads1=[
            list(key)
            for key, xy in {
                ("A", "1"): (0, 0),
                ("B", "1"): (4, 0),
                ("C", "1"): (0, 0.3),
                ("D", "1"): (4, 0.3),
            }.items()
            if xy == a and ((key[0] in "AB") == (net == "P"))
        ],
        pads2=[
            list(key)
            for key, xy in {
                ("A", "1"): (0, 0),
                ("B", "1"): (4, 0),
                ("C", "1"): (0, 0.3),
                ("D", "1"): (4, 0.3),
            }.items()
            if xy == b and ((key[0] in "AB") == (net == "P"))
        ],
    )


def copper():
    return dict(
        pads=[
            pad("A", "P", 0, 0),
            pad("B", "P", 4, 0),
            pad("C", "N", 0, 0.3),
            pad("D", "N", 4, 0.3),
        ],
        tracks=[track("P", (0, 0), (4, 0)), track("N", (0, 0.3), (4, 0.3))],
        vias=[],
        zones=[],
        contact_errors=[],
        copper_layers=["F.Cu", "B.Cu"],
    )


def check(kind, **kw):
    return dict(id="SMPS-1", kind=kind, source="synthetic-test-requirement", **kw)


class SmpsTest(unittest.TestCase):
    def run_check(self, kind, dump=None, **kw):
        return audit(dump or copper(), [check(kind, **kw)])["checks"][0]

    def test_distance_and_missing_budget(self):
        self.assertEqual(
            self.run_check("pad_distance", terminals=["A:1", "B:1"], max_mm=3)["status"], "fail"
        )
        self.assertEqual(
            self.run_check("pad_distance", terminals=["A:1", "B:1"])["status"], "unqualified"
        )
        self.assertEqual(
            self.run_check("pad_distance", terminals=["UNKNOWN:1", "B:1"], max_mm=3)["status"],
            "unqualified",
        )

    def test_loop_perimeter_catches_collinear_long_loop(self):
        row = self.run_check(
            "loop_geometry",
            terminals=["A:1", "B:1", "D:1", "C:1"],
            max_area_mm2=2,
            max_perimeter_mm=8,
        )
        self.assertEqual(row["status"], "fail")
        self.assertAlmostEqual(row["metrics"]["area_mm2"], 1.2)
        self.assertEqual(row["failures"][0]["metric"], "perimeter_mm")

    def test_route_detour_open_and_unrelated_nets(self):
        args = dict(terminals=["A:1", "B:1"], max_length_mm=5, max_vias=0)
        self.assertEqual(self.run_check("route_path", **args)["status"], "pass")
        d = copper()
        d["tracks"] = [
            track("P", (0, 0), (0, 3)),
            track("P", (0, 3), (4, 3)),
            track("P", (4, 3), (4, 0)),
        ]
        self.assertEqual(self.run_check("route_path", d, **args)["status"], "fail")
        d["tracks"] = []
        self.assertEqual(self.run_check("route_path", d, **args)["status"], "fail")
        self.assertEqual(
            self.run_check("route_path", terminals=["A:1", "C:1"], max_length_mm=9)["status"],
            "fail",
        )

    def test_unverified_pad_bbox_contact_never_certifies_routing(self):
        d = copper()
        d["tracks"][0]["pads1"] = []
        self.assertEqual(
            self.run_check("route_path", d, terminals=["A:1", "B:1"], max_length_mm=5)["status"],
            "unqualified",
        )

    def test_ground_plane_never_becomes_short_zero_impedance_return(self):
        d = copper()
        d["tracks"] = []
        d["zones"] = [dict(net="P", layer="F.Cu", index=0)]
        for p in d["pads"]:
            p["zone_hits"] = [[0, "F.Cu", 0]]
        self.assertEqual(
            self.run_check("route_path", d, terminals=["A:1", "B:1"], max_length_mm=5)["status"],
            "unqualified",
        )

    def test_width_and_switch_node_extent(self):
        self.assertEqual(
            self.run_check("net_geometry", net="P", min_width_mm=0.3)["status"], "fail"
        )
        self.assertEqual(
            self.run_check("net_geometry", net="P", max_bbox_area_mm2=0.1)["status"], "fail"
        )
        self.assertEqual(
            self.run_check("net_geometry", net="P", max_length_mm=5, min_width_mm=0.15)["status"],
            "pass",
        )

    def test_noise_separation_tracks_pads_and_crossings(self):
        self.assertEqual(
            self.run_check("separation", nets=["P"], aggressors=["N"], min_mm=0.2)["status"], "fail"
        )
        d = copper()
        d["tracks"][1] = track("N", (2, -2), (2, 2))
        self.assertEqual(
            self.run_check("separation", d, nets=["P"], aggressors=["N"], min_mm=0.1)["metrics"][
                "mm"
            ],
            0,
        )

    def test_sense_pair_proximity_length_and_parallelism(self):
        args = dict(
            paths=[["A:1", "B:1"], ["C:1", "D:1"]], max_gap_mm=0.4, max_skew_mm=0.1, max_angle_deg=5
        )
        self.assertEqual(self.run_check("paired_routes", **args)["status"], "pass")
        d = copper()
        d["tracks"][1:] = [track("N", (0, 0.3), (2, 2)), track("N", (2, 2), (4, 0.3))]
        self.assertEqual(self.run_check("paired_routes", d, **args)["status"], "fail")

    def test_kelvin_rejects_power_pad_in_sense_path(self):
        d = copper()
        d["pads"].append(pad("Q", "P", 2, 0))
        self.assertEqual(
            self.run_check("kelvin", d, terminals=["A:1", "B:1"], power_terminals=["Q:1", "B:1"])[
                "status"
            ],
            "fail",
        )
        self.assertEqual(
            self.run_check("kelvin", terminals=["A:1", "B:1"], power_terminals=["B:1"])["status"],
            "pass",
        )
        d = copper()
        d["zones"] = [dict(net="P")]
        self.assertEqual(
            self.run_check("kelvin", d, terminals=["A:1", "B:1"], power_terminals=["B:1"])[
                "status"
            ],
            "unqualified",
        )

    def test_stability_evidence_is_bound_to_board_units_and_corners(self):
        c = check(
            "evidence",
            metric="phase_margin",
            min_value=45,
            unit="deg",
            corners=["5V-full", "12V-full"],
        )
        e = dict(
            board_sha256="a" * 64,
            artifact_sha256="b" * 64,
            metric="phase_margin",
            value=55,
            unit="deg",
            status="complete",
            method="loop-gain-simulation",
            corners=c["corners"],
        )

        def run(e):
            return audit(copper(), [c], board_sha256="a" * 64, evidence={"SMPS-1": e})["checks"][0][
                "status"
            ]

        self.assertEqual(run(e), "pass")
        self.assertEqual(run(dict(e, board_sha256="c" * 64)), "unqualified")
        self.assertEqual(run(dict(e, corners=["5V-full"])), "unqualified")
        self.assertEqual(run(dict(e, value=30)), "fail")
        self.assertEqual(run(dict(e, artifact_sha256="not-a-hash")), "unqualified")
        self.assertEqual(run(dict(e, corners="5V-full")), "unqualified")
        self.assertEqual(run(dict(e, value=float("nan"))), "unqualified")
        self.assertEqual(audit(copper(), [c])["status"], "unqualified")

    def test_plane_coverage_and_via_array_do_not_invent_missing_evidence(self):
        c = check(
            "reference_plane",
            terminals=["A:1", "D:1"],
            net="P",
            layer="B.Cu",
            max_uncovered_mm2=0.1,
        )
        self.assertEqual(audit(copper(), [c])["checks"][0]["status"], "unqualified")
        d = copper()
        d["plane_coverage"] = {"SMPS-1": dict(uncovered_mm2=0.5)}
        self.assertEqual(audit(d, [c])["checks"][0]["status"], "fail")
        d["vias"] = [dict(net="P", x=0, y=0, d=0.5, top="F.Cu", bottom="B.Cu")]
        c = check(
            "via_array",
            terminals=["A:1", "B:1"],
            net="P",
            layers=["F.Cu", "B.Cu"],
            radius_mm=1,
            min_count=2,
        )
        self.assertEqual(audit(d, [c])["checks"][0]["status"], "fail")
        d["vias"].append(dict(d["vias"][0], x=0.5))
        self.assertEqual(audit(d, [c])["checks"][0]["status"], "pass")

    def test_compilation_and_native_ranking_keep_unknowns_visible(self):
        import os
        from unittest.mock import patch

        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.full_iteration import smps_side_fields
        from pnr.hier.synth_native import is_complete, rank_key
        from pnr.mc.halving import _rank_key

        c = check("pad_distance", terminals=["A:1", "B:1"], max_mm=3)
        self.assertIsNone(smps_side_fields({}, {"smps_checks": [c]})["smps_layout_unqualified"])
        self.assertEqual(smps_side_fields({}, {})["smps_layout_unqualified"], 0)
        con = compile_constraints(dict(smps_checks=[c]), ["A", "B"])
        self.assertEqual(compile_routing_rules(con, ["P"])["smps_checks"], [c])
        good = dict(
            status="ok",
            objective=[0] * 6,
            smps_layout_failures=0,
            smps_layout_unqualified=0,
            hot_loops_open=0,
            power_loop_failures=0,
        )
        bad = dict(good, smps_layout_failures=1)
        unknown = dict(good, smps_layout_unqualified=None)
        with patch.dict(os.environ, {"PNR_SMPS_LAYOUT": "1"}):
            self.assertLess(rank_key(good), rank_key(bad))
            self.assertLess(rank_key(good), rank_key(unknown))
            self.assertFalse(is_complete(unknown))
            self.assertTrue(is_complete(good))
            for stage in ("native", "deep"):
                key = _rank_key(stage)
                self.assertLess(key(dict(good, id="a")), key(dict(bad, id="b")))
                self.assertLess(key(dict(good, id="a")), key(dict(unknown, id="c")))

    def test_subboard_scope_and_nonfinite_geometry(self):
        from pnr.smps import scope_checks

        checks = [
            check("pad_distance", terminals=["A:1", "B:1"], max_mm=3),
            dict(check("pad_distance", terminals=["A:1", "Q:1"], max_mm=3), id="parent"),
        ]
        local, deferred = scope_checks(checks, ["A", "B"], ["P"])
        self.assertEqual([c["id"] for c in local], ["SMPS-1"])
        self.assertEqual(deferred, ["parent"])
        d = copper()
        d["pads"][0]["x"] = float("nan")
        self.assertEqual(audit(d, checks[:1])["checks"][0]["status"], "unqualified")

    def test_bad_contracts_and_read_only_input(self):
        for c in (
            check("no-such-kind"),
            check("reference_plane", terminals=["A:1", "B:1"], net="P"),
            check("pad_distance", terminals=[":", "B:1"], max_mm=3),
            check("pad_distance", terminals=["A:1", "B:1"], max_mm=float("nan")),
            check("route_path", terminals=["A:1", "B:1"], max_vias=True),
        ):
            with self.assertRaises(ValueError):
                parse_checks([c])
        d = copper()
        before = copy.deepcopy(d)
        report = audit(d, [check("route_path", terminals=["A:1", "B:1"], max_length_mm=5)])
        self.assertEqual(d, before)
        self.assertFalse(report["qualified"])
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
