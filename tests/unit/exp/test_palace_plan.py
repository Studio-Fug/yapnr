"""tools/exp/palace_plan.py and palace_job.py: Palace models become an mc-eval campaign that runs
in the Palace image (its own interpreter, no yapnr launcher), one model of R MPI ranks per VM by
default; the job runner rewrites the configuration (output, mesh, overrides), checks it, runs the
solve under mpirun, reads Palace's palace.json and log, and compares port-S.csv with a
reference; collect lays fetched results out like local runs."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.exp import palace_job, palace_plan
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import gcp_batch

IMAGE = "us-west4-docker.pkg.dev/example-project/images/palace:b797ea8-pts-x86-64-v3@" + (
    testing.DIGEST
)
# Palace's port-S.csv layout (two ports, as Palace writes it).
PORT_S = (
    "        f (GHz),             |S[1][1]| (dB),        arg(S[1][1]) (deg.),"
    "             |S[2][1]| (dB),        arg(S[2][1]) (deg.)\n"
    " 2.00000000e+00,        -2.304316476605e+01,        -1.119957618551e+02,"
    "        -5.548754594630e-02,        -2.385473049060e+01\n"
    " 8.00000000e+00,        -1.504139718423e+01,        +1.767648544515e+02,"
    "        -1.823959296167e-01,        -9.396177476419e+01\n"
)
# A stand-in Palace: --version, --dry-run CONFIG (checks the mesh exists), and a solve that writes
# what Palace writes (palace.json, port-S.csv, its log lines) into the configuration's output.
FAKE_PALACE = r"""
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("Palace version: v0.18.1-160-gb797ea8\nSchema version: 2-1-0")
    sys.exit(0)
dry = args[0] == "--dry-run"
config = json.load(open(args[-1]))
if not os.path.exists(config["Model"]["Mesh"]):
    print("Unable to open mesh file"); sys.exit(1)
if dry:
    print('Dry-run: No errors detected in configuration file "%s"' % args[-1]); sys.exit(0)
out = config["Problem"]["Output"]
os.makedirs(out, exist_ok=True)
print("\nAssembling system matrices, number of global unknowns:")
print(" Level 0 (p = 1): 1000 unknowns")
print(" Level 0 (auxiliary) (p = 1): 300 unknowns")
print(" Level 1 (p = 2): 4000 unknowns")
print(" Level 2 (p = 3): 9000 unknowns, 50 NNZ")
print("\nAdaptive mesh refinement (AMR) iteration 1:")
print(" Indicator norm = 1.500e-02, global unknowns = 9000")
print("\nAssembling system matrices, number of global unknowns:")
print(" Level 0 (p = 3): 12000 unknowns")
print("\nCompleted 1 iteration of adaptive mesh refinement (AMR):")
print(" Indicator norm = 4.000e-03, global unknowns = 12000")
print("ranks", os.environ.get("FAKE_NP"), "omp", os.environ.get("OMP_NUM_THREADS"))
open(os.path.join(out, "port-S.csv"), "w").write(os.environ["FAKE_PORT_S"])
if (config["Model"].get("Refinement") or {}).get("SaveAdaptMesh"):
    stem = os.path.splitext(os.path.basename(config["Model"]["Mesh"]))[0]
    open(os.path.join(out, stem + ".meshgz"), "w").write("MFEM NC mesh v1.0")
json.dump({"GitTag": "v0.18.1-160-gb797ea8",
           "Problem": {"MPISize": int(os.environ["FAKE_NP"]), "DegreesOfFreedom": 12000,
                       "MultigridDegreesOfFreedom": [3000, 12000], "MeshElements": 2500,
                       "Iteration": 2},
           "LinearSolver": {"TotalSolves": 7, "TotalIts": 140},
           "ElapsedTime": {"Durations": {"Total": 12.5, "Solve": 9.0}},
           "PeakMemoryMegabytes": {"Min": 90.0, "Max": 120.0, "Average": 100.0, "Total": 800.0},
           "PeakNodeMemoryMegabytes": {"Total": 820.0}},
          open(os.path.join(out, "palace.json"), "w"))
if os.environ.get("FAKE_SNAPSHOT") and not os.path.basename(out).startswith("stage-"):
    src, dst = os.environ["FAKE_SNAPSHOT"].split(":")
    open(dst, "w").write(open(src).read())
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""
# A stand-in mpirun: records its arguments, runs the program after the launcher's flags.
FAKE_MPIRUN = r"""
import os, subprocess, sys
args = sys.argv[1:]
open(os.environ["FAKE_MPIRUN_LOG"], "a").write(" ".join(args) + "\n")
with_value = {"-np", "--bind-to", "--map-by"}
i, np = 0, None
while i < len(args) and args[i].startswith("-"):
    if args[i] == "-np":
        np = args[i + 1]
    i += 2 if args[i] in with_value else 1
sys.exit(subprocess.call(args[i:], env=dict(os.environ, FAKE_NP=np)))
"""
CONFIG = """{
  "Problem": {"Type": "Driven", "Output": "postpro/wave"},  // Palace's own output
  "Model": {"Mesh": "mesh/cpw.msh", "L0": 1.0e-6,},
  "Boundaries": {"PEC": {"Attributes": [8-10, 12]}, /* ranges */ "WavePort": [{"Index": 1}]},
  "Solver": {"Order": 2, "Driven": {"Samples": [{"MinFreq": 2.0}]}},
}
"""


def executable(path: Path, script: str) -> Path:
    body = script if script.startswith("#!") else "#!%s\n%s" % (sys.executable, script)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class PalacePlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp)
        (self.tmp / "models" / "mesh").mkdir(parents=True)
        (self.tmp / "models" / "cpw.json").write_text(CONFIG)
        (self.tmp / "models" / "mesh" / "cpw.msh").write_text("$MeshFormat\n")
        self.jobs = self.tmp / "jobs.toml"
        self.jobs.write_text(
            textwrap.dedent(
                """
                name = "palace-smoke"
                image = "%s"
                ranks = 8

                [inputs]
                models = "models"

                [set]
                "Solver.Order" = 2

                [[jobs]]
                id = "cpw-wave-r8"
                config = "/opt/palace/share/palace/examples/cpw/cpw_wave_uniform.json"
                reference = "/opt/palace/share/palace/regression/cpw/wave_uniform"

                [[jobs]]
                id = "cpw-p3"
                config = "models/cpw.json"
                set = {"Solver.Order" = 3, "Model.Refinement.MaxIts" = 4}
                max_wall_s = 3600
                """
                % IMAGE
            )
        )

    def tearDown(self):
        self._tmp.cleanup()

    # --- plan

    def test_models_become_a_campaign_in_the_palace_image(self):
        out = self.tmp / "camp"
        manifest = palace_plan.generate(self.jobs, out, IMAGE)
        self.assertEqual(manifest["jobs"], ["cpw-wave-r8", "cpw-p3"])
        self.assertEqual(
            manifest["placement"], {"families": ["c4d"], "packing": "core", "vm_vcpus": 16}
        )
        lines = [json.loads(x) for x in (out / "stage.jsonl").read_text().splitlines()]
        self.assertEqual(
            lines[0]["command"],
            ["${PYTHON}", "job/palace_job.py", "--id", "cpw-wave-r8", "--ranks", "8"]
            + ["--bind", "core", "--set", "Solver.Order=2"]
            + ["--reference", "/opt/palace/share/palace/regression/cpw/wave_uniform", "--"]
            + ["/opt/palace/share/palace/examples/cpw/cpw_wave_uniform.json"],
        )
        # Campaign-wide sets first, the job's own replace them.
        self.assertEqual(
            lines[1]["command"][8:12],
            ["--set", "Solver.Order=3", "--set", "Model.Refinement.MaxIts=4"],
        )
        self.assertEqual(lines[1]["resources"]["max_wall_s"], 3600)
        self.assertEqual(lines[0]["resources"]["cpus"], 8)
        self.assertEqual(lines[0]["env"]["OMP_NUM_THREADS"], "1")
        self.assertEqual(lines[0]["labels"], {"model": "cpw-wave-r8", "ranks": "8"})
        self.assertIn("cpw-wave-r8/postpro/*.csv", lines[0]["summary"])
        self.assertTrue((out / "job" / "palace_job.py").is_file())
        plan = planning.make_plan(
            out / "campaign.toml",
            "gcp-batch",
            self.config,
            out=self.tmp / "plan",
            offline=True,
            today=testing.TODAY,
        )
        self.assertEqual(plan.meta["image"]["runtime"], palace_plan.RUNTIME)
        self.assertEqual(len(plan.meta["bundles"]), 2)  # job, models
        cls = plan.classes[0]
        placement = plan.placement(cls.name)
        self.assertEqual((placement.shape, placement.tasks_per_vm), ("c4d-highcpu-16", 1))
        self.assertTrue(placement.template)
        job = gcp_batch.render_job(
            plan.meta, self.config, cls, placement, 1, len(cls.lines), testing.DEADLINE
        )
        container = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]
        self.assertNotIn("entrypoint", container)
        self.assertEqual(container["commands"][0], "/opt/palace/venv/bin/python")
        self.assertIn("--shm-size 1g", container["options"])

    def test_placement_follows_memory_to_shapes_with_and_without_templates(self):
        doc = dict(palace_plan.DEFAULTS, ranks=8, memory_gb=40)
        self.assertEqual(palace_plan.vm_shape("c4d", 8, 40, 16, "core"), "c4d-standard-16")
        self.assertNotIn("template", palace_plan.placement(doc))
        doc = dict(palace_plan.DEFAULTS, ranks=4, memory_gb=20)  # c4d-standard-8: no template
        self.assertEqual(palace_plan.placement(doc)["vm_vcpus"], 8)
        self.assertIs(palace_plan.placement(doc)["template"], False)
        # 13 GB: c4d-standard-8 (no template), c4-highcpu-8 (a template in the second region),
        # so the plan keeps templates.
        doc = dict(doc, memory_gb=13)
        self.assertEqual(palace_plan.vm_shape("c4", 4, 13, 8, "core"), "c4-highcpu-8")
        self.assertIs(palace_plan.placement(doc)["template"], False)
        self.assertNotIn("template", palace_plan.placement(dict(doc, families=["c4d", "c4"])))
        doc = dict(palace_plan.DEFAULTS, ranks=16, packing="vcpu", memory_gb=20)
        self.assertEqual(palace_plan.placement(doc)["vm_vcpus"], 16)
        self.assertNotIn("template", palace_plan.placement(doc))

    def test_per_job_resources_place_the_campaign(self):
        # a job with 56 GB needs c4d-highmem-16, which has no template: the campaign runs from
        # instance policies instead of being refused at submit (w3f)
        base = dict(palace_plan.DEFAULTS, ranks=8, memory_gb=48)
        job = {"id": "a", "config": "a.json"}
        self.assertNotIn("template", palace_plan.placement(dict(base, jobs=[job])))
        big = dict(job, id="b", memory_gb=56)
        self.assertEqual(palace_plan.vm_shape("c4d", 8, 56, 16, "core"), "c4d-highmem-16")
        self.assertIs(palace_plan.placement(dict(base, jobs=[job, big]))["template"], False)
        # 16 ranks: 16 cores, a 32-vCPU shape (no template) with the ranks bound to its cores
        wide = dict(job, id="c", ranks=16)
        self.assertEqual(palace_plan.job_cpus(wide, base), 16)
        self.assertEqual(palace_plan.vm_shape("c4d", 16, 48, 16, "core"), "c4d-highcpu-32")
        self.assertIs(palace_plan.placement(dict(base, jobs=[job, wide]))["template"], False)
        line = palace_plan.stage_line(wide, base)
        self.assertEqual(line["resources"]["cpus"], 16)
        self.assertEqual(line["command"][line["command"].index("--ranks") + 1], "16")
        # 4 ranks reserve the campaign's 8 cores: one job per VM, its ranks bound to its cores,
        # rather than two 4-rank jobs bound to the same cores of one VM
        narrow = dict(job, id="d", ranks=4)
        line = palace_plan.stage_line(narrow, base)
        self.assertEqual(line["resources"]["cpus"], 8)
        self.assertEqual(line["command"][line["command"].index("--ranks") + 1], "4")
        self.assertEqual(line["command"][line["command"].index("--bind") + 1], "core")
        self.assertNotIn("template", palace_plan.placement(dict(base, jobs=[job, narrow])))

    def test_binding_follows_the_packing(self):
        doc = dict(palace_plan.DEFAULTS)
        self.assertEqual(palace_plan.bind_of({}, doc), "core")
        self.assertEqual(palace_plan.bind_of({}, dict(doc, packing="vcpu")), "hwthread")
        self.assertEqual(palace_plan.bind_of({}, dict(doc, models_per_vm=2)), "none")
        self.assertEqual(palace_plan.bind_of({"bind": "core"}, dict(doc, models_per_vm=2)), "core")
        line = palace_plan.stage_line(
            {"id": "m", "config": "c.json", "prepare": ["code/mesh.py", "{out}"]},
            dict(doc, models_per_vm=2, prune=["*/postpro/paraview/**"]),
        )
        self.assertEqual(line["command"][6:8], ["--bind", "none"])
        self.assertEqual(json.loads(line["command"][9]), ["code/mesh.py", "{out}"])
        self.assertEqual(line["prune"], ["*/postpro/paraview/**"])

    def test_bad_jobs_are_listed(self):
        errors = palace_plan.check_jobs(
            {
                "name": "X",
                "image": "",
                "ranks": 0,
                "bind": "socket",
                "set": {"bad key": 1},
                "inputs": {"out": "x"},
                "jobs": [{"id": "a", "prepare": "mesh.py"}, {"id": "a", "config": "c"}],
            }
        )
        joined = "; ".join(errors)
        for text in ("name", "image", "ranks", "bind", "set", "'out'", "config", "prepare"):
            self.assertIn(text, joined)
        self.assertIn("repeated", joined)

    # --- the job runner's parts

    def test_relaxed_json_reads_palace_configurations(self):
        config = palace_job.relaxed_json(CONFIG)
        self.assertEqual(config["Boundaries"]["PEC"]["Attributes"], [8, 9, 10, 12])
        self.assertEqual(config["Model"]["L0"], 1.0e-6)
        text = '{"a": "x // not a comment, [1-2]", "b": [-1, 2-4,], /* c */ "c": "/* s */"}'
        self.assertEqual(
            palace_job.relaxed_json(text),
            {"a": "x // not a comment, [1-2]", "b": [-1, 2, 3, 4], "c": "/* s */"},
        )

    def test_effective_config_redirects_output_and_applies_sets(self):
        out = self.tmp / "work" / "out" / "m"
        sets = [("Solver.Order", 3), ("Solver.Driven.Samples.0.MinFreq", 4.0), ("New.Key", "x")]
        config = palace_job.effective_config(self.tmp / "models" / "cpw.json", sets, out)
        self.assertEqual(config["Problem"]["Output"], str(out / "postpro"))
        self.assertEqual(
            config["Model"]["Mesh"], str((self.tmp / "models" / "mesh" / "cpw.msh").resolve())
        )
        self.assertEqual(config["Solver"]["Order"], 3)
        self.assertEqual(config["Solver"]["Driven"]["Samples"][0]["MinFreq"], 4.0)
        self.assertEqual(config["New"], {"Key": "x"})
        with self.assertRaises(ValueError):
            palace_job.set_path(config, "Solver.Driven.Samples.3", 1)

    def test_port_s_comparison(self):
        (self.tmp / "a.csv").write_text(PORT_S)
        result = palace_job.compare_port_s(self.tmp / "a.csv", self.tmp / "a.csv", 1e-6)
        self.assertTrue(result["ok"])
        self.assertEqual((result["values"], result["frequencies"]), (4, 2))
        shifted = PORT_S.replace("-1.119957618551e+02", "-1.129957618551e+02")
        (self.tmp / "b.csv").write_text(shifted)
        result = palace_job.compare_port_s(self.tmp / "b.csv", self.tmp / "a.csv", 1e-3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["at"], {"f_ghz": 2.0, "s": "S[1][1]"})
        # |S11| = -23 dB (0.0705): one degree is |dS| = 0.0705 * 2 sin(0.5 deg) = 0.00123.
        self.assertAlmostEqual(result["max_abs_ds"], 0.00123, places=5)

    def test_log_parsing(self):
        lines = [
            "Assembling system matrices, number of global unknowns:",
            " Level 0 (p = 1): 1000 unknowns",
            " Level 0 (auxiliary) (p = 1): 300 unknowns",
            " Level 1 (p = 2): 9000 unknowns, 50 NNZ",
            "",
            "Adaptive mesh refinement (AMR) iteration 1:",
            " Indicator norm = 1.500e-02, global unknowns = 9000",
            " Max. iterations = 5, tol. = 1.000e-02",
            "Assembling system matrices, number of global unknowns:",
            " Level 0 (p = 2): 12000 unknowns",
            "Completed 1 iteration of adaptive mesh refinement (AMR):",
            " Indicator norm = 4.000e-03, global unknowns = 12000",
        ]
        parsed = palace_job.parse_log(lines)
        self.assertEqual(parsed["amr"], [{"iteration": 1, "indicator": 0.015, "unknowns": 9000}])
        self.assertEqual(
            parsed["amr_completed"], {"iterations": 1, "indicator": 0.004, "unknowns": 12000}
        )
        self.assertEqual(parsed["assembled_unknowns"], [9000, 12000])

    # --- the job runner end to end, with stand-in Palace and mpirun

    def run_job(self, work, extra=(), env_extra=None, config="models/cpw.json"):
        (work / "job").mkdir(parents=True, exist_ok=True)
        (work / "job" / "palace_job.py").write_bytes(Path(palace_job.__file__).read_bytes())
        if not (work / "models").exists():
            (work / "models" / "mesh").mkdir(parents=True)
            (work / "models" / "cpw.json").write_text(CONFIG)
            (work / "models" / "mesh" / "cpw.msh").write_text("$MeshFormat\n")
            (work / "ref").mkdir()
            (work / "ref" / "port-S.csv").write_text(PORT_S)
        palace = executable(work / "palace-x86_64.bin", FAKE_PALACE)
        mpirun = executable(work / "mpirun", FAKE_MPIRUN)
        argv = [sys.executable, "job/palace_job.py", "--id", "m1", "--ranks", "4"]
        argv += ["--palace", str(palace), "--mpirun", str(mpirun)] + list(extra)
        argv += ["--", config]
        env = {
            "PATH": "/usr/bin:/bin",
            "FAKE_PORT_S": PORT_S,
            "FAKE_MPIRUN_LOG": str(work / "mpirun.log"),
        }
        env.update(env_extra or {})
        done = subprocess.run(argv, cwd=work, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads((work / "out" / "m1.job.json").read_text())

    def test_the_job_runner_records_the_solve(self):
        work = self.tmp / "work"
        record = self.run_job(work, ["--set", "Solver.Order=3", "--reference", "ref"])
        log = (work / "out" / "m1.log").read_text()
        self.assertTrue(record["ok"], log)
        self.assertIsNone(record["failed"])
        self.assertEqual(record["sets"], {"Solver.Order": 3})
        self.assertEqual(
            record["palace_version"].splitlines()[0], "Palace version: v0.18.1-160-gb797ea8"
        )
        palace = record["palace"]
        self.assertEqual(
            (palace["dofs"], palace["mpi_size"], palace["adaptation_solves"]), (12000, 4, 2)
        )
        self.assertEqual(
            (palace["peak_memory_mb_sum"], palace["peak_memory_mb_max_rank"]), (800.0, 120.0)
        )
        self.assertEqual((palace["linear_solves"], palace["linear_iterations"]), (7, 140))
        self.assertEqual(record["amr"][0]["unknowns"], 9000)
        self.assertEqual(record["reference"]["max_abs_ds"], 0.0)
        self.assertEqual(set(record["stages"]), {"version", "dry_run", "solve"})
        self.assertIn("ranks 4 omp 1", log)
        self.assertTrue(log.endswith("exit 0\n"))
        self.assertEqual(
            (work / "mpirun.log").read_text().split(),
            [
                "-np",
                "4",
                "--bind-to",
                "core",
                "--map-by",
                "core",
                str(work / "palace-x86_64.bin"),
                str((work / "out" / "m1" / "config.json").resolve()),
            ],
        )
        config = json.loads((work / "out" / "m1" / "config.json").read_text())
        self.assertEqual(config["Solver"]["Order"], 3)
        self.assertEqual(
            config["Problem"]["Output"], str((work / "out" / "m1" / "postpro").resolve())
        )
        self.assertTrue((work / "out" / "m1" / "postpro" / "palace.json").is_file())

    def test_the_job_runner_reports_the_failed_stage(self):
        work = self.tmp / "ref-off"
        record = self.run_job(
            work, ["--reference", "ref"], {"FAKE_PORT_S": PORT_S.replace("-2.30", "-2.50")}
        )
        self.assertEqual((record["ok"], record["failed"], record["exit"]), (False, "reference", 0))
        self.assertGreater(record["reference"]["max_abs_ds"], 0.01)
        work = self.tmp / "solve-fails"
        record = self.run_job(work, env_extra={"FAKE_EXIT": "3"})
        self.assertEqual((record["ok"], record["failed"], record["exit"]), (False, "solve", 3))
        work = self.tmp / "no-mesh"
        (work / "models" / "mesh").mkdir(parents=True)
        (work / "models" / "cpw.json").write_text(CONFIG.replace("cpw.msh", "missing.msh"))
        (work / "ref").mkdir()
        record = self.run_job(work)
        self.assertEqual((record["ok"], record["failed"]), (False, "dry_run"))
        self.assertNotIn("solve", record["stages"])

    def test_the_job_runner_prepares_the_model_first(self):
        work = self.tmp / "prep"
        (work / "code").mkdir(parents=True)
        (work / "code" / "mesh.py").write_text(
            "import os, sys\n"
            "out = sys.argv[1]\n"
            "os.makedirs(out + '/mesh', exist_ok=True)\n"
            "open(out + '/mesh/m.msh', 'w').write('$MeshFormat')\n"
            "open(out + '/model.json', 'w').write("
            '\'{"Problem": {}, "Model": {"Mesh": "mesh/m.msh"}}\')\n'
            "print('meshed', sys.argv[2])\n"
        )
        prepare = json.dumps(["code/mesh.py", "{out}", "{ranks}"])
        record = self.run_job(
            work, ["--prepare", prepare, "--dry-run-only"], config="{out}/model.json"
        )
        self.assertTrue(record["ok"], (work / "out" / "m1.log").read_text())
        self.assertEqual(set(record["stages"]), {"prepare", "version", "dry_run"})
        self.assertIn("meshed 4", (work / "out" / "m1.log").read_text())
        self.assertIsNone(record["palace"])

    def test_the_job_runner_solves_stages_first(self):
        work = self.tmp / "stages"
        (work / "models" / "mesh").mkdir(parents=True)
        (work / "models" / "cpw.json").write_text(CONFIG)
        (work / "models" / "mesh" / "cpw.msh").write_text("$MeshFormat\n")
        amr = palace_job.relaxed_json(CONFIG)
        amr["Model"]["Refinement"] = {"MaxIts": 2, "SaveAdaptMesh": True}
        (work / "models" / "amr.json").write_text(json.dumps(amr))
        (work / "models" / "check.json").write_text(CONFIG)
        stages = ["--stage", "models/amr.json", "--stage", "models/check.json@1"]
        record = self.run_job(work, stages + ["--mesh-from", "models/amr.json"])
        log = (work / "out" / "m1.log").read_text()
        self.assertTrue(record["ok"], log)
        self.assertEqual(
            [(r["config"], r["ranks"], r["exit"]) for r in record["stage_runs"]],
            [("models/amr.json", 4, 0), ("models/check.json", 1, 0)],
        )
        self.assertEqual(record["stage_runs"][0]["palace"]["dofs"], 12000)
        self.assertIn("ranks 1 omp 1", log)
        self.assertEqual([r["name"] for r in record["stage_runs"]], ["1-amr", "2-check"])
        adapted = work / "out" / "m1" / "stage-1-amr" / "cpw.meshgz"
        self.assertTrue(adapted.is_file())
        config = json.loads((work / "out" / "m1" / "config.json").read_text())
        self.assertEqual(config["Model"]["Mesh"], str(adapted.resolve()))
        self.assertTrue((work / "out" / "m1" / "stage-2-check" / "port-S.csv").is_file())
        # a failed stage stops the job before the main solve
        work = self.tmp / "stage-fails"
        record = self.run_job(work, ["--stage", "models/cpw.json"], {"FAKE_EXIT": "5"})
        self.assertEqual(
            (record["ok"], record["failed"], record["exit"]), (False, "stage:1-cpw", 5)
        )
        self.assertNotIn("solve", record["stages"])

    def test_stages_of_the_same_name_keep_their_own_outputs(self):
        # two refinement stages named palace-amr.json in different model directories: each gets
        # its own output directory, and mesh_from takes the mesh of the stage it names
        work = self.tmp / "same-name"
        for model in ("a", "b"):
            (work / "models" / model / "mesh").mkdir(parents=True)
            cfg = palace_job.relaxed_json(CONFIG)
            cfg["Model"]["Mesh"] = "mesh/%s.msh" % model
            cfg["Model"]["Refinement"] = {"MaxIts": 1, "SaveAdaptMesh": True}
            (work / "models" / model / "palace-amr.json").write_text(json.dumps(cfg))
            (work / "models" / model / "mesh" / ("%s.msh" % model)).write_text("$MeshFormat\n")
        (work / "models" / "mesh").mkdir(parents=True)
        (work / "models" / "cpw.json").write_text(CONFIG)
        (work / "models" / "mesh" / "cpw.msh").write_text("$MeshFormat\n")
        stages = ["--stage", "models/a/palace-amr.json", "--stage", "models/b/palace-amr.json"]
        record = self.run_job(work, stages + ["--mesh-from", "models/a/palace-amr.json"])
        self.assertTrue(record["ok"], (work / "out" / "m1.log").read_text())
        out = work / "out" / "m1"
        self.assertTrue((out / "stage-1-palace-amr" / "a.meshgz").is_file())
        self.assertTrue((out / "stage-2-palace-amr" / "b.meshgz").is_file())
        config = json.loads((out / "config.json").read_text())
        self.assertEqual(
            config["Model"]["Mesh"], str((out / "stage-1-palace-amr" / "a.meshgz").resolve())
        )
        # the same stage twice is refused (mesh_from could not tell them apart)
        with self.assertRaises(SystemExit):
            palace_job.main(
                [
                    "--id",
                    "x",
                    "--ranks",
                    "1",
                    "--stage",
                    "a.json",
                    "--stage",
                    "a.json",
                    "--",
                    "c.json",
                ]
            )

    def test_the_record_is_written_while_the_job_runs(self):
        # the fake solve copies the record as it is during the solve: a task killed at its time
        # limit or preempted still leaves the stages it finished
        work = self.tmp / "running"
        snap = self.tmp / "snapshot.json"
        record = self.run_job(
            work,
            ["--stage", "models/cpw.json"],
            {"FAKE_SNAPSHOT": "%s:%s" % (work / "out" / "m1.job.json", snap)},
        )
        self.assertTrue(record["ok"])
        during = json.loads(snap.read_text())
        self.assertEqual((during["ok"], during["failed"]), (False, "running"))
        self.assertEqual([r["name"] for r in during["stage_runs"]], ["1-cpw"])
        self.assertIn("model", during["cpu"])

    def test_plan_passes_stages(self):
        job = {
            "id": "t",
            "config": "s.json",
            "stages": ["a.json", "c.json@1"],
            "mesh_from": "a.json",
        }
        base = dict(palace_plan.DEFAULTS, name="n", image="i", jobs=[job])
        self.assertEqual(palace_plan.check_jobs(base), [])
        command = palace_plan.stage_line(job, base)["command"]
        flags = command[command.index("--stage") : command.index("--")]
        self.assertEqual(
            flags, ["--stage", "a.json", "--stage", "c.json@1", "--mesh-from", "a.json"]
        )
        bad = dict(base, jobs=[dict(job, mesh_from="b.json")])
        self.assertTrue(any("mesh_from" in e for e in palace_plan.check_jobs(bad)))

    # --- collect

    def test_collect_lays_results_out_like_local_runs(self):
        plan_dir = self.tmp / "plans" / "20261004-mceval-abcdef"
        plan_dir.mkdir(parents=True)
        tasks = [
            {"id": "mc/m1", "labels": {"model": "m1"}},
            {"id": "mc/m2", "labels": {"model": "m2"}},
        ]
        (plan_dir / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
        fetched = self.tmp / "fetched" / plan_dir.name
        root = fetched / "tasks" / "mc~m1" / "summary"
        (root / "m1" / "postpro").mkdir(parents=True)
        (root / "m1" / "postpro" / "port-S.csv").write_text(PORT_S)
        (root / "m1.log").write_text("exit 0\n")
        record = {
            "ok": True,
            "ranks": 8,
            "wall_s": 61.0,
            "stages": {"solve": {"exit": 0, "wall_s": 55.0}},
            "palace": {"dofs": 12000, "adaptation_solves": 2, "peak_memory_mb_sum": 800.0},
            "amr_completed": {"iterations": 1, "indicator": 0.004, "unknowns": 12000},
            "reference": {"ok": True, "max_abs_ds": 1e-7},
            "image": {"palace.version": "v0.18.1-160-gb797ea8"},
        }
        (root / "m1.job.json").write_text(json.dumps(record))
        (fetched / "tasks" / "mc~m1" / "_DONE").write_text("{}")
        tree = self.tmp / "tree"
        summary = palace_plan.collect(plan_dir, fetched, tree)
        self.assertEqual(summary["missing"], ["m2"])
        model = summary["models"]["m1"]
        self.assertEqual((model["dofs"], model["solve_s"], model["full"]), (12000, 55.0, False))
        self.assertEqual((model["amr_refinements"], model["adaptation_solves"]), (1, 2))
        self.assertEqual(model["palace"], "v0.18.1-160-gb797ea8")
        self.assertTrue((tree / "runs" / "m1" / "postpro" / "port-S.csv").is_file())
        self.assertTrue((tree / "runs" / "20261004-mceval-abcdef.summary.json").is_file())

    def test_the_image_build_command(self):
        gcp = self.config.require_gcp()
        argv = palace_plan.build_command(gcp, palace_plan.DEFAULT_TAG, None, True)
        self.assertEqual(
            argv[argv.index("--config") + 1], str(palace_plan.DOCKER_DIR / "cloudbuild.yaml")
        )
        subs = dict(kv.split("=", 1) for kv in argv[argv.index("--substitutions") + 1].split(","))
        self.assertEqual(
            subs["_IMAGE"],
            "%s/palace" % gcp.images.format(region=gcp.home_region, project=gcp.project),
        )
        self.assertEqual(subs["_TAG"], "b797ea8-pts")
        self.assertIn("_YAPNR_COMMIT", subs)  # the yapnr commit, recorded as an image label
        self.assertEqual(argv[-1], "--async")
        cloudbuild = palace_plan.DOCKER_DIR / "cloudbuild.yaml"
        if cloudbuild.is_file():  # a checkout (Bazel's runfiles have no docker/)
            self.assertIn("_TAG: %s" % palace_plan.DEFAULT_TAG, cloudbuild.read_text())
            self.assertIn("_VARIANT: %s" % palace_plan.VARIANTS[0], cloudbuild.read_text())


if __name__ == "__main__":
    unittest.main()
