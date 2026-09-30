"""Replay the engine's placement cost model for one routed checkpoint (a child process).

Usage: ``python -m yapnr.viewer.services.cost_compute <request.json> <out.json>``, with
``PNR_COST_RUNTIME`` naming the engine runtime (the directory containing ``pnr``), which comes
first on the import path. The request binds the extracted board graph, the recorded placement
context (hashes of its inputs, constraints and parameters) and an optional component reference.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Placement contexts with capacitor annotation sources need pnr.capacitor_intent, which was never
# committed (the migration plan, appendix B); the service reports them as unavailable.
NO_CAPACITOR_INTENT = (
    "this placement context uses capacitor-intent annotations, which the engine does not provide"
    " yet (pnr.capacitor_intent)"
)


def main(argv):
    runtime = Path(os.environ["PNR_COST_RUNTIME"]).resolve()
    sys.path.insert(0, str(runtime))
    import yaml

    from pnr.constraints import compile_constraints
    from pnr.graph import BoardGraph
    from pnr.place.cost_inspect import Objective

    request = json.loads(Path(argv[1]).read_text())
    context = request["context"]
    if context.get("annotation_sources"):
        print(NO_CAPACITOR_INTENT, file=sys.stderr)
        return 3
    start = time.monotonic()
    for p, h in context["hashes"].items():
        assert hashlib.sha256(Path(p).read_bytes()).hexdigest() == h, "Context changed"
    g = BoardGraph.from_json(Path(request["graph"]).read_text())
    raw = yaml.safe_load(Path(context["constraints"]).read_text())
    holes = {v["name"] for v in raw.get("mounting_hole", [])}
    g.components = [c for c in g.components if c.ref not in holes]
    for net in g.nets:
        net.pins = [p for p in net.pins if p[0] not in holes]
    cc = compile_constraints(
        raw,
        g.refs,
        {c.address: c.ref for c in g.components if c.address},
        {f"{c.address}:{p.name}": p.net for c in g.components for p in c.pads if c.address},
    )
    m = Objective(g, cc, parameters=context["parameters"])
    report = m.report()
    report.update(
        context=context,
        board_sha256=request["board_sha256"],
        event_id=request["event_id"],
        model_sha256=hashlib.sha256(
            (runtime / "pnr/place/cost_inspect.py").read_bytes()
        ).hexdigest(),
    )
    if request.get("ref"):
        ref = request["ref"]
        report["component"] = report["components"][ref]
        report["field"] = m.field(ref, 1.5)
    report["seconds"] = time.monotonic() - start
    p = Path(argv[2])
    temp = p.with_suffix(".tmp")
    temp.write_text(json.dumps(report, separators=(",", ":")))
    temp.replace(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
