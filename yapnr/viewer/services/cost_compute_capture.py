"""Evaluate only immutable, recorded global/legality cost contexts."""

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

runtime = Path(os.environ["PNR_COST_RUNTIME"]).resolve()
sys.path.insert(0, str(runtime))
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
from pnr.graph import BoardGraph
from pnr.place.cost_inspect import Objective

req = json.loads(Path(sys.argv[1]).read_text())
record = Path(req["capture"]["path"])
raw = record.read_bytes()
assert hashlib.sha256(raw).hexdigest() == req["capture"]["sha256"]
d = json.loads(raw)
assert (
    d.get("runtime_sources", {}).get("cost_inspect.py")
    == hashlib.sha256((runtime / "pnr/place/cost_inspect.py").read_bytes()).hexdigest()
), "Recorded cost model does not match replay runtime"
ref = req["ref"]
g = BoardGraph.from_json(json.dumps(d["graph"]))
comp = g.component(ref)


def constraints(v):
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


assert json.loads(g.to_json()) == d["graph"]
assert (
    hashlib.sha256(
        json.dumps(d["graph"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    == req["layout_sha256"]
), "Capture/layout mismatch"
context = dict(
    placement_phase=d.get("phase_context", {}),
    label="RECORDED " + d["kind"] + " · " + d.get("scope", d.get("geometry_scope", "")),
    historical_score_recorded=True,
    hashes={str(record): req["capture"]["sha256"]},
    parameters=d.get("parameters", {}),
)
if d["kind"] == "global-objective":
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
    report["field"][
        "scope"
    ] = "Actual optimizer soft-state field; rotation mixtures and other components held fixed"
elif d["kind"] == "routing-probe":
    assert set(d["probe_sources"]) == {"relocate.py", "batch_relocate.py"}
    for name, sha in d["probe_sources"].items():
        assert (
            hashlib.sha256((runtime / "pnr/place" / name).read_bytes()).hexdigest() == sha
        ), "Recorded routing probe does not match replay runtime"
    chosen = d.get("components", {d["component"]["ref"]: d}).get(ref)
    if chosen is None:
        raise ValueError("This component was not evaluated in the selected routing-probe phase")
    report = dict(
        component=chosen["component"],
        recorded_fields=chosen["recorded_fields"],
        discrete_kind="routing-probe",
        field=None,
        field_scope=d["scope"],
        board_total=None,
        scope=d["scope"],
        decision_context=chosen["decision_context"],
    )
else:
    matches = []
    for item in d["decisions"]:
        p = Path(item["path"])
        assert p.resolve().parent == record.resolve().parent
        raw = p.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        v = json.loads(raw)
        if v["ref"] == ref:
            matches.append(v)
    if not matches:
        report = dict(
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
            scope="Held during this legalization: no decision cost; inspect mobility provenance for source lock vs sampled row",
        )
    else:
        assert len(matches) == 1
        v = matches[0]
        recorded_fields = []
        for f in v["candidate_fields"]:
            p = Path(f["path"])
            assert p.resolve().parent == record.resolve().parent
            assert hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"]
            values = np.load(p)["values"]
            recorded_fields.append(
                dict(rotation=f["rotation"], columns=f["columns"], values=values.tolist())
            )
        labels = {
            "target_displacement": "Distance to continuous target",
            "channel_shortage": "Surface escape shortage",
            "local_capacitor_loop": "Local capacitor loop",
        }
        terms = [
            dict(
                key=t["key"],
                label=labels[t["key"]],
                raw_share=t["raw"],
                weights=[] if t["weight"] is None else [t["weight"]],
                weighted=t["weighted"],
                unit="objective term",
                details=t.get("details", []),
            )
            for t in v["terms"]
        ]
        report = dict(
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
            field_scope="Actual legal candidate centers at the time this component was placed; later neighbors were not occupied yet",
            board_total=None,
            scope="recorded-legalizer-decision",
            decision_context={
                k: v[k] for k in ["target", "stationary_neighbors", "grid_mm", "total"]
            },
        )
report.update(
    context=context,
    board_sha256=req["layout_sha256"],
    event_id=req["event_id"],
    geometry_kind="layout",
    model_sha256=hashlib.sha256((runtime / "pnr/place/cost_inspect.py").read_bytes()).hexdigest(),
)
out = Path(sys.argv[2])
tmp = out.with_suffix(".tmp")
tmp.write_text(json.dumps(report, separators=(",", ":")))
tmp.replace(out)
