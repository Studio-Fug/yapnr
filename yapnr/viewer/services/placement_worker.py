"""One reproducible, single-threaded local placement experiment, never a routing pass."""

import copy
import json
import os
import sys
import time
from pathlib import Path

from .placement_lab import digest, write
from .placement_objective import parse


def main(folder):
    engine = Path(os.environ["PNR_COST_RUNTIME"])
    sys.path.insert(0, str(engine))
    import platform

    import numpy as np
    import torch

    from pnr.graph import BoardGraph
    from pnr.place.channels import ChannelModel
    from pnr.place.geometry import outline_size, resolve_fixed_poses
    from pnr.place.legalize import legalize, legalize_constraint_kwargs
    from pnr.place.metrics import hard_violations
    from pnr.place.model import global_place
    from pnr.place.pad_locality import audit, legalize_kwargs

    from .cost_compute_capture import constraints

    write(
        folder / "runtime.json",
        dict(
            python=platform.python_version(),
            torch=torch.__version__,
            numpy=np.__version__,
            platform=platform.system(),
            machine=platform.machine(),
            threads=1,
            deterministic_algorithms=True,
            python_hash_seed=os.environ.get("PYTHONHASHSEED"),
        ),
    )
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    request = json.loads((folder / "request.json").read_text())
    for name, sha in request["engine_sources"].items():
        if digest(engine / name) != sha:
            raise ValueError("Engine changed before execution")
    source = json.loads((folder / "input.json").read_text())
    graph = BoardGraph.from_json(json.dumps(source["graph"]))
    cons = constraints(source["constraints"])
    width, height = outline_size(graph, cons)
    os.environ.update(PNR_PAD_LOCALITY="1", PNR_COST_CAPTURE_DIR=str(folder / "captures"))
    status = json.loads((folder / "status.json").read_text())
    revision = None
    expression = request["expression"]
    weights = parse(expression)
    start = time.monotonic()
    trace = (folder / "iterations.jsonl").open("a")

    def control(step):
        nonlocal revision, weights, expression
        while True:
            c = json.loads((folder / "control.json").read_text())
            if c["stop"]:
                raise InterruptedError("Stopped by user; saved frames retained")
            if c["revision"] != revision:
                weights = parse(c["expression"])
                expression, revision = c["expression"], c["revision"]
                trace.write(
                    json.dumps(
                        dict(
                            kind="control",
                            step=step,
                            revision=revision,
                            expression=expression,
                            weights=weights,
                            paused=c["paused"],
                        )
                    )
                    + "\n"
                )
                trace.flush()
            status.update(
                state="paused" if c["paused"] else "running",
                step=step,
                control_revision=revision,
                expression=expression,
            )
            if not c["paused"]:
                return weights
            write(folder / "status.json", status)
            time.sleep(0.15)

    def initial_snapshot(positions, probabilities):
        frame = copy.deepcopy(graph)
        for comp, point, p in zip(frame.components, positions, probabilities):
            comp.pos = point
            comp.rot = 90 * max(range(4), key=lambda i: p[i])
        initial = dict(
            kind="initialization",
            step=0,
            completed_iterations=0,
            graph=json.loads(frame.to_json()),
            positions=positions,
            rotation_probabilities=probabilities,
            scope="Seeded initial placement; zero optimizer updates; shown rotations are argmax of soft mixture",
        )
        write(folder / "initial.json", initial)
        write(folder / "frame.json", initial)
        trace.write(json.dumps({k: v for k, v in initial.items() if k != "graph"}) + "\n")
        trace.flush()

    def progress(step, positions, probabilities, loss):
        frame = copy.deepcopy(graph)
        for comp, point, p in zip(frame.components, positions, probabilities):
            comp.pos = point
            comp.rot = 90 * max(range(4), key=lambda i: p[i])
        record = dict(
            step=step,
            loss=loss,
            revision=revision,
            positions=positions,
            rotation_probabilities=probabilities,
        )
        trace.write(json.dumps(record, separators=(",", ":")) + "\n")
        trace.flush()
        write(
            folder / "frame.json",
            dict(
                graph=json.loads(frame.to_json()),
                step=step,
                loss=loss,
                revision=revision,
                scope=(
                    "Continuous optimization; overlapping bodies permitted; "
                    "shown rotations are argmax of soft mixture"
                ),
            ),
        )
        status.update(step=step + 1, loss=loss, elapsed_seconds=time.monotonic() - start)
        write(folder / "status.json", status)
        if request["frame_delay_ms"]:
            time.sleep(request["frame_delay_ms"] / 1000)

    try:
        xy, turns = global_place(
            graph,
            cons,
            width,
            height,
            seed=request["seed"],
            iters=request["iterations"],
            control=control,
            progress=progress,
            initialized=initial_snapshot,
            **weights
        )
        cont = copy.deepcopy(graph)
        for comp in cont.components:
            comp.pos, comp.rot = xy[comp.ref], turns[comp.ref]
        write(folder / "continuous.json", json.loads(cont.to_json()))
        control(request["iterations"])
        status.update(state="legalizing")
        write(folder / "status.json", status)
        from pnr.constraints import compile_routing_rules

        rules = source.get("rules") or compile_routing_rules(cons, [n.name for n in graph.nets])
        channels = ChannelModel(cont, rules)
        placed = legalize(
            cont,
            width,
            height,
            allow_rotation=True,
            channel_model=channels,
            grid_mm=0.25,
            **legalize_constraint_kwargs(cont, cons, resolve_fixed_poses(cont, cons)),
            **legalize_kwargs(cont, cons)
        )
        write(folder / "placed.json", json.loads(placed.to_json()))
        captures = [(p, json.loads(p.read_text())) for p in (folder / "captures").glob("*.json")]
        complete = next(d for p, d in captures if d.get("kind") == "legalizer-complete")
        review = dict(
            decisions=[
                dict(item, ref=json.loads(Path(item["path"]).read_text())["ref"])
                for item in complete["decisions"]
            ],
            motion=complete.get("motion"),
            graph=complete["graph"],
            input_graph=complete["input_graph"],
            backtracks=complete["backtracks"],
            scope=complete["scope"],
        )
        write(folder / "review.json", review)
        violations = hard_violations(placed, cons)
        status.update(
            state="complete",
            legal=not any(violations.values()),
            violations=violations,
            decisions=len(review["decisions"]),
            locality=audit(placed, cons),
            message="Placement only; native routing and electrical checks not run",
        )
        write(
            folder / "frame.json",
            dict(
                graph=json.loads(placed.to_json()),
                step=request["iterations"],
                scope="Legalized placement; native copper unchecked",
            ),
        )
    except InterruptedError as exc:
        status.update(state="stopped", message=str(exc))
    except Exception as exc:
        import traceback

        traceback.print_exc()
        status.update(state="failed", message=str(exc))
    finally:
        trace.close()
        status["elapsed_seconds"] = time.monotonic() - start
        write(folder / "status.json", status)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
