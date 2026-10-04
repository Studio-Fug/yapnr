"""python -m yapnr.rf.order0 write --out DIR | loss --fetched DIR (see `yapnr.rf.order0.write`)."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    from yapnr.rf.order0 import write

    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.order0", description=write.__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="specs, criteria, forward runs and job files")
    w.add_argument("--out", required=True)
    w.add_argument("--s21", type=float, default=-3.4, help="D-O0-11's |S21| criterion (dB)")
    w.add_argument("--offset", type=float, default=0.10, help="R1's loss correction (dB)")
    lo = sub.add_parser("loss", help="run 0b: the loss correction and D-O0-11")
    lo.add_argument("--fetched", required=True, help="the fetched campaign's out/ directory")
    lo.add_argument("--out", help="write the result here (JSON)")
    v = sub.add_parser("variant", help="an optimized run's footprint as a forward run")
    v.add_argument("--run", required=True, help="the (fetched) run directory")
    v.add_argument("--out", required=True)
    v.add_argument("--substrate", help="M-nom, M-eq-em528, W-nom, ... (default: as optimized)")
    v.add_argument("--no-wide", action="store_true", help="keep the spec's bands")
    args = ap.parse_args(argv)
    if args.command == "variant":
        from yapnr.rf.order0 import demos

        print(demos.forward_variant(args.run, args.out, args.substrate, not args.no_wide))
        return 0
    if args.command == "write":
        manifest = write.write(args.out, args.s21, args.offset)
        print(json.dumps({k: manifest[k] for k in ("s21_criterion_db", "s21_optimizer_db")}))
        return 0
    result = write.loss(args.fetched)
    text = json.dumps(result, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
