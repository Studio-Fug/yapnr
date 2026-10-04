"""The Palace validation models (plan cases a-c): planar documents, meshes and run configs.

- (a) ``line-{msl,gcpw}-{5,10}mm``: straight 50-ohm lines on the 4-mil RO4835 feed stack between
  wave ports; 2D mode solves at 1, 30 and 62 GHz on the P1 face and a 54-70 GHz sweep.
- (b) ``patch-w`` (board into the south wall, wave port; the comparison geometry) and
  ``patch-finite`` (the stage-2 finite board with a lumped port; the tie-back), from an
  ``rfmacro`` record.
- (c) ``tx12-A`` / ``tx12-B``: the radar60 TX feed models with and without the GND slivers, from
  the openEMS model JSONs, over the openEMS run's PML interior (the domain openEMS actually
  solved), walls moved off the via barrels by ``fit_box``.

Each case directory gets ``model.json`` (the planar document), ``mesh.msh``/``mesh.json``, and
the Palace configs: ``palace-amr.json`` (adaptive refinement at a few frequencies around the
feature, saving the adapted mesh) and ``palace-sweep.json`` (the sweep on the adapted mesh), plus
``palace-uniform.json`` (the sweep on the initial mesh) and, for lines, ``palace-mode-*.json``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from yapnr.rf.palace import config, mesh
from yapnr.rf.planar import adapters, model

AMR_DEFAULT = dict(Tol=1e-2, MaxIts=6, UpdateFraction=0.7, MaxSize=4_000_000)


def case_settings(name: str) -> Dict[str, Any]:
    """Sweep, excitation and refinement settings of a case (GHz).

    ``amr`` overrides ``AMR_DEFAULT`` (Palace stops refining once a solve has more than
    ``MaxSize`` unknowns, so the last mesh can be up to about twice that); ``adaptive_max_samples``
    caps the adaptive sweep's full solves (Palace's default is 20); ``copper_bc`` "impedance"
    writes the copper sheets as Impedance boundaries frozen at the band centre (see
    ``config.sheet_impedance``: Palace b797ea8 aborts a multi-rank mode solve whose cross-section
    a Conductivity sheet crosses)."""
    if name.startswith("line-"):
        return dict(
            band=(54.0, 70.0, 0.5),
            adaptive_tol=1e-3,
            excite=["P1"],
            amr_freqs=[62.0],
            amr=dict(MaxIts=3, MaxSize=1_200_000),
            modes=[1.0, 30.0, 62.0],
            copper_bc="impedance",
        )
    if name.startswith("patch-"):
        return dict(
            band=(58.0, 66.0, 0.05),
            adaptive_tol=1e-3,
            adaptive_max_samples=30,
            excite=["P1"],
            amr_freqs=[61.0, 61.9, 62.8],
            amr=dict(MaxIts=1, MaxSize=1_500_000),
            copper_bc="impedance",
        )
    if name.startswith("tx12-"):
        return dict(
            band=(54.0, 70.0, 0.025),
            adaptive_tol=1e-3,
            adaptive_max_samples=30,
            excite=["TX1.P0"],
            amr_freqs=[59.0, 60.0, 61.0, 63.8],
            amr=dict(MaxIts=1, MaxSize=2_000_000),
            copper_bc="impedance",
        )
    raise KeyError(name)


def openems_pml_box(result: Dict[str, Any]) -> List[float]:
    """[x0, x1, y0, y1] inside the PML of an openEMS feed run (its ``meta.pml``)."""
    pml = result["meta"]["pml"]
    return [float(pml["w"]), float(pml["e"]), float(pml["s"]), float(pml["n"])]


def feed_case(fm: Dict[str, Any], box: Sequence[float], name: str, source: str) -> Dict[str, Any]:
    pts = [q for piece in list(fm["gnd"][0]) + list(fm["gnd"][1]) for q in piece]
    pts += [
        q
        for inner, zone in fm["nets"].values()
        for piece in list(inner) + list(zone)
        for q in piece
    ]
    fitted, notes = adapters.fit_box(box, fm["vias"], pts)
    doc = adapters.from_feedmodel(fm, box=fitted, name=name, source=source)
    doc["provenance"]["box_requested"] = [round(float(v), 6) for v in box]
    doc["provenance"]["box_notes"] = notes
    return doc


def write_case(
    doc: Dict[str, Any],
    out: str,
    mesh_opts: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mesh one case and write its model, mesh and configs into ``out``; returns the mesh record
    with the config file names."""
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "model.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    rec = mesh.build(doc, out, **(mesh_opts or {}))
    st = settings or case_settings(doc["name"])
    rec["configs"] = write_configs(doc, rec, out, st)
    with open(os.path.join(out, "mesh.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
        fh.write("\n")
    return rec


def write_configs(
    doc: Dict[str, Any], rec: Dict[str, Any], out: str, st: Dict[str, Any]
) -> List[str]:
    """The run configs of a meshed case (``rec`` its mesh record) into ``out``; their names.

    ``palace-uniform.json`` sweeps the initial mesh, ``palace-amr.json`` refines it at the
    ``amr_freqs`` (saving every iteration's results and the adapted mesh) and
    ``palace-sweep.json`` sweeps the adapted mesh; ``palace-mode-<f>GHz.json`` (the port face,
    shielded as the 3D port sees it) and ``palace-mode-wall-<f>GHz.json`` (the whole wall, open)
    are the 2D mode solves on P1's plane."""
    f0, f1, df = st["band"]
    copper = dict(
        copper_bc=st.get("copper_bc", "conductivity"), copper_f_ghz=st.get("copper_f_ghz")
    )
    sweep = dict(
        adaptive_tol=st["adaptive_tol"],
        adaptive_max_samples=st.get("adaptive_max_samples"),
        excite=st["excite"],
        **copper,
    )
    cfgs = {
        "palace-uniform.json": config.driven(
            doc, rec, f0, f1, df, output="postpro-uniform", **sweep
        ),
        "palace-amr.json": config.driven(
            doc,
            rec,
            f0,
            f1,
            df,
            excite=st["excite"],
            amr=dict(AMR_DEFAULT, **st.get("amr", {})),
            amr_freqs=st["amr_freqs"],
            output="postpro-amr",
            **copper,
        ),
        "palace-sweep.json": config.driven(
            doc,
            rec,
            f0,
            f1,
            df,
            mesh_file="postpro-amr/mesh.meshgz",
            output="postpro-sweep",
            **sweep,
        ),
    }
    for f in st.get("modes", []):
        for face, tag in (("port", ""), ("wall", "wall-")):
            cfgs[f"palace-mode-{tag}{f:g}GHz.json"] = config.boundary_mode(
                doc,
                rec,
                "P1",
                f,
                face=face,
                copper_bc=copper["copper_bc"],
                output=f"postpro-mode-{tag}{f:g}GHz",
            )
    for fname, cfg in cfgs.items():
        config.check(cfg)
        with open(os.path.join(out, fname), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
            fh.write("\n")
    return sorted(cfgs)


def build_all(
    out: str,
    record: Optional[Dict[str, Any]] = None,
    feeds: Optional[Dict[str, Dict[str, Any]]] = None,
    feed_box: Optional[Sequence[float]] = None,
    only: Optional[Sequence[str]] = None,
    mesh_opts: Optional[Dict[str, Any]] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Dict[str, Any]]:
    """Every case whose inputs are given: lines always, patches with an rfmacro ``record``,
    feeds with ``feeds`` {name: prep.py model} and ``feed_box`` (the openEMS PML interior)."""
    docs: Dict[str, Dict[str, Any]] = {}
    for kind in ("msl", "gcpw"):
        for length in (5.0, 10.0):
            d = adapters.line_model(kind, length=length)
            docs[d["name"]] = d
    if record is not None:
        for variant in ("w", "finite"):
            d = adapters.patch_from_record(record, variant=variant)
            docs[d["name"]] = d
    for name, fm in (feeds or {}).items():
        if feed_box is None:
            raise ValueError("feed models need the domain box (the openEMS PML interior)")
        docs[name] = feed_case(fm, feed_box, name, source=f"radar60 rf-uniform models/{name}.json")
    recs = {}
    for name, d in docs.items():
        if only and name not in only:
            continue
        rec = write_case(d, os.path.join(out, name), mesh_opts)
        recs[name] = rec
        log(mesh.summary(rec))
    return recs


def table(recs: Dict[str, Dict[str, Any]]) -> str:
    """A Markdown table of the meshes."""
    rows = [
        "| Model | Tetrahedra | Nodes | DOFs p=2 / p=3 (est.) | gamma min / p01 / median "
        "| edge_h, air (mm) | Mesh time |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, r in recs.items():
        q = r["quality"]
        d2, d3 = r["dofs_estimate"]["2"] / 1e6, r["dofs_estimate"]["3"] / 1e6
        rows.append(
            f"| {name} | {r['tetrahedra']:,} | {r['nodes']:,} | {d2:.2f} M / {d3:.2f} M | "
            f"{q['gamma_min']} / {q['gamma_p01']} / {q['gamma_median']} | "
            f"{r['sizes']['edge_h']}, {r['sizes']['air']} | {r['mesh_seconds']} s |"
        )
    return "\n".join(rows)


def port_table(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Port faces of a document, for reports."""
    g = model.port_geometry(doc)
    return [
        dict(
            name=p["name"],
            kind=p["kind"],
            wall=g[p["name"]].get("wall"),
            offset=round(g[p["name"]]["offset"], 4),
            half_width=g[p["name"]]["half_width_actual"],
        )
        for p in doc["ports"]
    ]
