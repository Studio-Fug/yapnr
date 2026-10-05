"""Command line of the fab-model coupons: `python -m yapnr.rf.coupons <command>` (or
`bazel run //yapnr/rf/coupons:cli -- <command>`).

- `generate`: the KiCad project of a coupon board (DRC and the fab package when a headless
  kicad-cli is configured in YAPNR_KICAD_CLI);
- `expected`: the predicted Touchstone file of every modelled stick;
- `synthetic`: a synthetic measurement session (the input format of `extract`);
- `check-session`: file names, missing sticks and grids of a session;
- `extract`: calibration, joint fit, held-out checks, the fit record, the report and the
  stackup overlay;
- `study`: the synthetic-recovery study (design §10).

Every command writes files and prints where; none needs more than numpy (and PyYAML for
`session.yaml`). The 2D tables are rebuilt with `python -m yapnr.rf.coupons.families build` in
the FEA environment.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import List, Optional

import numpy as np

from yapnr.rf.coupons import stackups


def _stackup(a) -> str:
    stackups.get(a.stackup)  # validates
    return a.stackup


def cmd_generate(a) -> int:
    from yapnr.rf.coupons import fab

    out = fab.generate(
        _stackup(a), a.out, revision=a.revision, fab=not a.no_fab, upload=a.upload, git=a.git
    )
    w, h = out["panel"]
    print(f"board: {out['board']} (panel {w:.1f} x {h:.1f} mm, {w * h / 645.16:.2f} sq in)")
    if out.get("drc") is not None:
        drc = out["drc"]
        n = sum(drc["violations"].values())
        print(f"DRC: {n} violations, {drc['unconnected']} unconnected ({drc['kicad_version']})")
        for v in drc["details"][:10]:
            print(f"  {v['severity']} {v['type']}: {v['description']}")
        if out.get("fab_zip"):
            print(f"fab package: {out['fab_zip']}")
        return 0 if n == 0 and drc["unconnected"] == 0 else 1
    if not a.no_fab:
        print("no headless kicad-cli in YAPNR_KICAD_CLI: DRC and the fab package skipped")
    return 0


def cmd_expected(a) -> int:
    from yapnr.rf.coupons import expected

    st = stackups.get(_stackup(a))
    if st.board == "O":
        paths = expected.write_o(a.upload or "M", a.out)
    else:
        paths = expected.write(_stackup(a), a.out)
    print(f"{len(paths)} Touchstone files in {a.out}")
    return 0


def cmd_synthetic(a) -> int:
    from yapnr.rf.coupons import synthetic

    st = stackups.get(_stackup(a))
    rng = np.random.default_rng(a.seed)
    truth = synthetic.draw_truth(st, rng) if not a.nominal else {}
    imp = synthetic.Imperfections(**(json.loads(a.imperfections) if a.imperfections else {}))
    f = synthetic.grid(a.fmax * 1e9, a.step * 1e6)
    rec = synthetic.simulate(st.id, truth, a.out, f, rng, serial=a.serial, imp=imp)
    print(f"{rec['files']} files and truth.json in {a.out}")
    return 0


def cmd_check(a) -> int:
    from yapnr.rf.coupons import catalog, session

    ses = session.load(a.session, a.serial)
    problems = session.check(ses, catalog.board(_stackup(a)))
    for p in problems:
        print(p)
    print(f"{sum(len(v) for v in ses.meas.values())} files, {len(problems)} problems")
    return 1 if problems else 0


def _prior(path: Optional[str]):
    """Priors from an earlier fit (a lot fit with the board fit as prior, design §5.4)."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    return {n: (p["value"], p["sigma"]) for n, p in rec["parameters"].items()}


def cmd_extract(a) -> int:
    from yapnr.rf.coupons import export, fit

    t0 = time.time()
    res, d, p = fit.extract(
        _stackup(a),
        a.measured,
        serial=a.serial,
        prior=_prior(a.prior),
        f_max=a.fmax * 1e9 if a.fmax else None,
        with_systematics=not a.no_systematics,
        boot=a.bootstrap,
    )
    paths = export.write_all(a.out, res, d, p, a.measured, a.serial)
    print(f"fit in {time.time() - t0:.1f} s ({res.iterations} iterations)")
    for n in res.names:
        q = stackups.param(n, p.st)
        print(f"  {n:12s} {res.value[n]:.5g} ± {res.sigma_total[n]:.2g} {q.unit}")
    for w in res.warnings:
        print(f"  warning: {w}")
    for pr in d.problems:
        print(f"  problem: {pr}")
    for name, path in paths.items():
        print(f"{name}: {path}")
    failed = [k for k, h in res.held_out.items() if not h["passed"]]
    if failed:
        print(f"held-out checks failed: {', '.join(failed)}")
    return 0


def cmd_study(a) -> int:
    from yapnr.rf.coupons import synthetic

    out = synthetic.study(
        _stackup(a),
        a.out,
        draws=a.draws,
        seed=a.seed,
        f_max=a.fmax * 1e9,
        truth=a.truth,
        clamp_on=a.clamp_on,
        boot=a.bootstrap,
        log=lambda m: print(m, flush=True),
    )
    print(json.dumps(out["summary"], indent=2))
    return 0 if out["summary"]["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m yapnr.rf.coupons", description=__doc__.split("\n")[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    known = ", ".join(sorted(stackups.STACKUPS))

    def stackup_arg(p):
        p.add_argument("--stackup", required=True, help=f"stackup id ({known})")

    g = sub.add_parser("generate", help="KiCad project, DRC and fab package of a coupon board")
    stackup_arg(g)
    g.add_argument("--out", required=True)
    g.add_argument("--revision", default="1")
    g.add_argument("--no-fab", action="store_true", help="skip DRC, Gerbers and the zip")
    g.add_argument("--upload", default="", help="board O: the OSH Park upload, M, W or D")
    g.add_argument("--git", default=None, help="board O: the generator commit on the tag")
    g.set_defaults(fn=cmd_generate)

    e = sub.add_parser("expected", help="predicted Touchstone files at nominal parameters")
    stackup_arg(e)
    e.add_argument("--out", required=True)
    e.add_argument(
        "--upload", default="", help="board O: the upload (M, W, D); FR408HR and EM528 both"
    )
    e.set_defaults(fn=cmd_expected)

    s = sub.add_parser("synthetic", help="a synthetic measurement session")
    stackup_arg(s)
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--serial", default="01")
    s.add_argument("--fmax", type=float, default=6.0, help="GHz (default 6)")
    s.add_argument("--step", type=float, default=10.0, help="MHz (default 10)")
    s.add_argument("--nominal", action="store_true", help="truth = nominal parameters")
    s.add_argument("--imperfections", default="", help="JSON overrides of the error model")
    s.set_defaults(fn=cmd_synthetic)

    c = sub.add_parser("check-session", help="check file names and completeness of a session")
    stackup_arg(c)
    c.add_argument("session")
    c.add_argument("--serial", default=None)
    c.set_defaults(fn=cmd_check)

    x = sub.add_parser("extract", help="fit the fab parameters to a measured session")
    stackup_arg(x)
    x.add_argument("--measured", required=True, help="session directory (design §7.6)")
    x.add_argument("--out", required=True)
    x.add_argument("--serial", default=None, help="one board of the session")
    x.add_argument("--prior", default=None, help="fit.json of an earlier fit as the prior")
    x.add_argument("--fmax", type=float, default=None, help="highest frequency fitted (GHz)")
    x.add_argument("--no-systematics", action="store_true", help="skip the systematic refits")
    x.add_argument(
        "--bootstrap",
        type=int,
        default=12,
        help="parametric-bootstrap sessions for the uncertainty (default 12; 0 = fit σ only)",
    )
    x.set_defaults(fn=cmd_extract)

    y = sub.add_parser("study", help="synthetic-recovery study (design §10)")
    stackup_arg(y)
    y.add_argument("--out", required=True)
    y.add_argument("--draws", type=int, default=50)
    y.add_argument("--seed", type=int, default=1000)
    y.add_argument("--fmax", type=float, default=12.0, help="GHz (default 12)")
    y.add_argument(
        "--truth",
        choices=("tables", "direct"),
        default="tables",
        help="truth from the shipped tables, or from direct 2D solves (FEA environment)",
    )
    y.add_argument("--clamp-on", action="store_true", help="a reusable clamp-on connector pair")
    y.add_argument("--bootstrap", type=int, default=8, help="bootstrap sessions per draw")
    y.set_defaults(fn=cmd_study)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)
