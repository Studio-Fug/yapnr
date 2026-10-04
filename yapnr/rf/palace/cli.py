"""``python -m yapnr.rf.palace``: mesh planar models and write Palace configurations.

    mesh MODEL.json --out DIR [--copper sheet|solid|pec] [--edge-h MM] [--grade G] [--threads N]
    driven DIR --band F0 F1 DF [--adaptive-tol T] [--excite PORT ...] [--order P]
           [--amr-its N --amr-freqs F ...] [--mesh-file FILE] [--name palace.json]
    mode DIR --port PORT --freq F [--name ...]
    case MODEL.json --out DIR [--band F0 F1 DF --excite PORT ... --amr-freqs F ...]
    configs DIR ...                     # rewrite a meshed case's configs (validation settings)
    validate CONFIG.json ...
    validation --out DIR [--record RFMACRO.json] [--feed NAME=MODEL.json ...]
               [--feed-result OPENEMS_RESULT.json] [--only NAME ...]

``DIR`` holds ``model.json`` (the planar document as meshed), ``mesh.msh`` and ``mesh.json``.
``case`` does it all in one step (the ``prepare`` of a Palace task): mesh, then the uniform,
refinement and sweep configurations (``validation.write_case``), with the settings of a validation
case or the ones given. Configs are checked against the vendored Palace schema before they are
written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from yapnr.rf.palace import config, mesh, schema, validation
from yapnr.rf.planar import model


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
        fh.write("\n")


def cmd_mesh(a) -> int:
    doc = model.check(_load(a.model))
    if a.copper:
        doc = model.with_layer_model(doc, a.copper)
    os.makedirs(a.out, exist_ok=True)
    _dump(doc, os.path.join(a.out, "model.json"))
    rec = mesh.build(
        doc, a.out, edge_h=a.edge_h, grade=a.grade, threads=a.threads, algorithm3d=a.algorithm3d
    )
    print(mesh.summary(rec))
    return 0


def cmd_driven(a) -> int:
    doc = _load(os.path.join(a.dir, "model.json"))
    rec = _load(os.path.join(a.dir, "mesh.json"))
    amr = dict(validation.AMR_DEFAULT, MaxIts=a.amr_its) if a.amr_its else None
    cfg = config.driven(
        doc,
        rec,
        *a.band,
        adaptive_tol=a.adaptive_tol,
        excite=a.excite or None,
        order=a.order,
        amr=amr,
        amr_freqs=a.amr_freqs,
        mesh_file=a.mesh_file,
        output=a.output,
    )
    config.check(cfg)
    _dump(cfg, os.path.join(a.dir, a.name))
    print(os.path.join(a.dir, a.name))
    return 0


def cmd_mode(a) -> int:
    doc = _load(os.path.join(a.dir, "model.json"))
    rec = _load(os.path.join(a.dir, "mesh.json"))
    cfg = config.check(
        config.boundary_mode(
            doc,
            rec,
            a.port,
            a.freq,
            order=a.order,
            output=a.output or f"postpro-mode-{a.freq:g}GHz",
        )
    )
    name = a.name or f"palace-mode-{a.freq:g}GHz.json"
    _dump(cfg, os.path.join(a.dir, name))
    print(os.path.join(a.dir, name))
    return 0


def cmd_case(a) -> int:
    doc = model.check(_load(a.model))
    if a.copper:
        doc = model.with_layer_model(doc, a.copper)
    try:
        st = validation.case_settings(doc["name"])
    except KeyError:
        st = None
    if a.band:
        st = dict(st or {}, band=tuple(a.band))
    if st is None or "band" not in st:
        raise SystemExit(f"no settings for case {doc['name']!r}: give --band (and --excite)")
    st.setdefault("adaptive_tol", 1e-4)
    if a.excite:
        st["excite"] = a.excite
    st.setdefault("excite", [p["name"] for p in doc["ports"] if p.get("excite")])
    if a.amr_freqs:
        st["amr_freqs"] = a.amr_freqs
    st.setdefault("amr_freqs", [0.5 * (st["band"][0] + st["band"][1])])
    rec = validation.write_case(doc, a.out, dict(edge_h=a.edge_h, threads=a.threads), st)
    print(mesh.summary(rec))
    print("configs: " + ", ".join(rec["configs"]))
    return 0


def cmd_configs(a) -> int:
    for d in a.dirs:
        doc = _load(os.path.join(d, "model.json"))
        rec = _load(os.path.join(d, "mesh.json"))
        rec["configs"] = validation.write_configs(
            doc, rec, d, validation.case_settings(doc["name"])
        )
        _dump(rec, os.path.join(d, "mesh.json"))
        print(f"{d}: " + ", ".join(rec["configs"]))
    return 0


def cmd_validate(a) -> int:
    sch = schema.load()
    if sch is None:
        print(
            "no Palace schema in this installation (third_party/palace/config-schema.json)",
            file=sys.stderr,
        )
        return 2
    bad = 0
    for path in a.configs:
        errs = schema.validate(_load(path), sch)
        print(
            f"{path}: {'ok' if not errs else str(len(errs)) + ' findings'} (schema {schema.version(sch)})"
        )
        for e in errs or []:
            print("  " + e)
        bad += bool(errs)
    return 1 if bad else 0


def cmd_validation(a) -> int:
    record = _load(a.record) if a.record else None
    feeds = {}
    for item in a.feed:
        name, path = item.split("=", 1)
        feeds[name] = _load(path)
    box = validation.openems_pml_box(_load(a.feed_result)) if a.feed_result else None
    recs = validation.build_all(
        a.out,
        record=record,
        feeds=feeds,
        feed_box=box,
        only=a.only,
        mesh_opts=dict(threads=a.threads),
    )
    print(validation.table(recs))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m yapnr.rf.palace", description=__doc__.split("\n\n")[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("mesh", help="mesh a planar model")
    p.add_argument("model")
    p.add_argument("--out", required=True)
    p.add_argument("--copper", choices=model.LAYER_MODELS)
    p.add_argument("--edge-h", type=float)
    p.add_argument("--grade", type=float)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--algorithm3d", type=int)
    p.set_defaults(fn=cmd_mesh)
    p = sub.add_parser("driven", help="write a Driven (S-parameter) config")
    p.add_argument("dir")
    p.add_argument("--band", type=float, nargs=3, required=True, metavar=("F0", "F1", "DF"))
    p.add_argument("--adaptive-tol", type=float)
    p.add_argument("--excite", nargs="*")
    p.add_argument("--order", type=int, default=2)
    p.add_argument("--amr-its", type=int, default=0)
    p.add_argument("--amr-freqs", type=float, nargs="*")
    p.add_argument("--mesh-file")
    p.add_argument("--output", default="postpro")
    p.add_argument("--name", default="palace.json")
    p.set_defaults(fn=cmd_driven)
    p = sub.add_parser("mode", help="write a BoundaryMode config on a wave-port face")
    p.add_argument("dir")
    p.add_argument("--port", required=True)
    p.add_argument("--freq", type=float, required=True)
    p.add_argument("--order", type=int, default=2)
    p.add_argument("--output")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_mode)
    p = sub.add_parser("case", help="mesh a model and write its run configs (a task's prepare)")
    p.add_argument("model")
    p.add_argument("--out", required=True)
    p.add_argument("--copper", choices=model.LAYER_MODELS)
    p.add_argument("--band", type=float, nargs=3, metavar=("F0", "F1", "DF"))
    p.add_argument("--excite", nargs="*")
    p.add_argument("--amr-freqs", type=float, nargs="*")
    p.add_argument("--edge-h", type=float)
    p.add_argument("--threads", type=int, default=1)
    p.set_defaults(fn=cmd_case)
    p = sub.add_parser("configs", help="rewrite the run configs of meshed validation cases")
    p.add_argument("dirs", nargs="+")
    p.set_defaults(fn=cmd_configs)
    p = sub.add_parser("validate", help="check configs against the vendored Palace schema")
    p.add_argument("configs", nargs="+")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("validation", help="build the validation models (plan cases a-c)")
    p.add_argument("--out", required=True)
    p.add_argument("--record", help="rfmacro record JSON (the patch cases)")
    p.add_argument("--feed", action="append", default=[], help="NAME=prep.py model JSON")
    p.add_argument(
        "--feed-result", help="an openEMS feed run's result.json (its PML interior is the box)"
    )
    p.add_argument("--only", nargs="*")
    p.add_argument("--threads", type=int, default=1)
    p.set_defaults(fn=cmd_validation)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
