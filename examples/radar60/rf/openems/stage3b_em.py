"""Stage-3b EM matrix: the models and the GCP campaign files, reproducible from the generator.

  # 1. boards: python3 -m rfmacro variants --out GEN; DRC each with --keep-filled (copies)
  # 2. copper of the filled boards (KiCad's Python):
  <kicad python> openems/stage3b_em.py export --fill FILL --em EM
  # 3. models (host Python with shapely and matplotlib):
  python3 openems/stage3b_em.py models --gen GEN --em EM [--only bank,pp,tx,pa,tri]
  # 4. sizes (openEMS image, --setup-only), then the campaign files and their estimates:
  python3 openems/stage3b_em.py sizes --em EM [--docker IMAGE]
  python3 openems/stage3b_em.py jobs --em EM --code RFDIR
  python3 openems/stage3b_em.py summary --gen GEN       # -> results/stage3b/variants.json
  # after E1-C1a: the w35 x t_y points round the best (L scale, inset)
  python3 openems/stage3b_em.py c1b --em EM --best '{"fullwave_l_scale": 1.01, "inset": 0.325}'

FILL holds <name>/<name>.filled.kicad_pcb; EM gets boards/, models/, sizes.json and jobs/*.toml.
The campaign files follow cloud-em's JOBS format (yapnr tools/exp/openems_plan.py): one model x 8
threads per c4d-highcpu-8 (us-west4, the bank) or c4-highcpu-8 (northamerica-northeast1, the
small models; about 1.4x slower), exact end criteria, frequency-domain far fields only, one
bondply plane dump at 62.05 GHz on two drives per pour variant. Every comparison stays on one
machine family and one mesh template.
"""

import argparse
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RF = os.path.dirname(HERE)
sys.path.insert(0, RF)

BANK = {"A": "s3b-A", "B": "s3b-B", "C": "s3b-C", "S1": "s3b-S1"}
BANK_DRIVES = {
    "A": ["TX1", "TX2", "TX3", "RX1", "RX2", "RX3", "RX4"],
    "B": ["TX1", "TX2", "TX3", "RX1", "RX2", "RX3", "RX4"],
    "C": ["TX1", "TX2", "TX3", "RX1", "RX2", "RX3", "RX4"],
    "S1": ["TX1", "TX2", "TX3", "RX3", "RX4"],
}
PP = {"A": "s3b-A", "B": "s3b-B", "C": "s3b-C"}
TXA = {"T0": "s3b-A", "T2": "s3b-T2", "T4": "s3b-T4", "S1": "s3b-S1", "B": "s3b-B", "C": "s3b-C"}
PA = {"pa": "s3b-A", "pa0": "s3b-pa0"}
EXPORT = sorted(set(BANK.values()) | set(PP.values()) | set(TXA.values()) | set(PA.values()))

# measured references on c4d-highcpu-8, T8 (rf-uniform review fixes, 2026-10-04):
# (cells, smallest cell um, minutes)
REF = {
    "bank": (16.03e6, 13.04, 100.0),  # rf-s2, TX 95-104 min (RX drives x 1.45: 138-165 min)
    "cell": (2.66e6, 8.94, 22.6),  # cell-n-r27 (one column with its entry, 27 um)
    "tri": (2.66e6, 8.94, 22.6 * 1.2),  # the same, x 1.2 for the three columns' slower decay
    "txa": (7.64e6, 6.67, 13.9),  # txa-all-r20
    "pa": (7.64e6, 6.67, 25.0),  # like txa, a longer ring-down (the island)
}
C4D_PRICE = 0.0795  # $ per VM-hour, c4d-highcpu-8 Spot us-west4 (cloud-em calib)
C4_SLOW = 1.44  # c4-highcpu-8 Montreal vs c4d (cloud-em c4ne)
C4_PRICE = 0.0795 / 1.015  # about equal cost per run (cloud-em c4ne)


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def cmd_export(a):
    os.makedirs(os.path.join(a.em, "boards"), exist_ok=True)
    for n in EXPORT:
        src = os.path.join(a.fill, n, f"{n}.filled.kicad_pcb")
        run(
            [
                sys.executable,
                os.path.join(HERE, "board_export.py"),
                src,
                os.path.join(a.em, "boards", f"{n}.json"),
                "100",
                "100",
            ]
        )


def tri_specs():
    from rfmacro import variants as V

    out = {}
    for k, ov in enumerate(V.c1a_points()):  # the DOE on the centre column alone (cell3)
        out[f"cell-c1a-{k:02d}"] = ov
    for k, ov in enumerate(V.ser_points()):
        out[f"cell-ser-{k}"] = ov
    out["tri-K0"] = {}
    out["tri-K2"] = {"l23_cavity": "K2"}
    out["tri-K1"] = {"l23_cavity": "K1"}
    return out


def cmd_models(a):
    only = set((a.only or "bank,pp,tx,pa,tri").split(","))
    md = os.path.join(a.em, "models")
    figs = os.path.join(a.em, "figs")
    os.makedirs(md, exist_ok=True)
    os.makedirs(figs, exist_ok=True)

    def board(n):
        return os.path.join(a.em, "boards", f"{n}.json")

    def rec(n):
        return os.path.join(a.gen, n, f"{n}.json")

    py = sys.executable
    if "bank" in only:
        for v, n in BANK.items():
            run(
                [
                    py,
                    os.path.join(HERE, "board_ant.py"),
                    board(n),
                    rec(n),
                    os.path.join(md, f"bank-{v}.json"),
                    "--model",
                    "bank",
                    "--banks",
                    "RX,TX",
                    "--plot",
                    os.path.join(figs, f"bank-{v}.png"),
                ]
            )
    if "pp" in only:
        for v, n in PP.items():
            run(
                [
                    py,
                    os.path.join(HERE, "board_pp.py"),
                    board(n),
                    rec(n),
                    os.path.join(md, f"pp-{v}.json"),
                    "--plot",
                    os.path.join(figs, f"pp-{v}.png"),
                ]
            )
    if "tx" in only:
        for v, n in TXA.items():
            run(
                [
                    py,
                    os.path.join(HERE, "board_feeds.py"),
                    board(n),
                    rec(n),
                    os.path.join(md, f"txa-{v}.json"),
                    "--model",
                    "txa",
                    f"--tag=-{v}",
                    "--plot",
                    os.path.join(figs, f"txa-{v}.png"),
                ]
            )
    if "pa" in only:
        for v, n in PA.items():
            for line in ("TX1", "RX4"):
                tag = f"pa-{v}-{line.lower()}"
                run(
                    [
                        py,
                        os.path.join(HERE, "board_pa.py"),
                        board(n),
                        rec(n),
                        os.path.join(md, f"{tag}.json"),
                        "--line",
                        line,
                        "--plot",
                        os.path.join(figs, f"{tag}.png"),
                    ]
                )
    if "tri" in only:
        for name, ov in tri_specs().items():
            cmd = [py, os.path.join(HERE, "triplet.py"), os.path.join(md, f"{name}.json")]
            for k, v in ov.items():
                cmd += ["--set", f"{k}={json.dumps(v)}"]
            if name == "tri-K1":  # K0's window edges on K1's mesh (one template)
                cmd += ["--mesh-like", os.path.join(md, "tri-K0.json")]
            if name.startswith("cell-"):
                cmd.append("--centre-only")
            if name in ("tri-K0", "tri-K2", "tri-K1", "cell-c1a-00", "cell-ser-0"):
                cmd += ["--plot", os.path.join(figs, f"{name}.png")]
            run(cmd)


def job_list(models_dir):
    """Every E1 run: (campaign, id, script, args, kind, model)."""
    jobs = []
    w = "w/openems"

    def mdl(m):
        return f"models/{m}.json"

    end = ["--max-steps", "400000"]
    for v, drives in BANK_DRIVES.items():
        refs = ",".join(mdl(f"bank-{o}") for o in ("A", "B", "C") if o != v) if v != "S1" else ""
        for dname in drives:
            args = [
                mdl(f"bank-{v}"),
                "--out",
                "{out}",
                "--excite",
                f"{dname}.Pg",
                "--threads",
                "{threads}",
            ] + end
            if refs:
                args += ["--mesh-ref", refs]
            if dname in ("TX1", "RX4") and v != "S1":
                args += ["--dump-bond", "--dump-bond-f", "62.05"]
            jobs.append(
                (
                    f"s3b-e1-bank-{v.lower()}",
                    f"bank-{v}-e{dname}",
                    f"{w}/ant_sim.py",
                    args,
                    "bank",
                    f"bank-{v}",
                )
            )
    for name in tri_specs():
        res = [("r27", 0.016875)]
        if name in ("tri-K0", "tri-K2"):  # 15 um triplets exceed the 4 h task limit (E2: cells)
            res += [("r20", 0.0125)]
        for tag, r in res:
            camp = (
                "s3b-e1-cav"
                if name.startswith("tri-K")
                else ("s3b-e1-c1a" if "-c1a-" in name else "s3b-e1-ser")
            )
            jobs.append(
                (
                    camp,
                    f"{name}-{tag}",
                    f"{w}/ant_sim.py",
                    [
                        mdl(name),
                        "--out",
                        "{out}",
                        "--excite",
                        "T1.Pg",
                        "--threads",
                        "{threads}",
                        "--res",
                        f"{r}",
                    ]
                    + end,
                    "tri" if name.startswith("tri-") else "cell",
                    name,
                )
            )
    for v in PP:
        for tag, r in (("r27", 0.027), ("r20", 0.020), ("r15", 0.015)):
            jobs.append(
                (
                    "s3b-e1-pp",
                    f"pp-{v}-{tag}",
                    f"{w}/pp_sim.py",
                    [
                        mdl(f"pp-{v}"),
                        "--out",
                        "{out}",
                        "--threads",
                        "{threads}",
                        "--res",
                        f"{r}",
                        "--t-ns",
                        "4",
                    ],
                    "pp",
                    f"pp-{v}",
                )
            )
    for v in TXA:
        for ll in ("", "-ll"):
            args = [
                mdl(f"txa-{v}"),
                "--out",
                "{out}",
                "--excite",
                "all",
                "--threads",
                "{threads}",
                "--res",
                "0.0125",
            ]
            if ll:
                args.append("--lossless")
            jobs.append(
                ("s3b-e1-tx", f"txa-{v}{ll}-r20", f"{w}/feed_sim.py", args, "txa", f"txa-{v}")
            )
    for line in ("tx1", "rx4"):  # the PA corner, one model per line: baseline and 3 brackets
        exc = f"{line.upper()}.Pb"
        for v, mode in (("pa0", "short"), ("pa", "short"), ("pa", "open"), ("pa", "port")):
            jobs.append(
                (
                    "s3b-e1-pa",
                    f"pa-{v}-{line}-{mode}",
                    f"{w}/pa_sim.py",
                    [
                        mdl(f"pa-{v}-{line}"),
                        "--out",
                        "{out}",
                        "--excite",
                        exc,
                        "--pa",
                        mode,
                        "--threads",
                        "{threads}",
                        "--res",
                        "0.0125",
                    ],
                    "pa",
                    f"pa-{v}-{line}",
                )
            )
    return jobs


def estimate(kind, size, args):
    """Minutes on c4d-highcpu-8 [D]: the reference scaled by cells and by the smallest cell
    (the time step); the plane-pair runs from their fixed 4 ns."""
    cells, mc_um = size["cells"], min(size["min_cell_um"].values())
    if kind == "pp":
        dt = mc_um * 1e-6 / (299792458.0 * math.sqrt(3)) * 0.9
        steps = 4e-9 / dt
        return cells * steps / 300e6 / 60.0  # 300 MCells/s per c4d-highcpu-8 (calib)
    c0, m0, t0 = REF[kind]
    rx = 1.45 if kind == "bank" and any(str(x).startswith("RX") for x in args) else 1.0
    return t0 * rx * (cells / c0) * (m0 / mc_um)


def _size_key(script, args):
    """The arguments that set a model's mesh: everything but the outputs, the threads, the
    drive, the PA bracket and the dumps."""
    clean, skip = [], 0
    for x in args:
        if skip:
            skip -= 1
            continue
        if x in ("--out", "--threads", "--dump-bond-f", "--excite", "--pa"):
            skip = 1
            continue
        if x in ("--dump-bond", "--lossless"):
            continue
        clean.append(x)
    return os.path.basename(script), clean


def cmd_sizes(a):
    """Cells of every model and resolution (openEMS image, --setup-only), once per mesh."""
    path = os.path.join(a.em, "sizes.json")
    sizes = json.load(open(path)) if os.path.exists(path) else {}
    done = {}
    for camp, jid, script, args, kind, model in job_list(None):
        if jid in sizes:
            continue
        name, clean = _size_key(script, args)
        key = json.dumps([name] + clean)
        if key not in done:
            tag = f"{name[:-3]}-{len(done)}"
            cmd = [
                "docker",
                "run",
                "--rm",
                "--cpus",
                "2",
                "-v",
                f"{RF}:/w/w",
                "-v",
                f"{os.path.abspath(a.em)}:/w/em",
                "-w",
                "/w/em",
                "-e",
                "RFMACRO_ROOT=/w/w",
                a.docker,
                "python3",
                f"/w/w/openems/{name}",
            ] + clean
            if name == "ant_sim.py":
                cmd += ["--excite", "x"]
            elif name in ("feed_sim.py", "pa_sim.py"):
                cmd += ["--excite", "all"]
            cmd += ["--out", f"setup/{tag}", "--setup-only"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            meta = os.path.join(a.em, "setup", tag, "meta.json")
            if r.returncode or not os.path.exists(meta):
                print(jid, "FAILED", r.stderr[-800:], flush=True)
                done[key] = None
                continue
            m = json.load(open(meta))
            mc = m.get("min_cell_um") or {"x": m.get("res_um", 15.0)}
            done[key] = dict(cells=m["cells"], min_cell_um=mc, mesh=m.get("mesh"))
            print(jid, m["cells"], flush=True)
        if done[key]:
            sizes[jid] = done[key]
        json.dump(sizes, open(path, "w"), indent=1)


def cmd_jobs(a):
    sizes = json.load(open(os.path.join(a.em, "sizes.json")))
    jd = os.path.join(a.em, "jobs")
    os.makedirs(jd, exist_ok=True)
    camps = {}
    for camp, jid, script, args, kind, model in job_list(None):
        camps.setdefault(camp, []).append((jid, script, args, kind, model))
    summary = {}
    for camp, jobs in camps.items():
        c4d = camp.startswith("s3b-e1-bank")
        fam = "c4d" if c4d else "c4"
        est = []
        for jid, script, args, kind, model in jobs:
            sz = sizes.get(jid)
            m = estimate(kind, sz, args) if sz else None
            if m is not None and not c4d:
                m *= C4_SLOW
            est.append(m)
        mins = [m for m in est if m]
        vmh = sum(mins) / 60.0
        price = C4D_PRICE if c4d else C4_PRICE
        wall = max(mins) if mins else None
        lines = [
            f"# stage 3b E1 ({camp}): written by openems/stage3b_em.py jobs; estimates [D] from the",
            f"# rf-uniform references scaled by cells and smallest cell: {vmh:.2f} VM-h, about"
            f" ${vmh * price:.2f} expected (boot and preemption extra)",
            f'name = "{camp}"',
            'image = "openems:0.37.0-rc3-x86-64-v4"',
            "threads = 8",
            "models_per_vm = 1",
            'packing = "vcpu"',
            f'families = ["{fam}"]',
            'openems_options = ["exact-endcriteria"]',
            "memory_gb = 8",
            "disk_gb = 16",
            f"max_wall_s = {14400}",
            "",
            "[inputs]",
            f'w = "{os.path.abspath(a.code)}"',
            f'models = "{os.path.abspath(os.path.join(a.em, "models"))}"',
            "",
            "[env]",
            'RFMACRO_ROOT = "w"',
        ]
        for (jid, script, args, kind, model), m in zip(jobs, est):
            lines += [
                "",
                "[[jobs]]",
                f'id = "{jid}"',
                f'script = "{script}"',
                "args = [" + ", ".join(json.dumps(x) for x in args) + "]",
            ]
            if m:
                lines.append(f"# estimate {m:.0f} min on {fam}")
        with open(os.path.join(jd, f"{camp}.toml"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        summary[camp] = dict(
            family=fam,
            runs=len(jobs),
            vm_hours=round(vmh, 2),
            expected_usd=round(vmh * price, 2),
            longest_min=round(wall or 0, 1),
            unsized=[j for (j, *_), m in zip(jobs, est) if not m],
        )
    json.dump(summary, open(os.path.join(jd, "summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


def cmd_summary(a):
    """The stage-3b boards in one table (results/stage3b/variants.json): options, checks, vias,
    the D15 metrics, the PA feed's numbers, the cavity, the TX lengths and the DRC."""
    from rfmacro import variants as V

    out = {}
    for n in V.BOARDS:
        d = os.path.join(a.gen, n)
        rec = json.load(open(os.path.join(d, f"{n}.json")))
        drc_p = os.path.join(d, f"{n}.drc-summary.json")
        drc = json.load(open(drc_p)) if os.path.exists(drc_p) else None
        ck = {c["check"]: c for c in rec["checks"]}

        def get(prefix):
            return next((c for k, c in ck.items() if k.startswith(prefix)), {})

        sm, g8, g9 = get("stitch metrics"), get("G8"), get("G9")
        eq = get("equal length P0->P1 TX")
        out[n] = dict(
            overrides=V.BOARDS[n],
            checks_failed=[k for k, c in ck.items() if c.get("ok") is False],
            vias=rec["vias"],
            vias_0p15_drill=sm.get("vias_0p15_drill"),
            l1_dmax_mm=[sm.get("l1_dmax_edge_mm"), sm.get("l1_dmax_interior_mm")],
            l1_area_beyond_mm2=sm.get("l1_area_beyond_mm2"),
            l1_gnd_mm2=sm.get("l1_gnd_mm2"),
            l23_largest_gap_radius_mm=sm.get("l23_largest_gap_radius_mm"),
            cutoff_ghz=sm.get("cutoff_ghz"),
            pa=(
                None
                if not g8
                else dict(
                    vias=g8["geometry"]["vias"],
                    ir_drop_mv=g8["electrical"]["ir_drop_mv"],
                    i_peak_per_via_a=g8["electrical"]["i_peak_per_via_a"],
                    neck_mm=g8["electrical"]["neck_width_mm"],
                    antipad_to_feed_mm=g8["l2_antipad_to_feed_centreline_mm"],
                )
            ),
            cavity={
                b: dict(
                    largest_via_free_radius_mm=v["largest_via_free_radius_mm"],
                    tm_modes_57_70=v["tm_modes_57_70ghz"]["count"],
                )
                for b, v in g9.get("banks", {}).items()
            },
            posts=g9.get("posts"),
            tx_p0_p1_mm=eq.get("lengths_mm"),
            tx_spread_mm=eq.get("spread_mm"),
            tx_entry_y=rec["fit"]["tx"]["E"],
            tx_dx_bank=rec["fit"]["tx"]["dx_bank"],
            pocket_board=rec["board_frame"]["vout_pa_pocket"],
            drc=(
                None
                if drc is None
                else dict(violations=drc["violations"], unconnected=drc["unconnected"])
            ),
        )
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
        fh.write("\n")
    for n, r in out.items():
        print(
            n,
            r["checks_failed"],
            r["vias"]["total"],
            r["vias_0p15_drill"],
            r["l1_dmax_mm"],
            r["drc"],
        )


def cmd_c1b(a):
    from rfmacro import variants as V

    best = json.loads(a.best)
    md = os.path.join(a.em, "models")
    for k, ov in enumerate(V.c1b_points(best)):
        cmd = [
            sys.executable,
            os.path.join(HERE, "triplet.py"),
            os.path.join(md, f"cell-c1b-{k}.json"),
            "--centre-only",
        ]
        for kk, v in ov.items():
            cmd += ["--set", f"{kk}={json.dumps(v)}"]
        run(cmd)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--fill", required=True)
    e.add_argument("--em", required=True)
    m = sub.add_parser("models")
    m.add_argument("--gen", required=True)
    m.add_argument("--em", required=True)
    m.add_argument("--only")
    s = sub.add_parser("sizes")
    s.add_argument("--em", required=True)
    s.add_argument("--docker", default="radar60-openems:0.37.0-rc3")
    j = sub.add_parser("jobs")
    j.add_argument("--em", required=True)
    j.add_argument("--code", default=RF)
    u = sub.add_parser("summary")
    u.add_argument("--gen", required=True)
    u.add_argument("--out", default=os.path.join(RF, "results", "stage3b", "variants.json"))
    c = sub.add_parser("c1b")
    c.add_argument("--em", required=True)
    c.add_argument("--best", required=True)
    a = ap.parse_args()
    dict(
        export=cmd_export,
        models=cmd_models,
        sizes=cmd_sizes,
        jobs=cmd_jobs,
        c1b=cmd_c1b,
        summary=cmd_summary,
    )[a.cmd](a)


if __name__ == "__main__":
    main()
