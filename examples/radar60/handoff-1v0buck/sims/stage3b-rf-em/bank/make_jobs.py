"""Stage-3b EM bank and ground campaigns (D15 A/B/C, S1 vs S2, L2-L3 cavity K0/K2/K1, plane-pair
ring-downs): JOBS.toml files for tools/exp/openems_plan.py. Code: the rf tree at the commit in
code/COMMIT (git archive), models copied from ../models (stage3b_em.py models)."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.join(HERE, "code", "examples", "radar60", "rf")
MODELS = os.path.join(HERE, "models")
HEAD = """# stage 3b EM bank and ground ({name}); written by em/bank/make_jobs.py
name = "{name}"
image = "openems:0.37.0-rc3-x86-64-v4"
threads = 8
models_per_vm = 1
packing = "vcpu"
families = ["{fam}"]
openems_options = ["exact-endcriteria"]
memory_gb = 8
disk_gb = 16
max_wall_s = {wall}

[inputs]
w = "{w}"
models = "{models}"

[env]
RFMACRO_ROOT = "w"
"""


def job(jid, script, args):
    a = ", ".join(f'"{x}"' for x in args)
    return f'\n[[jobs]]\nid = "{jid}"\nscript = "w/openems/{script}"\nargs = [{a}]\n'


def write(name, fam, wall, jobs):
    with open(os.path.join(HERE, "jobs", f"{name}.toml"), "w") as fh:
        fh.write(HEAD.format(name=name, fam=fam, wall=wall, w=W, models="models_in"))
        for j in jobs:
            fh.write(job(*j))
    # inputs: models by absolute path
    txt = open(os.path.join(HERE, "jobs", f"{name}.toml")).read().replace("models_in", MODELS)
    open(os.path.join(HERE, "jobs", f"{name}.toml"), "w").write(txt)


DR = ["TX1", "TX2", "TX3", "RX1", "RX2", "RX3", "RX4"]
for v in ("A", "B", "C", "S1"):
    drives = DR if v != "S1" else ["TX1", "TX2", "TX3", "RX3", "RX4"]
    jobs = []
    for d in drives:
        args = [
            f"models/bank-{v}.json",
            "--out",
            "{out}",
            "--excite",
            f"{d}.Pg",
            "--threads",
            "{threads}",
            "--max-steps",
            "400000",
            "--probe-bond",
        ]
        if v != "S1":
            args += ["--mesh-ref", ",".join(f"models/bank-{o}.json" for o in "ABC" if o != v)]
            if d in ("TX1", "RX4"):
                args += ["--dump-bond", "--dump-bond-f", "62.05"]
        jobs.append((f"bank-{v}-e{d}", "ant_sim.py", args))
    write(f"s3b-bank-{v.lower()}", "c4d", 11700, jobs)

jobs = []
K = ("K0", "K2", "K1")
for k in K:
    for tag, r in (("r27", "0.016875"), ("r20", "0.0125")):
        if k == "K1" and tag == "r20":
            continue
        refs = ",".join(f"models/tri-{o}.json" for o in K if o != k)
        jobs.append(
            (
                f"tri-{k}-{tag}",
                "ant_sim.py",
                [
                    f"models/tri-{k}.json",
                    "--out",
                    "{out}",
                    "--excite",
                    "T1.Pg",
                    "--threads",
                    "{threads}",
                    "--res",
                    r,
                    "--max-steps",
                    "400000",
                    "--mesh-ref",
                    refs,
                    "--probe-bond",
                ],
            )
        )
write("s3b-cav", "c4d", 10800, jobs)

jobs = []
for v in "ABC":
    for tag, r in (("r27", "0.027"), ("r20", "0.02"), ("r15", "0.015")):
        jobs.append(
            (
                f"pp-{v}-{tag}",
                "pp_sim.py",
                [
                    f"models/pp-{v}.json",
                    "--out",
                    "{out}",
                    "--threads",
                    "{threads}",
                    "--res",
                    r,
                    "--t-ns",
                    "4",
                ],
            )
        )
write("s3b-pp", "c4", 5400, jobs)

# cavity ring-downs (code at de19c97: --ringdown-ns): no port driven, bondply sources
W_RD = os.path.join(HERE, "code-rd", "examples", "radar60", "rf")
jobs = []
for k, tag, r_, ns in (
    ("K0", "r27", "0.016875", "2.5"),
    ("K2", "r27", "0.016875", "2.5"),
    ("K0", "r20", "0.0125", "2.0"),
):
    refs = ",".join(f"models/tri-{o}.json" for o in K if o != k)
    jobs.append(
        (
            f"tri-{k}-rd-{tag}",
            "ant_sim.py",
            [
                f"models/tri-{k}.json",
                "--out",
                "{out}",
                "--excite",
                "none",
                "--threads",
                "{threads}",
                "--res",
                r_,
                "--mesh-ref",
                refs,
                "--ringdown-ns",
                ns,
            ],
        )
    )
jobs.append(
    (
        "bank-A-rd",
        "ant_sim.py",
        [
            "models/bank-A.json",
            "--out",
            "{out}",
            "--excite",
            "none",
            "--threads",
            "{threads}",
            "--mesh-ref",
            "models/bank-B.json,models/bank-C.json",
            "--ringdown-ns",
            "2.5",
        ],
    )
)
write("s3b-cavrd", "c4d", 10800, jobs)
txt = open(os.path.join(HERE, "jobs", "s3b-cavrd.toml")).read().replace(W, W_RD)
open(os.path.join(HERE, "jobs", "s3b-cavrd.toml"), "w").write(txt)
