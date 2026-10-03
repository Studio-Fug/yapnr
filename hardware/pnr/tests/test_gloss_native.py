"""PNR_GLOSS KiCad adapter (pnr.gloss): planning, transactional apply/re-check, facts, metrics.

Headless KiCad python, in-memory boards (gloss_fixture), one process: every worker runs
in-process (no subprocess, no kicad-cli). End-to-end passes with native DRC live in
test_gloss_e2e (PnR runtime python driving KiCad workers one at a time).
"""

import argparse
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None
MM = 1_000_000


def args(**kw):
    base = dict(
        board=None,
        rules=None,
        annotation_source=[],
        drc=None,
        gloss_si=False,
        worker=None,
        spec=None,
        out=None,
        report=None,
        step=None,
        skip=None,
        deadline=60.0,
        full=False,
        guard_open_nets=False,
        no_hug=False,
        classes=None,
        cross_group_mm=None,
        classes_from=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@unittest.skipUnless(NATIVE, "requires native KiCad Python")
class GlossNativeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def board(self, extra=None, rules=None, name="fx"):
        import gloss_fixture

        path = gloss_fixture.write(self.root / name, extra)
        if rules is not None:
            (path.parent / "rules.json").write_text(json.dumps(rules))
        return path, path.parent / "rules.json"

    def model(self, path, rules_path, **kw):
        from pnr import gloss
        from pnr.fab_profile import load_board

        b = load_board(path)
        b.BuildConnectivity()
        self._keep = b
        return gloss.Model(b, json.loads(Path(rules_path).read_text()), path=path, **kw)

    def run_worker(self, **kw):
        from pnr import gloss

        report = self.root / ("%s-%d.json" % (kw["worker"], len(list(self.root.glob("*.json")))))
        gloss.worker(args(report=report, **kw))
        return json.loads(report.read_text())

    def plan_all(self, path, rules):
        out = {}
        for step in ("normalize", "dekink", "gloss", "corridor"):
            out[step] = self.run_worker(worker="inventory", board=path, rules=rules, step=step)
        return out

    # ------------------------------------------------------------ planning
    def test_plan_every_step_on_fixture(self):
        from pnr import gloss_geometry as g

        path, rules = self.board()
        inv = self.plan_all(path, rules)
        norm = inv["normalize"]["specs"]
        self.assertEqual([s["net"] for s in norm], ["sig2"])
        self.assertEqual(norm[0]["counts"]["merged"], 3)
        self.assertEqual(inv["normalize"]["stats"]["duplicate_joint"], 1)  # sig3 duplicate kept
        self.assertEqual([s["net"] for s in inv["dekink"]["specs"]], ["sig"])
        gl = {s["net"]: s for s in inv["gloss"]["specs"]}
        self.assertEqual(sorted(gl), ["c1", "sig"])  # c1's U pulls up inside its tube
        old, new = [tuple(p) for p in gl["sig"]["old_nm"]], [tuple(p) for p in gl["sig"]["new_nm"]]
        self.assertTrue(g.rule_r(old, new))
        # homotopy: the foreign 'other' pad stays on the same side (inside the first detour)
        self.assertEqual(g.swept_points(old, new, [(10500000, 10000000)]), [])
        self.assertEqual((new[0], new[-1]), (old[0], old[-1]))
        cor = inv["corridor"]["specs"]
        self.assertEqual(len(cor), 1)
        self.assertEqual([m["net"] for m in cor[0]["members"]], ["c1"])
        self.assertAlmostEqual(cor[0]["dE_mm2"], -2.553, places=3)
        self.assertAlmostEqual(cor[0]["members"][0]["dL"], 0.184, places=3)

    def test_eligibility_and_frozen_copper(self):
        from pnr import gloss
        from pnr.fab_profile import apply_rules

        def pair(b, h):
            h["path"]([(8, 7.9), (20, 7.9)], "Dp")
            h["path"]([(8, 7.45), (20, 7.45)], "Dn")

        base = json.loads(Path(self.board()[1]).read_text())
        rules = apply_rules(
            dict(
                base,
                diff_pairs=[dict(name="pair0", p="Dp", n="Dn", gap_mm=0.15)],
                net_classes=[dict(name="power", width_mm=0.5, nets=["c2"])],
            )
        )
        path, rules_path = self.board(pair, rules, name="pair")
        m = self.model(path, rules_path)
        la = m.k.F_Cu
        self.assertTrue(m.info("sig", la)["eligible"])
        self.assertEqual(m.info("Dp", la)["reason"], "E1:pair")
        self.assertEqual(m.info("c2", la)["reason"], "E1:power")
        guards = m.guards("sig", la, 200000, (4 * MM, 8 * MM, 26 * MM, 12 * MM))
        self.assertEqual([x.name for x in guards], ["G1:pair0"])
        self.assertAlmostEqual(guards[0].cap, 0.45 * MM)
        # a DRC report naming a track freezes it (A7/F3)
        sig = [t for t in m.groups["sig", la]]
        drc = dict(
            violations=[dict(type="x", items=[dict(uuid=gloss.uid(sig[0]))])], unconnected_items=[]
        )
        frozen = self.model(path, rules_path, drc=drc)
        self.assertEqual(sum(s.frozen for s in frozen.segs("sig", la)), 1)
        self.assertEqual(frozen.chainset("sig", la).frozen, 1)

    def test_terminal_contract_freezes_neck_zone(self):
        def extra(b, h):
            h["pad"]("U1", "6", (4, 2), "sig4")
            h["pad"]("U2", "6", (26, 2), "sig4")
            h["path"]([(4, 2), (4.5, 2), (8, 2), (26, 2)], "sig4")

        fab = dict(
            via_drill_mm=0.3,
            via_diameter_mm=0.45,
            min_via_plating_um=18,
            board_thickness_mm=1.6,
            copper_resistivity_ohm_mm=2.1e-05,
            via_barrel_loss_budget_w=0.01,
            via_array_peak_drop_v=0.01,
            hole_clearance_mm=0.2,
            track_width_mm=0.2,
            outer_copper_oz=1.0,
            inner_copper_oz=0.4342857142857143,
            delta_t_c=40,
            neck_loss_budget_w=0.01,
            neck_peak_drop_v=0.005,
        )
        base = json.loads(Path(self.board()[1]).read_text())
        intent = dict(
            scope="terminal",
            ref="U1",
            pads=["6"],
            net="sig4",
            rms_current_a=0.1,
            peak_current_a=0.1,
            neck_max_length_mm=0.5,
        )
        path, rules_path = self.board(
            extra, dict(base, electrical_fab=fab, current_intents=[intent]), name="terminal"
        )
        m = self.model(path, rules_path)
        la = m.k.F_Cu
        frozen = sorted(s.a for s in m.segs("sig4", la) if s.frozen)
        # touching U1.6, and within its 0.5 mm neck zone; the 18 mm run beyond stays editable
        self.assertEqual(frozen, [(4 * MM, 2 * MM), (4500000, 2 * MM)])
        self.assertFalse(any(s.frozen for s in m.segs("sig", la)))

    # ------------------------------------------------------------ transactions
    def test_trial_applies_batch_and_keeps_native_facts(self):
        from pnr import gloss

        path, rules = self.board()
        inv = self.plan_all(path, rules)
        # one edit per net (the controller's batches never hold two edits of one net)
        specs = (
            inv["normalize"]["specs"]
            + [s for s in inv["gloss"]["specs"] if s["net"] == "sig"]
            + inv["corridor"]["specs"]
        )
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(dict(step="mixed", specs=specs)))
        out = self.root / "trial.kicad_pcb"
        base = self.run_worker(worker="facts", board=path, rules=rules)
        trial = self.run_worker(worker="trial", board=path, rules=rules, spec=spec, out=out)
        self.assertEqual(trial["dropped"], {})  # the Oracle agrees with the planner
        self.assertEqual(sorted(trial["accepted_specs"]), sorted(s["id"] for s in specs))
        facts = trial["facts"]
        self.assertEqual(
            sorted(map(sorted, facts["partition"])), sorted(map(sorted, base["partition"]))
        )
        self.assertEqual(facts["entries"], base["entries"])
        cor = trial["applied"][inv["corridor"]["specs"][0]["id"]]["quality"]
        self.assertLess(cor["DS_new"]["DS"], cor["DS"]["DS"])  # the dead sliver is gone
        m = self.model(out, rules)
        la = m.k.F_Cu
        chains = m.chainset("sig", la).chains
        self.assertEqual(
            [list(p) for p in chains[0].legs],
            next(s for s in inv["gloss"]["specs"] if s["net"] == "sig")["new_nm"],
        )
        self.assertEqual(len(m.groups["sig2", la]), 1)  # four collinear pieces merged
        self.assertEqual(len(gloss.read(spec)["specs"]), 3)
        self.assertEqual(len({n for x in specs for n in x["nets"]}), 3)

    def test_stale_spec_is_dropped(self):
        path, rules = self.board()
        spec = copy.deepcopy(
            next(s for s in self.plan_all(path, rules)["gloss"]["specs"] if s["net"] == "sig")
        )
        spec["old_segments_nm"][0][3] += 1  # width differs from the board
        (self.root / "stale.json").write_text(json.dumps(dict(step="gloss", specs=[spec])))
        trial = self.run_worker(
            worker="trial",
            board=path,
            rules=rules,
            spec=self.root / "stale.json",
            out=self.root / "stale.kicad_pcb",
        )
        self.assertEqual(trial["dropped"], {spec["id"]: "stale:geometry"})
        self.assertFalse((self.root / "stale.kicad_pcb").exists())

    def crafted(self, extra, new_path, name):
        path, rules = self.board(extra, name=name)
        spec = copy.deepcopy(
            next(
                s
                for s in self.run_worker(worker="inventory", board=path, rules=rules, step="gloss")[
                    "specs"
                ]
                if s["net"] == "sig"
            )
        )
        spec["new_nm"] = [[round(x * MM), round(y * MM)] for x, y in new_path]
        (self.root / (name + ".json")).write_text(json.dumps(dict(step="gloss", specs=[spec])))
        return (
            self.run_worker(
                worker="trial",
                board=path,
                rules=rules,
                spec=self.root / (name + ".json"),
                out=self.root / (name + ".kicad_pcb"),
            ),
            spec,
        )

    def test_new_same_net_contact_is_refused(self):
        def extra(b, h):
            h["pad"]("U3", "1", (14.5, 8.6), "sig")  # a same-net pad off the chain

        trial, spec = self.crafted(
            extra, [(4, 10), (5.4, 8.6), (21.4, 8.6), (22.8, 10), (26, 10)], "contact"
        )
        self.assertEqual(trial["dropped"], {spec["id"]: "L2:contact"})

    def test_pair_guard_refuses_approach(self):
        from pnr.fab_profile import apply_rules

        def extra(b, h):
            h["path"]([(8, 7.9), (20, 7.9)], "Dp")
            h["path"]([(8, 7.45), (20, 7.45)], "Dn")

        path, rules_path = self.board(extra, name="g1")
        rules = apply_rules(
            dict(
                json.loads(rules_path.read_text()),
                diff_pairs=[dict(name="pair0", p="Dp", n="Dn", gap_mm=0.15)],
            )
        )
        rules_path.write_text(json.dumps(rules))
        spec = copy.deepcopy(
            next(
                s
                for s in self.run_worker(
                    worker="inventory", board=path, rules=rules_path, step="gloss"
                )["specs"]
                if s["net"] == "sig"
            )
        )
        spec["new_nm"] = [
            [round(x * MM), round(y * MM)]
            for x, y in [(4, 10), (5.6, 8.4), (21.6, 8.4), (23.2, 10), (26, 10)]
        ]
        (self.root / "g1.json").write_text(json.dumps(dict(step="gloss", specs=[spec])))
        trial = self.run_worker(
            worker="trial",
            board=path,
            rules=rules_path,
            spec=self.root / "g1.json",
            out=self.root / "g1.kicad_pcb",
        )
        self.assertEqual(trial["dropped"], {spec["id"]: "L7:G1:pair0"})

    def test_normalize_union_guard(self):
        path, rules = self.board()
        spec = copy.deepcopy(
            self.run_worker(worker="inventory", board=path, rules=rules, step="normalize")["specs"][
                0
            ]
        )
        uid, (a, b) = next(iter(spec["modify"].items()))
        spec["modify"][uid] = [
            a,
            [b[0] + 2 * MM if b[0] > a[0] else b[0] - 2 * MM, b[1]],
        ]  # copper would change
        (self.root / "n.json").write_text(json.dumps(dict(step="normalize", specs=[spec])))
        trial = self.run_worker(
            worker="trial",
            board=path,
            rules=rules,
            spec=self.root / "n.json",
            out=self.root / "n.kicad_pcb",
        )
        self.assertEqual(trial["dropped"], {spec["id"]: "N:union"})

    # ------------------------------------------------------------ freeze sets, L2 exemptions, budgets
    def test_chain_anchored_just_outside_a_via_is_dekinked(self):
        # router lattice: the track ends 0.25 mm from the via centre, outside the 0.225 mm via
        # disc but touching it with its end cap; every new first leg keeps touching the via
        def extra(b, h):
            k = h["k"]
            v = k.PCB_VIA(b)
            v.SetPosition(h["vec"](8, 1.5))
            v.SetViaType(k.VIATYPE_THROUGH)
            v.SetLayerPair(k.F_Cu, k.B_Cu)
            v.SetWidth(450000)
            v.SetDrill(200000)
            v.SetNetCode(h["net"]("sv"))
            b.Add(v)
            h["pad"]("U3", "1", (12.6, 2.5), "sv")
            h["path"](
                [
                    (8, 1.75),
                    (8.5, 1.75),
                    (8.75, 2.0),
                    (9.25, 2.0),
                    (9.5, 2.25),
                    (10, 2.25),
                    (10.25, 2.5),
                    (12.5, 2.5),
                ],
                "sv",
            )

        path, rules = self.board(extra, name="via")
        m = self.model(path, rules)
        la = m.k.F_Cu
        (ch,) = m.chainset("sv", la).chains
        via = next(x for x in m.terminals("sv", la) if x.GetClass() == "PCB_VIA")
        from pnr import gloss

        self.assertNotIn(gloss.uid(via), m.anchor_items("sv", la, (ch.points[0], ch.points[-1])))
        self.assertIn(gloss.uid(via), m.touched_terminals("sv", la, ch.points, ch.width))
        ctx = m.context("sv", la, ch, m.chainset("sv", la))
        self.assertTrue(
            ctx.contact((8 * MM, 1750000), (11 * MM, 1750000)) is False
        )  # kept contact is allowed
        specs = self.run_worker(worker="inventory", board=path, rules=rules, step="dekink")["specs"]
        spec = next(s for s in specs if s["net"] == "sv")
        self.assertLess(spec["metrics"]["B_all_new"], spec["metrics"]["B_all"])
        self.assertAlmostEqual(spec["metrics"]["L_new"], spec["metrics"]["L"], places=5)
        # the trial worker re-checks L2 on the applied board with the same rule
        (self.root / "via.json").write_text(json.dumps(dict(step="dekink", specs=[spec])))
        trial = self.run_worker(
            worker="trial",
            board=path,
            rules=rules,
            spec=self.root / "via.json",
            out=self.root / "via.kicad_pcb",
        )
        self.assertEqual(trial["dropped"], {})
        self.assertEqual(trial["accepted_specs"], [spec["id"]])

    def open_net(self, b, h):
        # net 'on': a routed staircase from U4.1 that stops short of U4.2 (one DRC open)
        h["pad"]("U4", "1", (20, 1.5), "on")
        h["pad"]("U4", "2", (28, 3.0), "on")
        h["path"]([(20, 1.5), (21, 1.5), (21.5, 2.0), (22, 2.0), (22.5, 2.5), (24, 2.5)], "on")

    def drc_naming(self, path, track_index):
        from pnr import gloss
        from pnr.fab_profile import load_board

        b = load_board(path)
        tracks = sorted(
            (t for t in b.GetTracks() if t.GetNetname() == "on"),
            key=lambda t: (t.GetStart().x, t.GetEnd().x),
        )
        pad = next(
            p
            for f in b.GetFootprints()
            for p in f.Pads()
            if p.GetNetname() == "on" and p.GetNumber() == "2"
        )
        t = tracks[track_index]
        return dict(
            violations=[],
            unconnected_items=[
                dict(
                    type="unconnected_items",
                    items=[
                        dict(uuid=gloss.uid(t), description="Track [on] on F.Cu, length 0.5 mm"),
                        dict(uuid=gloss.uid(pad), description="Pad 2 [on] of U4 on F.Cu"),
                    ],
                )
            ],
        )

    def test_frozen_and_guard_sets_do_not_depend_on_named_items(self):
        path, rules = self.board(self.open_net, name="open")
        r1, r2 = self.drc_naming(path, 0), self.drc_naming(path, -1)
        self.assertNotEqual(r1, r2)
        m1, m2 = self.model(path, rules, drc=r1), self.model(path, rules, drc=r2)
        la = m1.k.F_Cu
        self.assertEqual(
            (m1.frozen_ids, m1.open_pads, m1.open_nets), (m2.frozen_ids, m2.open_pads, m2.open_nets)
        )
        self.assertEqual(m1.open_nets, {"on"})
        self.assertTrue(all(s.frozen for s in m1.segs("on", la)))  # the whole open net
        self.assertEqual(len(m1.open_pads), 2)  # every pad, not the named one
        # a description-only report (uuids unknown to this board) gives the same sets
        r3 = json.loads(
            json.dumps(r1)
            .replace(r1["unconnected_items"][0]["items"][0]["uuid"], "gone")
            .replace(r1["unconnected_items"][0]["items"][1]["uuid"], "gone2")
        )
        self.assertEqual(self.model(path, rules, drc=r3).frozen_ids, m1.frozen_ids)
        # the inventory is identical for reports that differ only in the items KiCad named
        (self.root / "r1.json").write_text(json.dumps(r1))
        (self.root / "r2.json").write_text(json.dumps(r2))
        for step in ("normalize", "dekink", "gloss", "corridor"):
            a = self.run_worker(
                worker="inventory", board=path, rules=rules, step=step, drc=self.root / "r1.json"
            )
            b = self.run_worker(
                worker="inventory", board=path, rules=rules, step=step, drc=self.root / "r2.json"
            )
            self.assertEqual(a["specs"], b["specs"], step)
            self.assertFalse(any("on" in s["nets"] for s in a["specs"]), step)
        # without a report 'on' is editable; with either report the trial refuses its edit
        free = [
            s
            for s in self.run_worker(worker="inventory", board=path, rules=rules, step="dekink")[
                "specs"
            ]
            if s["net"] == "on"
        ]
        self.assertEqual(len(free), 1)
        (self.root / "on.json").write_text(json.dumps(dict(step="dekink", specs=free)))
        for report in ("r1.json", "r2.json"):
            trial = self.run_worker(
                worker="trial",
                board=path,
                rules=rules,
                spec=self.root / "on.json",
                out=self.root / ("on-%s.kicad_pcb" % report),
                drc=self.root / report,
            )
            self.assertEqual(trial["dropped"], {free[0]["id"]: "ineligible:frozen"})

    def test_open_net_copper_guard_before_refinement(self):
        path, rules = self.board(self.open_net, name="guard")
        drc = self.drc_naming(path, 0)
        m = self.model(path, rules, drc=drc)
        la = m.k.F_Cu
        box = (19 * MM, 0, 26 * MM, 4 * MM)
        pads_only = [x for x in m.guards("sig", la, 200000, box) if x.name == "G2"]
        self.assertEqual(len(pads_only[0].shapes), 1)  # U4.1 (U4.2 is out of reach)
        g = self.model(path, rules, drc=drc, guard_open_nets=True)
        (g2,) = [x for x in g.guards("sig", la, 200000, box) if x.name == "G2"]
        self.assertEqual(len(g2.shapes), 1 + len(m.groups["on", la]))  # + every 'on' track

    def test_nothing_reads_the_pre_apply_board_after_a_corridor_apply(self):
        # apply_spec deletes the members' old tracks (board.Delete invalidates their wrappers),
        # so the pre-apply Corridor and grids may not be consulted after the apply.
        from pnr import gloss

        path, rules = self.board()
        (spec,) = self.run_worker(worker="inventory", board=path, rules=rules, step="corridor")[
            "specs"
        ]
        model0 = self.model(path, rules)
        pre = gloss.corridor_before(model0, spec)
        self.assertEqual(pre["E_after"], pre["cor"].excess(pre["after"], pre["window"]))

        def stale(*a, **kw):
            raise AssertionError("the pre-apply board was read after the apply")

        pre["cor"].excess = stale
        for grid in list(model0._item_grid.values()) + list(model0._grids.values()):
            grid.query = stale
        applied, alive = gloss.apply_spec(model0, spec)
        self.assertEqual(len(applied["removed"]), len(spec["members"][0]["ops"]["remove"]))
        model0.b.BuildConnectivity()
        model1 = gloss.Model(model0.b, json.loads(Path(rules).read_text()), path=path, base=model0)
        self.assertIsNone(gloss.recheck(model1, spec, applied))
        q = gloss.corridor_after(model1, spec, pre)
        self.assertLess(q["dX_mm2"], 0)
        self.assertEqual(q["E_new_mm2"], pre["E_after"] / MM / MM)

    def test_derived_groups_from_rule_net_classes(self):
        # PNR_GLOSS_CLASSES_FROM=netclasses: c1 and c2 in one rules net class are one group
        # (packing unlimited); a groups file entry wins for its net (c1 moves to its own group)
        path, rules = self.board()
        data = json.loads(rules.read_text())
        data["net_classes"] = [dict(name="bus", nets=["c1", "c2"])]
        rules.write_text(json.dumps(data))
        corridor = dict(worker="inventory", board=path, rules=rules, step="corridor")
        none = self.run_worker(**corridor)["specs"]
        self.assertEqual(self.run_worker(classes_from="netclasses", **corridor)["specs"], none)
        m = self.model(path, rules, classes_from=("netclasses",))
        self.assertEqual(m.info("c1", m.k.F_Cu)["tag"], "netclass:bus")
        self.assertIsNone(m.allowed_nm("c1", "c2"))
        self.assertEqual(m.allowed_nm("c1", "sig"), 10 * MM)
        own = self.root / "own.json"
        own.write_text(json.dumps({"classes": {"alone": ["c1"]}}))
        apart = self.run_worker(classes_from="netclasses", classes=str(own), **corridor)
        self.assertEqual(apart["specs"], [])
        self.assertGreater(apart["stats"]["cap_refused"], 0)

    def test_functional_groups_cap_cross_group_packing(self):
        # gloss_fixture: corridor packs c1's 11.5 mm leg onto c2, leaving ~11.9 mm of parallel run at
        # minimum pitch (owner decision 2026-09-30: unlimited within a group, 10 mm across groups)
        from pnr import gloss

        path, rules = self.board()
        split, same = self.root / "split.json", self.root / "same.json"
        split.write_text(
            json.dumps(
                {"classes": {"bus": {"nets": ["c1"], "provenance": ["test"]}, "ctl": ["c2"]}}
            )
        )
        same.write_text(json.dumps({"classes": {"bus": ["c1", "c2"]}}))

        def corridor(**kw):
            return self.run_worker(
                worker="inventory", board=path, rules=rules, step="corridor", **kw
            )

        none = corridor()["specs"]
        together = corridor(classes=str(same))["specs"]
        self.assertEqual([m["net"] for s in none for m in s["members"]], ["c1"])
        self.assertEqual(together, none)  # one group: unlimited, as without groups
        apart = corridor(classes=str(split))
        self.assertEqual(apart["specs"], [])  # two groups: the 11.9 mm run is capped
        self.assertGreater(apart["stats"]["cap_refused"], 0)
        wide = corridor(classes=str(split), cross_group_mm=15.0)["specs"]
        self.assertEqual([m["net"] for s in wide for m in s["members"]], ["c1"])
        m = self.model(path, rules, classes=str(split))
        la = m.k.F_Cu
        self.assertEqual(m.info("c1", la)["tag"], "bus")
        self.assertEqual(
            m.info("c1", la)["key"], m.info("c2", la)["key"]
        )  # groups never split the class
        self.assertEqual(m.allowed_nm("c1", "c2"), 10 * MM)
        self.assertIsNone(self.model(path, rules, classes=str(same)).allowed_nm("c1", "c2"))
        self.assertEqual(m.allowed_nm("c1", "sig"), 10 * MM)  # grouped vs undeclared
        # the trial re-checks the cap on the applied board: the one-group plan is dropped under two groups
        (self.root / "cor.json").write_text(json.dumps(dict(step="corridor", specs=none)))
        dropped = self.run_worker(
            worker="trial",
            board=path,
            rules=rules,
            spec=self.root / "cor.json",
            out=self.root / "cap.kicad_pcb",
            classes=str(split),
        )
        self.assertEqual(dropped["dropped"], {none[0]["id"]: "cap:c1|c2"})
        kept = self.run_worker(
            worker="trial",
            board=path,
            rules=rules,
            spec=self.root / "cor.json",
            out=self.root / "kept.kicad_pcb",
            classes=str(same),
        )
        self.assertEqual(kept["accepted_specs"], [none[0]["id"]])
        self.assertNotIn("c1|c2", kept["facts"]["cross_group"])  # one group: not a cross-group row
        after = self.model(self.root / "kept.kicad_pcb", rules, classes=str(split))
        self.assertGreater(after.pair_total("c1", "c2"), 11 * MM)
        self.assertEqual(after.cross_group_rows()["c1|c2"][1], 10.0)
        # facts (B_0) and metrics report the cross-group rows; the gate refuses an increase over the cap
        facts = self.run_worker(worker="facts", board=path, rules=rules, classes=str(split))
        self.assertNotIn("c1|c2", facts["cross_group"])
        self.assertIn(
            "cross_group_cap",
            gloss.compare(
                dict(violations=[], unconnected_items=[]),
                facts,
                dict(violations=[], unconnected_items=[]),
                dict(facts, cross_group=after.cross_group_rows()),
            )[1],
        )
        metrics = self.run_worker(
            worker="metrics", board=self.root / "kept.kicad_pcb", rules=rules, classes=str(split)
        )["metrics"]["cross_group"]
        self.assertEqual(metrics["max_pair"], ["c1", "c2"])
        self.assertEqual([r["nets"] for r in metrics["over_cap"]], [["c1", "c2"]])
        # dekink and gloss plan with the cap on the fixture (no cross-group run grows)
        for step in ("dekink", "gloss"):
            specs = self.run_worker(
                worker="inventory", board=path, rules=rules, step=step, classes=str(split)
            )["specs"]
            self.assertEqual(
                [s["id"] for s in specs],
                [
                    s["id"]
                    for s in self.run_worker(
                        worker="inventory", board=path, rules=rules, step=step
                    )["specs"]
                ],
                step,
            )

    # ------------------------------------------------------------ metrics
    def test_dead_space_channel_matches_analytic(self):
        def extra(b, h):
            h["path"]([(5, 2.0), (25, 2.0)], "wa")
            h["path"]([(5, 2.6), (25, 2.6)], "wb")
            h["path"]([(5, 3.6), (25, 3.6)], "wc")

        path, rules = self.board(extra, name="ds")
        m = self.model(path, rules)
        la = m.k.F_Cu
        w, c = 200000, 127000
        narrow = m.dead_space(la, w, c, (10 * MM, 2.1 * MM, 20 * MM, 2.5 * MM))
        width = 0.6 - 0.2 - 2 * 0.128  # 0.144 mm < w: all dead
        self.assertAlmostEqual(narrow["F"], 10 * width, places=3)
        self.assertAlmostEqual(narrow["DS"], 10 * width, places=3)
        wide = m.dead_space(la, w, c, (10 * MM, 2.7 * MM, 20 * MM, 3.5 * MM))
        width = 1.0 - 0.2 - 2 * 0.128  # 0.544 mm >= w: all usable
        self.assertAlmostEqual(wide["F"], 10 * width, places=3)
        self.assertAlmostEqual(wide["DS"], 0.0, places=3)

    def test_metrics(self):
        path, rules = self.board()
        m = self.run_worker(worker="metrics", board=path, rules=rules)["metrics"]
        self.assertAlmostEqual(m["layers"]["F.Cu"]["E_mm2"], 2.553, places=3)
        self.assertEqual(m["layers"]["F.Cu"]["corridor_pairs"], 1)
        self.assertGreater(m["classes"]["eligible"]["bends_total"], 10)
        self.assertGreater(m["DS_mm2"], 1.0)  # the c1/c2 sliver
        # ray metric, both sides sampled: c1's 11.5 mm bottom leg faces c2 at 0.35 mm, and sig's 1 mm
        # leg at y = 12 faces sig2 at 1.8 mm. Rays reaching the round end caps add ~0.36 mm^2: a ray
        # through a neighbour's vertex, where its parallel leg meets a turning one, counts the
        # neighbour (c2 under c1's corner and sig2 under sig's, 0.19 mm^2 of it), whichever way
        # either track is drawn.
        expected = 2 * 11.5 * (0.35 - 0.128) + 2 * 1.0 * (1.8 - 0.128)
        self.assertAlmostEqual(m["layers"]["F.Cu"]["X_mm2"], expected, delta=0.4)
        self.assertEqual(m["layers"]["F.Cu"]["T_mm"], 0)
        e = m["classes"]["eligible"]
        self.assertGreaterEqual(e["bends_all"], e["bends_total"])
        self.assertLessEqual(m["A3_mm2"], m["A2_mm2"])
        self.assertLessEqual(m["A2_mm2"], m["US_mm2"])


if __name__ == "__main__":
    unittest.main()
