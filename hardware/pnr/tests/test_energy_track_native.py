"""Actual headless worker/controller tests for the opt-in energy integration."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr import gloss

KP = os.environ.get("PNR_KICAD_PYTHON")
CLI = os.environ.get("PNR_KICAD_CLI")
READY = bool(KP and CLI and Path(KP).exists() and Path(CLI).exists())
HERE = Path(__file__).resolve().parent


@unittest.skipUnless(READY, "requires headless PNR_KICAD_PYTHON and PNR_KICAD_CLI")
class EnergyNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [str(HERE.parent), str(HERE), str(HERE.parents[2])] + sys.path
            ),
        )
        subprocess.run(
            [KP, str(HERE / "gloss_fixture.py"), str(self.root / "input")],
            check=True,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        self.board = self.root / "input/board.kicad_pcb"
        self.rules = self.root / "input/rules.json"

    def controller(self, energy, steps):
        args = argparse.Namespace(
            board=self.board,
            rules=self.rules,
            annotation_source=[],
            out=self.root / "out/board.kicad_pcb",
            work_dir=self.root / "work",
            report=self.root / "out/result.json",
            kicad_cli=CLI,
            kicad_python=KP,
            label="energy-native",
            seconds=180,
            metrics=False,
        )
        conf = gloss.Settings(
            steps=steps,
            passes=gloss.LABELS,
            si=False,
            cycle=False,
            seconds=180,
            max_transactions=4,
            batch=1,
            hug=False,
            sweeps=1,
            energy=energy,
        )
        return gloss.GlossPass(args, conf)

    def test_actual_engine_accepts_eligible_energy_improvement(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = self.controller(True, ("gloss",)).run()
        self.assertGreater(result["accepted_transactions"], 0)
        self.assertTrue(result["end_gate"]["passed"])
        self.assertEqual(result["objective_before"], result["objective_after"])
        accepted = [r for r in result["transactions"] if r["accepted"]]
        self.assertTrue(any(r.get("delta", {}).get("bends", 0) < 0 for r in accepted))
        inv = json.loads((self.root / "work/inventory/gloss-0.json").read_text())
        self.assertTrue(any(s.get("energy") for s in inv["specs"]))

    def test_default_off_noop_is_byte_identical(self):
        # A freshly constructed straight, connected fixture runs the actual
        # gloss step. No nets/copper are deleted to manufacture a no-op result.
        program = r"""
from pathlib import Path
import pcbnew as k
from gloss_fixture import rules
from pnr.writeback import patch_project_rules
path=Path(__import__('sys').argv[1]);b=k.BOARD();b.SetCopperLayerCount(4)
def vec(x,y):return k.VECTOR2I(round(x*1e6),round(y*1e6))
net=k.NETINFO_ITEM(b,'straight');b.Add(net)
for a,z in [((0,0),(30,0)),((30,0),(30,20)),((30,20),(0,20)),((0,20),(0,0))]:
 s=k.PCB_SHAPE(b);s.SetShape(k.SHAPE_T_SEGMENT);s.SetLayer(k.Edge_Cuts);s.SetWidth(100000);s.SetStart(vec(*a));s.SetEnd(vec(*z));b.Add(s)
for ref,x in [('U1',4),('U2',26)]:
 f=k.FOOTPRINT(b);f.SetReference(ref);f.Reference().SetVisible(False);b.Add(f)
 p=k.PAD(f);p.SetNumber('1');p.SetShape(k.PAD_SHAPE_RECT);p.SetSize(vec(.6,.6));p.SetPosition(vec(x,10));p.SetAttribute(k.PAD_ATTRIB_SMD)
 layers=k.LSET();layers.AddLayer(k.F_Cu);p.SetLayerSet(layers);p.SetNetCode(net.GetNetCode());f.Add(p)
t=k.PCB_TRACK(b);t.SetStart(vec(4,10));t.SetEnd(vec(26,10));t.SetWidth(200000);t.SetLayer(k.F_Cu);t.SetNetCode(net.GetNetCode());b.Add(t)
k.SaveBoard(str(path),b);patch_project_rules(str(path.with_suffix('.kicad_pro')),rules())
"""
        subprocess.run(
            [KP, "-c", program, str(self.board)],
            check=True,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        before = self.board.read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = self.controller(False, ("gloss",)).run()
        self.assertEqual(result["accepted_transactions"], 0)
        self.assertEqual((self.root / "out/board.kicad_pcb").read_bytes(), before)
        self.assertNotIn(
            "energy", json.loads((self.root / "work/checkpoint-000/facts.json").read_text())
        )

    def test_uncontracted_plane_dependency_refused_before_apply(self):
        program = r"""
import argparse,json,time
from pathlib import Path
import pcbnew as k
from pnr import gloss
from pnr.fab_profile import load_board
root=Path(__import__('sys').argv[1]);board=root/'input/board.kicad_pcb';rules=json.loads((root/'input/rules.json').read_text())
b=load_board(board)
n=k.NETINFO_ITEM(b,'uncontracted_plane');b.Add(n)
z=k.ZONE(b);z.SetLayer(k.F_Cu);z.SetNetCode(n.GetNetCode());z.Outline().NewOutline()
for x,y in [(1,1),(2,1),(2,2),(1,2)]:z.Outline().Append(int(x*1e6),int(y*1e6))
b.Add(z);b.BuildConnectivity();k.SaveBoard(str(board),b)
model=gloss.Model(b,rules,path=board,drc=None)
model.oracle
specs,stats=gloss.plan(model,'gloss',energy=True,deadline=time.monotonic()+30)
assert specs,stats
spec=specs[0];spec['energy']['dependent_zones']=[gloss.uid(z)]
(root/'spec.json').write_text(json.dumps({'specs':[spec]}))
"""
        subprocess.run(
            [KP, "-c", program, str(self.root)],
            check=True,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        before = hashlib.sha256(self.board.read_bytes()).hexdigest()
        report = self.root / "refused.json"
        out = self.root / "refused.kicad_pcb"
        subprocess.run(
            [
                KP,
                "-m",
                "pnr.gloss",
                str(self.board),
                "--rules",
                str(self.rules),
                "--worker",
                "trial",
                "--energy",
                "--spec",
                str(self.root / "spec.json"),
                "--report",
                str(report),
                "--out",
                str(out),
            ],
            check=True,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        row = json.loads(report.read_text())
        self.assertIn("energy:protected_or_uncontracted_plane", row["dropped"].values())
        self.assertFalse(row.get("accepted_specs"))
        self.assertFalse(out.exists())
        self.assertEqual(hashlib.sha256(self.board.read_bytes()).hexdigest(), before)

    def test_full_native_metadata_and_receipt_mutations_are_detected(self):
        program = r"""
import copy
import time
from pathlib import Path
import pcbnew as k
from gloss_fixture import build, rules
from pnr import gloss, energy_track
b,h=build()
z=k.ZONE(b)
z.SetLayer(k.F_Cu)
z.SetNetCode(h['net']('plane'))
z.Outline().NewOutline()
for x,y in [(1,1),(2,1),(2,2),(1,2)]:
 z.Outline().Append(round(x*1e6),round(y*1e6))
b.Add(z)
def changed(change, restore):
 before=energy_track.immutable(b)
 change()
 assert energy_track.immutable(b)!=before
 restore()
 assert energy_track.immutable(b)==before
priority=z.GetAssignedPriority()
changed(lambda:z.SetAssignedPriority(priority+1),lambda:z.SetAssignedPriority(priority))
clearance=z.GetLocalClearance()
changed(lambda:z.SetLocalClearance(clearance+10000),lambda:z.SetLocalClearance(clearance))
thermal=z.GetThermalReliefGap()
changed(lambda:z.SetThermalReliefGap(thermal+10000),lambda:z.SetThermalReliefGap(thermal))
footprint=list(b.GetFootprints())[0]
changed(lambda:footprint.SetLocked(True),lambda:footprint.SetLocked(False))
settings=b.GetDesignSettings()
thickness=settings.GetBoardThickness()
changed(lambda:settings.SetBoardThickness(thickness+10000),lambda:settings.SetBoardThickness(thickness))
minimum=settings.m_MinClearance
changed(lambda:setattr(settings,'m_MinClearance',minimum+1000),lambda:setattr(settings,'m_MinClearance',minimum))
arc=k.PCB_ARC(b)
arc.SetStart(h['vec'](3,3))
arc.SetMid(h['vec'](4,4))
arc.SetEnd(h['vec'](5,3))
arc.SetLayer(k.F_Cu)
arc.SetWidth(200000)
arc.SetNetCode(h['net']('arc'))
arc.SetLocked(True)
b.Add(arc)
changed(lambda:arc.SetMid(h['vec'](4,4.1)),lambda:arc.SetMid(h['vec'](4,4)))
b.BuildConnectivity()
model=gloss.Model(b,rules(),drc=None)
model.oracle
specs,_=gloss.plan(model,'gloss',energy=True,deadline=time.monotonic()+30)
assert specs
spec=specs[0]
assert energy_track.dependencies(model,spec) is None
wrong=copy.deepcopy(spec)
wrong['ops']['width']+=1
assert energy_track.dependencies(model,wrong)=='energy:width_invariant'
wrong=copy.deepcopy(spec)
wrong['ops']['keep'].append(['invented',[0,0],[1,1],200000])
assert energy_track.dependencies(model,wrong)=='energy:operation_scope'
wrong=copy.deepcopy(spec)
other=next(t for t in b.GetTracks() if gloss.uid(t) not in {r[0] for r in spec['old_segments_nm']})
wrong['ops']['remove'].append(gloss.uid(other))
assert energy_track.dependencies(model,wrong)=='energy:unrelated_removal'
# Full non-owned copper serialization detects arc metadata, even if its endpoints agree.
before=energy_track.track_snapshot(b)
arc.SetMid(h['vec'](4,4.1))
assert before!=energy_track.track_snapshot(b)
"""
        subprocess.run(
            [KP, "-c", program],
            check=True,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )


if __name__ == "__main__":
    unittest.main()
