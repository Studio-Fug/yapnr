"""Evaluate only immutable, recorded global/legality cost contexts (a child process).

Usage: ``python -m yapnr.viewer.services.cost_compute_capture <request.json> <out.json>``, with
``PNR_COST_RUNTIME`` naming the engine runtime that must match the recorded cost model.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

HELD_SCOPE = (
    "Held during this legalization: no decision cost; inspect mobility provenance for source lock"
    " vs sampled row"
)
FIELD_SCOPE = (
    "Actual legal candidate centers at the time this component was placed; later neighbors were"
    " not occupied yet"
)
OPTIMIZER_FIELD_SCOPE = (
    "Actual optimizer soft-state field; rotation mixtures and other components held fixed"
)
TERM_LABELS = {
    "target_displacement": "Distance to continuous target",
    "channel_shortage": "Surface escape shortage",
    "local_capacitor_loop": "Local capacitor loop",
    "wirelength": "Wirelength (half-perimeter, PNR_LEGALIZE_HPWL)",
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def constraints(v):
    from pnr.constraints import (
        BoardSpec,
        CompiledConstraints,
        Constraint,
        DiffPair,
        Enforcement,
        FabProfile,
        LengthMatch,
        NetClass,
    )

    v = dict(v)
    v["board"] = BoardSpec(**v["board"])
    v["fab"] = FabProfile(**v["fab"])
    v["constraints"] = [
        Constraint(**dict(c, enforcement=Enforcement(c["enforcement"]))) for c in v["constraints"]
    ]
    for name, cls in [
        ("net_classes", NetClass),
        ("diff_pairs", DiffPair),
        ("length_matches", LengthMatch),
    ]:
        v[name] = [cls(**x) for x in v.get(name, [])]
    return CompiledConstraints(**v)


def global_objective(d, g, ref):
    from pnr.place.cost_inspect import Objective

    m = Objective(
        g,
        constraints(d["constraints"]),
        parameters=d["parameters"],
        inflation=d["inflation"],
        effective_offsets=d["effective_offsets"],
        effective_half=d["effective_half"],
    )
    report = m.report()
    report["component"] = report["components"][ref]
    report["field"] = m.field(ref, 1.5)
    report["scope"] = d["report"]["scope"]
    report["actual_optimizer_loss"] = d["report"]["actual_optimizer_loss"]
    report["field"]["scope"] = OPTIMIZER_FIELD_SCOPE
    return report


def routing_probe(d, ref, runtime):
    assert set(d["probe_sources"]) == {"relocate.py", "batch_relocate.py"}
    for name, sha in d["probe_sources"].items():
        assert (
            sha256((runtime / "pnr/place" / name).read_bytes()) == sha
        ), "Recorded routing probe does not match replay runtime"
    chosen = d.get("components", {d["component"]["ref"]: d}).get(ref)
    if chosen is None:
        raise ValueError("This component was not evaluated in the selected routing-probe phase")
    return dict(
        component=chosen["component"],
        recorded_fields=chosen["recorded_fields"],
        discrete_kind="routing-probe",
        field=None,
        field_scope=d["scope"],
        board_total=None,
        scope=d["scope"],
        decision_context=chosen["decision_context"],
    )


def legalizer_decision(d, record, comp, ref):
    import numpy as np

    matches = []
    for item in d["decisions"]:
        p = Path(item["path"])
        assert p.resolve().parent == record.resolve().parent
        raw = p.read_bytes()
        assert sha256(raw) == item["sha256"]
        v = json.loads(raw)
        if v["ref"] == ref:
            matches.append(v)
    if not matches:
        return dict(
            component=dict(
                ref=ref,
                position=list(comp.pos),
                rotation=comp.rot,
                side=comp.side,
                fixed=True,
                mobility=d.get("mobility", {}).get(ref),
                terms=[],
                total=None,
            ),
            field=None,
            board_total=None,
            scope=HELD_SCOPE,
        )
    assert len(matches) == 1
    v = matches[0]
    recorded_fields = []
    for f in v["candidate_fields"]:
        p = Path(f["path"])
        assert p.resolve().parent == record.resolve().parent
        assert sha256(p.read_bytes()) == f["sha256"]
        values = np.load(p)["values"]
        recorded_fields.append(
            dict(rotation=f["rotation"], columns=f["columns"], values=values.tolist())
        )
    terms = [
        dict(
            key=t["key"],
            label=TERM_LABELS[t["key"]],
            raw_share=t["raw"],
            weights=[] if t["weight"] is None else [t["weight"]],
            weighted=t["weighted"],
            unit="objective term",
            details=t.get("details", []),
        )
        for t in v["terms"]
    ]
    return dict(
        component=dict(
            ref=ref,
            position=v["position"],
            rotation=v["rotation"],
            side=comp.side,
            fixed=False,
            terms=terms,
            total=v["total"],
        ),
        field=None,
        recorded_fields=recorded_fields,
        field_scope=FIELD_SCOPE,
        board_total=None,
        scope="recorded-legalizer-decision",
        decision_context={k: v[k] for k in ["target", "stationary_neighbors", "grid_mm", "total"]},
    )


def main(argv):
    runtime = Path(os.environ["PNR_COST_RUNTIME"]).resolve()
    sys.path.insert(0, str(runtime))
    from pnr.graph import BoardGraph

    model_sha = sha256((runtime / "pnr/place/cost_inspect.py").read_bytes())
    req = json.loads(Path(argv[1]).read_text())
    record = Path(req["capture"]["path"])
    raw = record.read_bytes()
    assert sha256(raw) == req["capture"]["sha256"]
    d = json.loads(raw)
    assert (
        d.get("runtime_sources", {}).get("cost_inspect.py") == model_sha
    ), "Recorded cost model does not match replay runtime"
    ref = req["ref"]
    g = BoardGraph.from_json(json.dumps(d["graph"]))
    comp = g.component(ref)
    assert json.loads(g.to_json()) == d["graph"]
    layout = json.dumps(d["graph"], sort_keys=True, separators=(",", ":")).encode()
    assert sha256(layout) == req["layout_sha256"], "Capture/layout mismatch"
    context = dict(
        placement_phase=d.get("phase_context", {}),
        label="RECORDED " + d["kind"] + " · " + d.get("scope", d.get("geometry_scope", "")),
        historical_score_recorded=True,
        hashes={str(record): req["capture"]["sha256"]},
        parameters=d.get("parameters", {}),
    )
    if d["kind"] == "global-objective":
        report = global_objective(d, g, ref)
    elif d["kind"] == "routing-probe":
        report = routing_probe(d, ref, runtime)
    else:
        report = legalizer_decision(d, record, comp, ref)
    report.update(
        context=context,
        board_sha256=req["layout_sha256"],
        event_id=req["event_id"],
        geometry_kind="layout",
        model_sha256=model_sha,
    )
    out = Path(argv[2])
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, separators=(",", ":")))
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
