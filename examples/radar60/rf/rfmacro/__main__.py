"""radar60 RF macro generator.

  python -m rfmacro xsec                         # 2D line table -> results/xsec.json (FEA env)
  python -m rfmacro build [--variant -1|0|1] [--out DIR] [--set key=value ...]
  python -m rfmacro drc DIR/NAME.kicad_pcb       # headless KiCad DRC (YAPNR_KICAD_CLI)
  python -m rfmacro coupons [--out DIR]

Run from examples/radar60/rf (the yapnr checkout on PYTHONPATH only for `xsec`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import SCHEMA

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT_S = 900


def _kicad_cli() -> str:
    cli = os.environ.get("YAPNR_KICAD_CLI")
    if not cli or not os.path.exists(cli):
        raise SystemExit("set YAPNR_KICAD_CLI to a headless kicad-cli (see DEVELOPERS.md#kicad)")
    return cli


def drc(pcb: str, keep_filled: bool = False) -> dict:
    """KiCad DRC with the zones refilled; the filled board is saved next to the input."""
    cli = _kicad_cli()
    d = os.path.dirname(os.path.abspath(pcb))
    name = os.path.splitext(os.path.basename(pcb))[0]
    with tempfile.TemporaryDirectory() as tmp:
        for ext in ("kicad_pcb", "kicad_pro", "kicad_dru"):
            src = os.path.join(d, f"{name}.{ext}")
            if os.path.exists(src):
                shutil.copy(src, tmp)
        rep = os.path.join(tmp, "drc.json")
        r = subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--refill-zones",
                "--save-board",
                "--format",
                "json",
                "--severity-all",
                "-o",
                rep,
                f"{name}.kicad_pcb",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        if not os.path.exists(rep):
            raise RuntimeError(f"kicad-cli drc failed: {r.stderr[-2000:]}")
        with open(rep, encoding="utf-8") as f:
            report = json.load(f)
        if keep_filled:
            shutil.copy(
                os.path.join(tmp, f"{name}.kicad_pcb"), os.path.join(d, f"{name}.filled.kicad_pcb")
            )
    counts: dict = {}
    examples: dict = {}
    for v in report.get("violations", []):
        key = f"{v.get('severity')}:{v.get('type')}"
        counts[key] = counts.get(key, 0) + 1
        examples.setdefault(key, v.get("description"))
    summary = dict(
        board=f"{name}.kicad_pcb",
        kicad_version=report.get("kicad_version"),
        command="kicad-cli pcb drc --refill-zones --severity-all",
        violations=counts,
        examples=examples,
        unconnected=len(report.get("unconnected_items", [])),
    )
    with open(os.path.join(d, f"{name}.drc-summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
        fh.write("\n")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rfmacro", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("xsec")
    b = sub.add_parser("build")
    b.add_argument("--variant", type=int, default=0, choices=(-1, 0, 1))
    b.add_argument("--out", default=os.path.join(ROOT, "build"))
    b.add_argument("--set", action="append", default=[], help="parameter override key=json")
    d = sub.add_parser("drc")
    d.add_argument("pcb")
    d.add_argument("--keep-filled", action="store_true", help="also save the zone-filled board")
    c = sub.add_parser("coupons")
    c.add_argument("--out", default=os.path.join(ROOT, "build"))
    a = ap.parse_args(argv)

    if a.cmd == "xsec":
        from . import xsec2d

        rows = xsec2d.table()
        os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
        with open(os.path.join(ROOT, "results", "xsec.json"), "w") as fh:
            json.dump(rows, fh, indent=1)
            fh.write("\n")
        for r in rows:
            print(
                f"{r['what']:44s} w {r['w']:.4f}  Z0 {r['z0_static']:6.2f}  eeff(62 GHz) "
                f"{r['eeff_62g']:.3f}  {r['alpha_db_mm']:.3f} dB/mm"
            )
        return 0
    if a.cmd == "build":
        from . import kicad, macro, report

        ov = {"variant": a.variant}
        for kv in a.set:
            k, v = kv.split("=", 1)
            ov[k] = json.loads(v)
        mc = macro.build(ov)
        tag = {-1: "m", 0: "n", 1: "p"}[a.variant]
        name = f"rfm1-{tag}"
        out = os.path.join(a.out, name)
        paths = kicad.write(
            mc,
            out,
            name,
            f"radar60 RFM1 conventional macro, variant {a.variant:+d} (PLACEHOLDER until C1/C2)",
        )
        rec = report.record(mc)
        rec["schema"] = SCHEMA
        rec["files"] = {k: os.path.basename(v) for k, v in paths.items()}
        with open(os.path.join(out, f"{name}.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
            fh.write("\n")
        bad = [c for c in mc.checks if c.get("ok") is False]
        print(json.dumps(dict(out=out, checks_failed=[c["check"] for c in bad]), indent=1))
        return 1 if bad else 0
    if a.cmd == "drc":
        res = drc(a.pcb, a.keep_filled)
        print(json.dumps(res, indent=1))
        return (
            0
            if not [k for k in res["violations"] if k.startswith("error")]
            and res["unconnected"] == 0
            else 1
        )
    if a.cmd == "coupons":
        from . import coupons

        paths = coupons.build_and_write(os.path.join(a.out, "coupons"))
        print(json.dumps(paths, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
