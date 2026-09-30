"""Independent assertions for circuit intent and the fail-closed acceptance gate."""
import copy,json,re,subprocess,sys,tempfile,unittest
from collections import Counter
from pathlib import Path
from designs import designs
from run import LISTING,acceptance,engine_revision,new_result,parser,sources_digest

class CircuitContract(unittest.TestCase):
 def test_ladder_and_multiterminal_networks(self):
  cases=designs();self.assertEqual([len(c['parts']) for c in cases],[2,3,5,8,10,14,20,20])
  self.assertEqual([c['constraints']['board']['layers'] for c in cases],[2]*7+[4])
  for c in cases:
   with self.subTest(c=c['name']):
    self.assertEqual(len({p['ref'] for p in c['parts']}),len(c['parts']))
    counts=Counter(n for p in c['parts'] for n in p['pins'].values() if n)
    self.assertTrue(all(v>=2 for v in counts.values()),counts)
    self.assertEqual(set(c['constraints']['fixed']),{'J1'})
 def test_timer_and_counter_pin_contract(self):
  for c in designs()[4:]:
   parts={p['ref']:p for p in c['parts']};timer=parts['U1']['pins']
   self.assertEqual(timer,{'1':'GND','2':'TIMING','3':'CLOCK','4':'VCC','5':'CONTROL','6':'TIMING','7':'DISCHARGE','8':'VCC'})
   if 'U2' in parts:
    counter=parts['U2']['pins'];self.assertEqual([counter[str(i)] for i in (8,13,14,15,16)],['GND','GND','CLOCK','RESET','VCC'])
    self.assertEqual(counter['4' if len(parts)==14 else '1'],'RESET')
    self.assertEqual(parts['C4']['pins'],{'1':'VCC','2':'GND'})
 def test_no_onboard_current_limiter_only_for_external_current_source(self):
  c=designs()[0];self.assertIn('current source',c['description'])
  for c in designs()[1:]:self.assertTrue(any(p['ref'].startswith('R') for p in c['parts']))

class GateContract(unittest.TestCase):
 def setUp(self):
  self.p=dict(legal=True,converged=True,unrouted=[],deferred=[])
  self.a=dict(netlist_preserved=True,subwidth_tracks=[],pad_entries=[dict(qualified=True)])
  self.d=dict(unconnected_items=[],violations=[])
 def test_clean(self):self.assertEqual(acceptance(self.p,self.a,self.d),[])
 def test_no_native_report_no_pass(self):self.assertIn('invalid_drc_report',acceptance(self.p,self.a,{}))
 def test_native_opens_override_router_success(self):
  self.d['unconnected_items']=[{}];self.assertIn('native_unconnected_items',acceptance(self.p,self.a,self.d))
 def test_every_native_warning_rejected(self):
  for kind in ['shorting_items','clearance','track_dangling','via_dangling','lib_footprint_mismatch']:
   self.d['violations']=[dict(type=kind,severity='warning')];self.assertIn('native_drc_violations',acceptance(self.p,self.a,self.d))
 def test_missing_net_not_hidden_by_zero_opens(self):
  self.a['netlist_preserved']=False;self.assertIn('changed_pin_netlist',acceptance(self.p,self.a,self.d))
 def test_narrow_grazing_contact_rejected(self):
  self.a['pad_entries'][0]['qualified']=False;self.assertIn('unqualified_pad_entry',acceptance(self.p,self.a,self.d))
 def test_power_or_pair_deferral_is_incomplete(self):
  self.p['deferred']=['VCC'];self.assertIn('incomplete_pnr',acceptance(self.p,self.a,self.d))
 def test_undersized_copper_rejected(self):
  self.a['subwidth_tracks']=['track'];self.assertIn('undersized_copper',acceptance(self.p,self.a,self.d))

BOARD = """(kicad_pcb (version 20260206)
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (gr_rect (start 10 20) (end 28 34) (layer "Edge.Cuts"))
  (footprint "LED_SMD:LED_0805_2012Metric" (layer "F.Cu") (at 20 27 90)
    (property "Reference" "D1") (property "Value" "red")
    (pad "1" smd roundrect (at -0.9375 0 90) (size 0.975 1.4) (layers "F.Cu") (roundrect_rratio 0.25)
      (net "GND"))
    (pad "2" smd roundrect (at 0.9375 0 90) (size 0.975 1.4) (layers "F.Cu") (roundrect_rratio 0.25)
      (net "LED_A")))
  (segment (start 20 26) (end 24 26) (width 0.25) (layer "F.Cu") (net "LED_A")))
"""


class RunnerContract(unittest.TestCase):
    def test_trace_is_opt_in(self):
        self.assertFalse(parser().parse_args(["--out", "x"]).trace)
        self.assertTrue(parser().parse_args(["--out", "x", "--trace"]).trace)

    def test_version_listing_needs_no_pip(self):
        out = subprocess.run([sys.executable, "-c", LISTING], capture_output=True, text=True, timeout=120, check=True)
        lines = out.stdout.splitlines()
        self.assertTrue(all(re.match(r"^[^=\s]+==\S+$", line) for line in lines), lines[:3])

    def test_the_ladder_is_judged_under_the_fixtures_own_rules_by_default(self):
        self.assertEqual(parser().parse_args(["--out", "x"]).fab_profile, "legacy")
        self.assertEqual(parser().parse_args(["--out", "x", "--fab-profile", "jlc-pofv"]).fab_profile, "jlc-pofv")
        with self.assertRaises(SystemExit):
            parser().parse_args(["--out", "x", "--fab-profile", "other"])

    def test_route_case_routes_under_the_profile_it_is_judged_by(self):
        source = (Path(__file__).resolve().parent / "route_case.py").read_text()
        self.assertIn("rules=apply_rules(compile_routing_rules(", source)

    def test_sources_digest_and_revision(self):
        a = sources_digest({"b.py": "2", "a.py": "1"})
        self.assertEqual(a, sources_digest({"a.py": "1", "b.py": "2"}))
        self.assertNotEqual(a, sources_digest({"a.py": "1", "b.py": "3"}))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(engine_revision(Path(tmp) / "not-a-checkout"), (None, None))

    def test_case_directories_are_run_relative(self):
        spec = designs()[0]
        result = new_result(spec, 1, Path("somewhere/run") / (spec["name"] + "-seed-1"))
        self.assertEqual(result["directory"], spec["name"] + "-seed-1")
        self.assertFalse(result["passed"])


class NativeTraceContract(unittest.TestCase):
    def test_native_lane_records_the_saved_board_and_the_verdict(self):
        from pnr import trace
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
        from trace_native import NativeTrace

        spec = designs()[0]
        pads = [Pad("1", "GND", (-0.9375, 0.0), (0.975, 1.4)), Pad("2", "LED_A", (0.9375, 0.0), (0.975, 1.4))]
        graph = BoardGraph(
            "led",
            [Component("D1", "LED", (5.0, 5.0), 0.0, "top", (3.0, 1.8), (3.0, 1.8), pads=pads)],
            [Net("LED_A", 1, [("D1", "1"), ("D1", "2")])],
            BoardOutline(18.0, 14.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "trace").mkdir(parents=True)
            (root / "source.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pcb").write_text(BOARD)
            native = NativeTrace(root, spec, 0, parser().parse_args(["--out", "x", "--trace"]), "run")
            self.assertEqual(native.environment(), dict(PNR_TRACE_DIR=str(root / "trace"), PNR_TRACE_LANE="engine"))
            run = json.loads((root / "trace" / "run.json").read_text())
            self.assertEqual((run["subject"]["case"], run["config"]["initial_pool"]), (spec["name"], False))
            recorder = trace.Recorder(root / "trace")
            recorder.begin_board(graph)
            recorder.close()
            drc = dict(unconnected_items=[dict(items=[dict(pos=dict(x=11, y=21)), dict(pos=dict(x=12, y=22))])], violations=[])
            native.finish(root / "routed.kicad_pcb", drc, dict(case=spec["name"], seed=0, passed=False, reasons=["native_unconnected_items"], opens=1, violations={}, vias=0, copper_length_mm=4.0))
            events = [json.loads(line) for line in (root / "trace" / "streams" / "native.jsonl").read_text().splitlines()]
            header = json.loads((root / "trace" / "header.json").read_text())
            self.assertFalse((root / "trace" / "errors.json").exists())
        self.assertEqual([e["kind"] for e in events], ["board", "result"])
        board, result = events
        self.assertEqual((board["stage"], board["drc"]["unconnected"]), ("refill", 1))
        self.assertEqual(board["drc"]["open_pairs"], [[1000, 13000, 2000, 12000]])
        self.assertEqual(board["progress"], dict(done=0, total=1, source="kicad"))
        self.assertEqual((result["passed"], result["opens"], result["rules"]), (False, 1, {}))
        pad = header["components"][0]["pads"][0]
        self.assertEqual((pad["shape"], pad["corner"], header["components"][0]["value"]), ("roundrect", 244, "red"))

    def test_snapshot_judges_with_the_custom_rules_and_keeps_no_library_paths(self):
        from pnr import trace
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
        from trace_native import NativeTrace

        # A stand-in kicad-cli: the DRC report says which of the board's side files it saw.
        cli_source = (
            "import json,sys\nfrom pathlib import Path\n"
            "board=Path(sys.argv[3]);out=Path(sys.argv[sys.argv.index('--output')+1])\n"
            "seen=[s for s in ('.kicad_pro','.kicad_dru') if board.with_suffix(s).is_file()]\n"
            "seen+=['fp-lib-table'] if (board.parent/'fp-lib-table').is_file() else []\n"
            "out.write_text(json.dumps(dict(unconnected_items=[],violations=[dict(type=s) for s in seen])))\n"
        )
        pads = [Pad("1", "GND", (-0.9375, 0.0), (0.975, 1.4)), Pad("2", "LED_A", (0.9375, 0.0), (0.975, 1.4))]
        graph = BoardGraph(
            "led",
            [Component("D1", "LED", (5.0, 5.0), 0.0, "top", (3.0, 1.8), (3.0, 1.8), pads=pads)],
            [Net("LED_A", 1, [("D1", "1"), ("D1", "2")])],
            BoardOutline(18.0, 14.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "trace").mkdir(parents=True)
            cli = Path(tmp) / "kicad-cli"
            cli.write_text("#!" + sys.executable + "\n" + cli_source)
            cli.chmod(0o755)
            (root / "source.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pro").write_text("{}")
            (root / "routed.kicad_dru").write_text("(version 1)\n")
            (root / "fp-lib-table").write_text('(fp_lib_table (lib (name "X") (uri "/abs/X.pretty")))\n')
            native = NativeTrace(root, designs()[0], 0, parser().parse_args(["--out", "x", "--trace"]), "run")
            recorder = trace.Recorder(root / "trace")
            recorder.begin_board(graph)
            recorder.close()
            native.snapshot("writeback", root / "routed.kicad_pcb", str(cli), 60)
            folder = root / "trace" / "native" / "writeback"
            self.assertFalse((root / "trace" / "errors.json").exists())
            self.assertTrue((folder / "board.kicad_dru").is_file())
            self.assertFalse((folder / "fp-lib-table").exists())
            events = [json.loads(line) for line in (root / "trace" / "streams" / "native.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["drc"]["by_type"], {".kicad_pro": 1, ".kicad_dru": 1, "fp-lib-table": 1})

    def test_tracing_errors_never_fail_a_case(self):
        from trace_native import NativeTrace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            native = NativeTrace(root, designs()[0], 0, parser().parse_args(["--out", "x", "--trace"]), "run")
            native.snapshot("writeback", root / "missing.kicad_pcb", "kicad-cli", 5)
            self.assertTrue((root / "trace" / "errors.json").is_file())
            native.finish(root / "missing.kicad_pcb", {}, {})


class LadderResultsContract(unittest.TestCase):
    def test_case_result_names_the_rules_and_holds_no_paths(self):
        from animate_ladder import case_result

        spec = designs()[4]
        drc = dict(unconnected_items=[], violations=[
            dict(type="clearance", description="Clearance violation (rule 'jlc-pofv_via_to_smd_pad' clearance 0.1270 mm; actual 0.0000 mm)"),
            dict(type="clearance", description="Clearance violation (rule 'jlc-pofv_via_to_smd_pad' clearance 0.1270 mm; actual 0.0034 mm)"),
            dict(type="silk_overlap", description="Silkscreen overlap")])
        pnr = dict(converged=True, unrouted=[], deferred=[], initial_pool=dict(selected="start-05"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / (spec["name"] + "-seed-0")
            root.mkdir()
            (root / "design.json").write_text(json.dumps(spec))
            (root / "drc.json").write_text(json.dumps(drc))
            (root / "routed.kicad_dru").write_text("(version 1)\n# Generated by pnr.fab_profile (profile jlc-pofv); notes\n")
            result = dict(new_result(spec, 0, root), reasons=["native_drc_violations"], opens=0, violations={"clearance": 2, "silk_overlap": 1},
                          vias=7, tracks=300, copper_length_mm=120.1234, pnr=pnr, elapsed_seconds=32.44)
            entry = case_result(spec["name"], root, result, ["--initial-pool"])
        self.assertEqual((entry["parts"], entry["nets"], entry["layers"]), (10, 7, 2))
        self.assertEqual(entry["drc_rules"], {"jlc-pofv_via_to_smd_pad": 2, "silk_overlap": 1})
        self.assertEqual((entry["fab_profile"], entry["routed"], entry["passed"]), ("jlc-pofv", True, False))
        self.assertEqual((entry["copper_length_mm"], entry["seconds"], entry["selected_start"]), (120.12, 32.4, "start-05"))
        self.assertNotIn(tmp, json.dumps(entry))

    def test_manifest_provenance_comes_from_the_ladder_run(self):
        from animate_ladder import README_CASES, case_result, ladder_provenance

        self.assertEqual(README_CASES[0], "05-timer-led-10")  # the 555 flasher
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            doc = dict(engine_revision="abc", engine_dirty=False, sources_sha256="f" * 64, fab_profile="legacy",
                       platform="linux-aarch64", arguments=dict(python=tmp + "/venv/bin/python"))
            (run / "provenance.json").write_text(json.dumps(doc))
            info = ladder_provenance(run, image="ghcr.io/example/yapnr@sha256:" + "0" * 64, kicad="10.0.6")
            spec = designs()[0]
            root = run / (spec["name"] + "-seed-0")
            root.mkdir()
            (root / "design.json").write_text(json.dumps(spec))
            entry = case_result(spec["name"], root, dict(new_result(spec, 0, root), passed=True), [], "legacy")
        self.assertEqual((info["engine_revision"], info["engine_dirty"], info["fab_profile"]), ("abc", False, "legacy"))
        self.assertEqual((info["platform"], info["kicad"]), ("linux-aarch64", "10.0.6"))
        self.assertNotIn(tmp, json.dumps(info))
        self.assertEqual(entry["fab_profile"], "legacy")  # no .kicad_dru: the run's profile


if __name__=='__main__':unittest.main()
