"""Read-only four-rotation slices of the engine's rigid-pose global cost model."""

import copy
import fnmatch
import json
import math
import os
import sys
import time
from pathlib import Path

from .placement_lab import digest, write
from .placement_objective import TERMS, parse
from .placement_surface_expression import NAMES, evaluate


def compute(request):
    import numpy as np
    import torch

    from pnr.graph import BoardGraph
    from pnr.place.cost_inspect import Objective
    from pnr.place.model import length_mismatch, matched_pin_sets
    from pnr.stack import split_plane_patterns

    from .cost_compute_capture import constraints

    torch.set_num_threads(1)
    os.environ["PNR_PAD_LOCALITY"] = "1"
    os.environ["PNR_PLACEMENT_BANKS"] = "1" if request.get("placement_banks") else "0"
    graph = BoardGraph.from_json(json.dumps(request["frame"]["graph"]))
    cons = constraints(request["source"]["constraints"])
    weights = parse(request["objective_expression"])
    idx = graph.refs.index(request["ref"])
    fields = []
    pitch = request["pitch"]
    for rotation in (0, 90, 180, 270):
        g = copy.deepcopy(graph)
        g.components[idx].rot = rotation
        model = Objective(g, cons, parameters=weights)
        # Match global_place's split-plane selection (not every declared plane).
        patterns = split_plane_patterns(cons, g)
        for net in model.nets:
            net["plane"] = any(fnmatch.fnmatch(net["name"], p) for p in patterns)
        if not all(math.isfinite(v) and 0 < v <= 8000 for v in (model.width, model.height)):
            raise ValueError("Invalid surface board dimensions")
        count = math.ceil(model.width / pitch - 0.5) * math.ceil(model.height / pitch - 0.5)
        if not 1 <= count <= 1600:
            raise ValueError("Choose a pitch with 1–1600 sites per rotation")
        xs = np.arange(pitch / 2, model.width, pitch)
        ys = np.arange(pitch / 2, model.height, pitch)
        sites = np.array([(x, y) for y in ys for x in xs])
        if len(sites) > 1600 or not len(sites):
            raise ValueError("Choose a pitch with 1–1600 sites per rotation")
        matches = matched_pin_sets(g, cons, model.keys)
        values = []
        for start in range(0, len(sites), 16):
            chunk = sites[start : start + 16]
            positions = np.broadcast_to(model.positions, (len(chunk), model.n, 2)).copy()
            positions[:, idx] = chunk
            raw, weighted = model.evaluate(positions)
            raw, weighted = raw.sum(1), weighted.sum(1)
            terms = {name: np.zeros(len(chunk)) for name in NAMES}
            for i, key in enumerate(model.term_keys):
                if key in terms:
                    terms[key] = raw[:, i]
            # Matching is evaluated by the actual engine helper; source-only terms
            # retain authored weights. Capacitor-loop diagnostics are not a global_place term.
            if matches:
                pins = positions[:, model.pin_owner] + model.offsets
                terms["length_matching"] = np.array(
                    [
                        float(
                            length_mismatch(
                                matches,
                                torch.tensor(p[:, 0]),
                                torch.tensor(p[:, 1]),
                                model.cfg["gamma"],
                            )
                        )
                        for p in pins
                    ]
                )
            terms["source_constraints"] = terms["length_matching"].copy()
            for i, key in enumerate(model.term_keys):
                if key in ("edge_alignment", "group_radius", "region", "alignment"):
                    terms["source_constraints"] += weighted[:, i]
            terms["objective"] = terms["source_constraints"].copy()
            for key, parameter in TERMS.items():
                if parameter:
                    terms["objective"] += terms[key] * weights[parameter]
            values.extend(
                np.broadcast_to(evaluate(request["expression"], terms), (len(chunk),)).tolist()
            )
        best = min(range(len(values)), key=values.__getitem__)
        fields.append(
            dict(
                rotation=rotation,
                nx=len(xs),
                ny=len(ys),
                origin=[float(xs[0]), float(ys[0])],
                values=values,
                minimum=values[best],
                maximum=max(values),
                best_position=sites[best].tolist(),
            )
        )
    return dict(
        ref=request["ref"],
        expression=request["expression"],
        objective_expression=request["objective_expression"],
        pitch_mm=pitch,
        fields=fields,
        minimum=min(f["minimum"] for f in fields),
        maximum=max(f["maximum"] for f in fields),
        frame_step=request["frame"].get("step"),
        frame_sha256=request["frame_sha256"],
        current_position=graph.components[idx].pos,
        current_rotation=graph.components[idx].rot,
        fixed=graph.components[idx].ref in model.fixed or graph.components[idx].locked,
        scope=(
            "Rigid-pose global objective slice: other parts held at displayed cardinal rotations; "
            "absolute whole-board cost, not soft-mixture loss or a legal-site certificate"
        ),
    )


def main(folder):
    request = json.loads((folder / "request.json").read_text())
    if (
        digest(Path(__file__)) != request["evaluator_sha256"]
        or digest(Path(__file__).with_name("placement_surface_expression.py"))
        != request["expression_sha256"]
    ):
        raise ValueError("Surface evaluator changed before execution")
    engine = Path(os.environ["PNR_COST_RUNTIME"])
    for name, sha in request["engine_sources"].items():
        if digest(engine / name) != sha:
            raise ValueError("Frozen engine changed")
    sys.path.insert(0, str(engine))
    start = time.monotonic()
    result = compute(request)
    result["seconds"] = time.monotonic() - start
    write(folder / "result.json", result)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
