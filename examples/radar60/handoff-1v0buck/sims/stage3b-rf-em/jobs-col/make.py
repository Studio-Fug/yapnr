"""Stage-3b EM column/feeds campaigns (C1 retune, D5 series points, TX feed options, D14 PA corner).

One model x 8 threads per c4-highcpu-8 (northamerica-northeast1), exact end criteria; every C1
point at one resolution shares one mesh template (--mesh-ref: the union of the group's edges).
Resolutions: r27/r20/r15 = ant_sim --res 0.016875/0.0125/0.009375 (fill 27/20/15 um).
"""

import json
import os

EM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RF = "${REPO}/examples/radar60/rf"
RES = {"r27": 0.016875, "r20": 0.0125, "r15": 0.009375}
C1 = [
    "cell-c1a-07",
    "cell-c1a-10",
    "cell-c1a-13",
    "cell-c1a-09",
    "cell-c1a-11",
    "cell-c1w-030",
    "cell-c1w-042",
]
C1L = C1[:3]
SER = ["cell-ser-1", "cell-ser-2", "cell-ser-7", "cell-ser-8"]


def ant(m, tag, group, extra=()):
    ref = ",".join(f"models/{o}.json" for o in group if o != m)
    return dict(
        id=f"{m}-{tag}" + ("".join(extra[1::2]) if False else ""),
        script="w/openems/ant_sim.py",
        args=[
            f"models/{m}.json",
            "--out",
            "{out}",
            "--excite",
            "T1.Pg",
            "--threads",
            "{threads}",
            "--res",
            str(RES[tag[:3]]),
            "--max-steps",
            "400000",
            "--mesh-ref",
            ref,
            "--fspan",
            "54",
            "70",
            *extra,
        ],
    )


def write(name, jobs, note):
    lines = [
        f"# {note}",
        f'name = "{name}"',
        'image = "openems:0.37.0-rc3-x86-64-v4"',
        "threads = 8",
        "models_per_vm = 1",
        'packing = "vcpu"',
        'families = ["c4"]',
        'openems_options = ["exact-endcriteria"]',
        "memory_gb = 8",
        "disk_gb = 16",
        "max_wall_s = 14400",
        "",
        "[inputs]",
        f'w = "{RF}"',
        f'models = "{EM}/models"',
        "",
        "[env]",
        'RFMACRO_ROOT = "w"',
    ]
    for j in jobs:
        lines += [
            "",
            "[[jobs]]",
            f'id = "{j["id"]}"',
            f'script = "{j["script"]}"',
            "args = [" + ", ".join(json.dumps(x) for x in j["args"]) + "]",
        ]
    open(os.path.join(EM, "jobs-col", f"{name}.toml"), "w").write("\n".join(lines) + "\n")
    print(name, len(jobs))


# C1 anchors at 15 um: the L sweep at inset 0.325 / w35 0.353 / t_y 0.37
write("s3b-c1-r15", [ant(m, "r15", C1) for m in C1L], "C1 L sweep at 15 um (critical path)")
# C1 at 20 um: L sweep + inset and w35 round L 1.025, plus L 1.025 with 8+8 z-cells
r20 = [ant(m, "r20", C1) for m in C1]
j = ant("cell-c1a-10", "r20", C1, ("--nz-core", "8", "--nz-bond", "8"))
j["id"] = "cell-c1a-10-r20-nz8"
r20.append(j)
write("s3b-c1-r20", r20, "C1 at 20 um: L x 3, inset x 2, w35 x 2, and one 8+8 z-cell run")
# C1 L sweep at 27 um (the trend's third mesh) + D5 series points at 27 um
r27 = [ant(m, "r27", C1) for m in C1L] + [ant(m, "r27", SER) for m in SER]
write("s3b-c1-r27", r27, "C1 L sweep and D5 series-fed points at 27 um")
# TX feeds at 20 um with 8 z-cells (palace READY: sign-off needs 8 across the core)
tx = []
for v in ["T0", "T2", "T4", "S1", "B", "C"]:
    for ll in ("", "-ll"):
        a = [
            f"models/txa-{v}.json",
            "--out",
            "{out}",
            "--excite",
            "all",
            "--threads",
            "{threads}",
            "--res",
            "0.0125",
            "--nz-core",
            "8",
        ]
        if ll:
            a.append("--lossless")
        tx.append(dict(id=f"txa-{v}{ll}-r20nz8", script="w/openems/feed_sim.py", args=a))
write("s3b-tx-r20", tx, "TX1-3 feeds P0->P1, options x lossy/lossless, 20 um, 8 z-cells")
# D14 PA corner (as built in E1)
pa = []
for line in ("tx1", "rx4"):
    for v, mode in (("pa0", "short"), ("pa", "short"), ("pa", "open"), ("pa", "port")):
        pa.append(
            dict(
                id=f"pa-{v}-{line}-{mode}",
                script="w/openems/pa_sim.py",
                args=[
                    f"models/pa-{v}-{line}.json",
                    "--out",
                    "{out}",
                    "--excite",
                    f"{line.upper()}.Pb",
                    "--pa",
                    mode,
                    "--threads",
                    "{threads}",
                    "--res",
                    "0.0125",
                ],
            )
        )
write("s3b-pa-r20", pa, "D14 PA corner: TX1/RX4 x (no feed; feed with vias shorted/open/ported)")


def e2_pa():
    """E2-PA: TX1 corner with and without the feed on one mesh template (each model meshes the
    other's copper edges too), 20 and 15 um."""
    jobs = []
    for tag, res in (("r20", "0.0125"), ("r15", "0.009375")):
        for v, mode, other in (
            ("pa0", "short", "pa"),
            ("pa", "short", "pa0"),
            ("pa", "port", "pa0"),
            ("pa", "open", "pa0"),
        ):
            if tag == "r15" and mode == "open":
                continue
            jobs.append(
                dict(
                    id=f"pa-{v}-tx1-{mode}-u{tag[1:]}",
                    script="w/openems/pa_sim.py",
                    args=[
                        f"models/pa-{v}-tx1.json",
                        "--out",
                        "{out}",
                        "--excite",
                        "TX1.Pb",
                        "--pa",
                        mode,
                        "--threads",
                        "{threads}",
                        "--res",
                        res,
                        "--mesh-ref",
                        f"models/pa-{other}-tx1.json",
                    ],
                )
            )
    write("s3b-pa2", jobs, "E2-PA: TX1 corner, feed vs no feed on one mesh template, 20/15 um")


if __name__ == "__main__" and os.environ.get("E2") == "pa":
    e2_pa()
