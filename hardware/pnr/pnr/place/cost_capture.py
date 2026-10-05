"""Opt-in immutable objective/decision snapshots. No routing decisions depend on it."""

import dataclasses
import enum
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def initial_start_context(start):
    """Keep per-start capture and live identity together; restore even on failure."""
    keys = ("PNR_LIVE_CANDIDATE", "PNR_COST_INITIAL_START", "PNR_COST_INITIAL_SEED")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(
        PNR_LIVE_CANDIDATE=(previous[keys[0]] or "source") + "/initial-" + start["id"],
        PNR_COST_INITIAL_START=start["id"],
        PNR_COST_INITIAL_SEED=str(start["seed"]),
    )
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def phase_context():
    return {
        name: os.environ[key]
        for name, key in (
            ("candidate", "PNR_LIVE_CANDIDATE"),
            ("iteration", "PNR_LIVE_ITERATION"),
            ("initial_start", "PNR_COST_INITIAL_START"),
            ("initial_seed", "PNR_COST_INITIAL_SEED"),
        )
        if key in os.environ
    }


def folder():
    value = os.environ.get("PNR_COST_CAPTURE_DIR")
    if not value:
        return None
    p = Path(value)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save(kind, payload):
    p = folder()
    if p is None:
        return None
    sources = {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("model.py", "legalize.py", "cost_inspect.py", "cost_capture.py")
    }
    if os.environ.get("PNR_POWER_FIRST") == "1":
        sources.update(
            {
                name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ("power_first.py", "../power_topology.py")
            }
        )
    raw = json.dumps(
        dict(
            schema="pnr-placement-cost-capture-v1",
            kind=kind,
            phase_context=phase_context(),
            runtime_sources=sources,
            **payload,
        ),
        separators=(",", ":"),
        default=lambda v: v.value if isinstance(v, enum.Enum) else str(v),
    ).encode()
    sha = hashlib.sha256(raw).hexdigest()
    dest = p / (sha + ".json")
    if not dest.exists():
        tmp = p / (uuid.uuid4().hex + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(dest)
    from pnr.live import emit

    if kind != "legalizer-decision":
        emit(
            "placement_cost_capture",
            layout=payload.get("graph"),
            data=dict(
                phase=kind,
                placement_context=phase_context(),
                cost_capture=dict(path=str(dest.resolve()), sha256=sha, kind=kind),
            ),
        )
    return str(dest)


def global_loss(
    graph,
    constraints,
    pos,
    probabilities,
    offsets,
    half,
    loss,
    parameters,
    inflation,
    step,
    roles=None,
    pf_state=None,
    shift=None,
):
    """``shift`` (PNR_COMPACT offset courtyards only): the expected body-centre offsets
    from the origins the optimizer used, replayed by the objective."""
    from pnr.graph import BoardGraph, BoardOutline

    from .cost_inspect import Objective
    from .geometry import outline_size

    g = BoardGraph.from_json(graph.to_json())
    width, height = outline_size(g, constraints)
    g.outline = BoardOutline(width, height)
    for i, c in enumerate(g.components):
        c.pos = tuple(pos[i])
        c.rot = float(90 * max(range(4), key=lambda j: probabilities[i][j]))
    m = Objective(
        g,
        constraints,
        parameters=parameters,
        inflation=inflation,
        effective_offsets=offsets,
        effective_half=half,
        roles=roles,
        pf_state=pf_state,
        **({} if shift is None else dict(effective_shift=shift)),
    )
    report = m.report()
    difference = report["board_total"] - loss
    if abs(difference) > max(0.005, abs(loss) * 2e-6):
        raise ValueError("Cost capture diverges from optimizer objective")
    report.update(
        scope="recorded-optimizer-soft-state-before-update",
        actual_optimizer_loss=loss,
        replay_error=difference,
    )
    extra = {} if roles is None else dict(roles=roles, pf_state=pf_state)
    return save(
        "global-objective",
        dict(
            graph=json.loads(g.to_json()),
            constraints=dataclasses.asdict(constraints),
            parameters=parameters,
            inflation=inflation or {},
            effective_offsets=offsets,
            effective_half=half,
            **({} if shift is None else dict(effective_shift=shift)),
            rotation_probabilities=probabilities,
            optimizer_step=step,
            report=report,
            geometry_scope="display uses argmax rotations; cost uses recorded soft rotation mixture",
            **extra,
        ),
    )


def legalizer_decision(
    graph,
    ref,
    target,
    position,
    rotation,
    neighbors,
    fields,
    chosen,
    grid,
    channel_weight,
    local_details=(),
    wire_weight=None,
):
    """Store actual evaluated legal candidates/terms before another part is placed.

    ``wire_weight`` (PNR_LEGALIZE_HPWL only): the legalizer's wirelength term is on; ``chosen``
    then carries the chosen slot's raw wirelength fourth, the terms gain ``wirelength`` and a
    candidate field with six columns names the sixth ``wire_raw``."""
    import numpy as np

    p = folder()
    if p is None:
        return
    artifacts = []
    for pose, values in fields.items():
        array = np.asarray(values, dtype=float)
        raw = array.tobytes()
        sha = hashlib.sha256(raw).hexdigest()
        dest = p / (sha + ".npz")
        if not dest.exists():
            tmp = p / (uuid.uuid4().hex + ".tmp.npz")
            np.savez_compressed(tmp, values=array)
            tmp.replace(dest)
        artifacts.append(
            dict(
                rotation=pose,
                path=str(dest.resolve()),
                sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
                columns=["x", "y", "target_distance_squared", "channel_raw", "local_loop_weighted"]
                + (["wire_raw"] if array.ndim == 2 and array.shape[1] == 6 else []),
                count=len(array),
            )
        )
    terms = [
        dict(key="target_displacement", raw=chosen[0], weight=1.0, weighted=chosen[0]),
        dict(
            key="channel_shortage",
            raw=chosen[1],
            weight=channel_weight,
            weighted=channel_weight * chosen[1],
        ),
        dict(
            key="local_capacitor_loop",
            raw=sum(t["raw"] for t in local_details),
            weight=(
                local_details[0]["weight"]
                if local_details and len({t["weight"] for t in local_details}) == 1
                else None
            ),
            weighted=chosen[2],
            details=local_details,
        ),
    ]
    if wire_weight is not None:
        terms.append(
            dict(
                key="wirelength",
                raw=chosen[3],
                weight=wire_weight,
                weighted=wire_weight * chosen[3],
            )
        )
    return save(
        "legalizer-decision",
        dict(
            graph=json.loads(graph.to_json()),
            ref=ref,
            target=list(target),
            position=list(position),
            rotation=rotation,
            stationary_neighbors=[c.ref for c in neighbors],
            grid_mm=grid,
            terms=terms,
            total=sum(t["weighted"] for t in terms),
            candidate_fields=artifacts,
            scope="Recorded decision: only previously placed neighbors occupied; later placement can change local channel costs",
        ),
    )


def routing_probe(graph, comp, original, candidates, context, *, accumulator=None):
    """Freeze the actual sampled field and terms on the K-component holdout."""
    terms = original["terms"]
    if abs(sum(t["weighted"] for t in terms) - original["cost"]) > 1e-7 * max(
        1.0, abs(original["cost"])
    ):
        raise ValueError("Probe decomposition mismatch")
    rows = []
    for c in candidates:
        if abs(sum(t["weighted"] for t in c["terms"]) - c["cost"]) > 1e-7 * max(
            1.0, abs(c["cost"])
        ):
            raise ValueError("Probe field decomposition mismatch")
        rows.append([*c["position"], c["cost"], *[t["weighted"] for t in c["terms"]]])
    component = dict(
        ref=comp.ref,
        position=list(comp.pos),
        rotation=comp.rot,
        side=comp.side,
        fixed=False,
        total=original["cost"],
        terms=terms,
    )
    entry = dict(
        component=component,
        recorded_fields=[
            dict(
                rotation=comp.rot,
                columns=["x", "y", "total"] + [t["key"] for t in terms],
                values=rows,
            )
        ],
        decision_context=dict(context, grid_mm=0.25),
    )
    accumulated = accumulator if accumulator is not None else {}
    accumulated[comp.ref] = entry
    return save(
        "routing-probe",
        dict(
            graph=json.loads(graph.to_json()),
            **entry,
            components=dict(accumulated),
            scope="Recorded layered routing proxy on joint-held-out substrate; fixed face/orientation; planes and internal-only nets omitted explicitly; no native DRC",
            probe_sources={
                name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ("relocate.py", "batch_relocate.py")
            },
        ),
    )


def record_global_loss(*args, **kwargs):
    """Cost capture is diagnostic: a failed capture is unavailable, never a score."""
    try:
        return global_loss(*args, **kwargs)
    except Exception as exc:
        import traceback

        failure = dict(
            error=repr(exc),
            traceback=traceback.format_exc(),
            phase_context=phase_context(),
            status="unavailable",
            reason="Cost capture failed; no score or field accepted",
        )
        try:
            p = folder()
            if p is not None:
                (p / ("failed-" + uuid.uuid4().hex + ".json")).write_text(
                    json.dumps(failure, indent=2)
                )
        except OSError:
            pass
        from pnr.live import emit

        emit("cost_capture_failed", data=failure)
        return None
