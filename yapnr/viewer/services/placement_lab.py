"""Local, bounded placement experiments and immutable legalization decision replay."""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from yapnr import experiments
from yapnr.viewer import runtime

from .placement_annealing import parse as parse_annealing
from .placement_objective import DEFAULT, TERMS, parse


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    tmp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(parent, item):
    p = Path(item["path"]).resolve()
    if not p.is_relative_to(parent.resolve()) or digest(p) != item["sha256"]:
        raise ValueError("Capture path or hash mismatch")
    return json.loads(p.read_text())


def decision(parent, item):
    """Actual candidates only. The closest evaluated site is a comparison, not a new solve."""
    import numpy as np

    d = checked(parent, item)
    candidates = []
    weights = {t["key"]: t["weight"] for t in d["terms"]}
    for field in d["candidate_fields"]:
        p = Path(field["path"]).resolve()
        if not p.is_relative_to(parent.resolve()) or digest(p) != field["sha256"]:
            raise ValueError("Candidate field hash mismatch")
        with np.load(p, allow_pickle=False) as archive:
            rows = archive["values"]
        columns = field["columns"]
        # Keep nearest and least-cost examples from EVERY explored orientation.
        total = rows[:, 2] + weights["channel_shortage"] * rows[:, 3] + rows[:, 4]
        for key in columns[5:]:
            total = total + rows[:, columns.index(key)] * (
                weights.get("wirelength", 0) if key == "wire_raw" else 1
            )
        indices = set(np.argsort(total, kind="stable")[:12].tolist())
        indices.update(np.argsort(rows[:, 2], kind="stable")[:4].tolist())
        for i in sorted(indices):
            terms = {
                "displacement": float(rows[i, 2]),
                "channels": float(rows[i, 3] * weights["channel_shortage"]),
                "local_loop": float(rows[i, 4]),
            }
            for key in columns[5:]:
                terms[key] = float(
                    rows[i, columns.index(key)]
                    * (weights.get("wirelength", 0) if key == "wire_raw" else 1)
                )
            candidates.append(
                dict(
                    position=rows[i, :2].tolist(),
                    rotation=field["rotation"],
                    total=float(total[i]),
                    terms=terms,
                )
            )
    d.pop("candidate_fields", None)
    d["decision_sha256"] = item["sha256"]
    d["alternatives"] = sorted(candidates, key=lambda c: (c["total"], c["rotation"], c["position"]))
    d["nearest"] = min(candidates, key=lambda c: c["terms"]["displacement"], default=None)
    return d


class PlacementLab:
    def __init__(self, root, engine, capture_root=None):
        self.root = Path(root).resolve() / "placement-lab"
        self.project = experiments.workspace_root(root)
        self.engine = Path(engine) if engine else None
        self.capture_root = Path(capture_root) if capture_root else Path(root).parent
        self.seen_events = set()
        self.lock = threading.RLock()
        self.process = None
        self.active = None
        from .placement_surfaces import Surfaces

        self.surfaces = Surfaces(self)

    def folder(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
            raise ValueError("Invalid run identity")
        return self.root / "runs" / ident

    def catalog(self):
        # Recorded global phases are reusable inputs without selecting filesystem paths
        # from a browser. Hash and root checks are identical to cost replay's boundary.
        for event_path in sorted((self.root.parent / "events").glob("*.json")):
            if event_path.name in self.seen_events:
                continue
            event = json.loads(event_path.read_text())
            capture = event.get("data", {}).get("cost_capture", {})
            if capture.get("kind") == "global-objective":
                doc = checked(self.capture_root, capture)
                ident = "global-" + capture["sha256"][:20]
                write(
                    self.root / "sources" / (ident + ".json"),
                    dict(
                        title=event.get("candidate", "Recorded global placement"),
                        graph=doc["graph"],
                        constraints=doc["constraints"],
                        capture_sha256=capture["sha256"],
                        rules=None,
                    ),
                )
            self.seen_events.add(event_path.name)
        sources = []
        for p in sorted((self.root / "sources").glob("*.json")):
            d = json.loads(p.read_text())
            sources.append(dict(id=p.stem, title=d.get("title", p.stem), sha256=digest(p)))
        runs = []
        for p in sorted((self.root / "runs").glob("*/status.json"), reverse=True):
            d = json.loads(p.read_text())
            if (
                d.get("state") in ("running", "paused", "legalizing")
                and p.parent.name != self.active
            ):
                d.update(
                    state="interrupted",
                    message="Worker no longer owned by this server; saved evidence retained",
                )
            runs.append(
                dict(
                    id=p.parent.name,
                    **{k: v for k, v in d.items() if k not in ("locality", "violations")},
                )
            )
        return dict(sources=sources, runs=runs, default_expression=DEFAULT, terms=list(TERMS))

    def get(self, ident, step=None):
        folder = self.folder(ident)
        status = json.loads((folder / "status.json").read_text())
        if status.get("state") in ("running", "paused", "legalizing") and ident != self.active:
            status.update(state="interrupted", message="Worker no longer owned by this server")
        out = dict(
            id=ident, status=status, request=json.loads((folder / "request.json").read_text())
        )
        if (folder / "control.json").exists():
            out["control"] = json.loads((folder / "control.json").read_text())
        if (folder / "frame.json").exists():
            out["frame"] = json.loads((folder / "frame.json").read_text())
        if (folder / "review.json").exists():
            review = json.loads((folder / "review.json").read_text())
            out["review"] = review
            if step is not None:
                index = int(step)
                if not 0 <= index < len(review["decisions"]):
                    raise ValueError("Decision out of range")
                out["decision"] = decision(folder, review["decisions"][index])
        from .placement_airwires import spans

        graph = (
            out.get("decision", {}).get("graph")
            or out.get("frame", {}).get("graph")
            or out.get("review", {}).get("graph")
        )
        if graph:
            out["airwires"] = spans(graph, out.get("decision"))
        return out

    def command(self, body):
        with self.lock:
            action = body.get("action")
            if action == "start":
                if self.process is not None and self.process.poll() is None:
                    raise ValueError(
                        "One local placement run at a time; pause or stop the active run"
                    )
                weights = parse(body.get("expression", DEFAULT))
                source = body.get("source", "")
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", source):
                    raise ValueError("Choose a registered source")
                source_path = self.root / "sources" / (source + ".json")
                doc = json.loads(source_path.read_text())
                seed, iterations = body.get("seed", 0), body.get("iterations", 400)
                if (
                    type(seed) is not int
                    or not 0 <= seed < 2**31
                    or type(iterations) is not int
                    or not 1 <= iterations <= 5000
                ):
                    raise ValueError("Seed must be 0..2³¹−1; iterations 1..5000")
                annealing = parse_annealing(body.get("annealing"), steps=iterations)
                delay = body.get("frame_delay_ms", 30)
                if type(delay) is not int or not 0 <= delay <= 200:
                    raise ValueError("Display pacing must be 0..200 ms")
                if type(body.get("run_forever", False)) is not bool:
                    raise ValueError("run_forever must be a boolean")
                if type(body.get("paused", False)) is not bool:
                    raise ValueError("paused must be a boolean")
                if self.engine is None:
                    raise ValueError("Placement engine runtime is not configured")
                ident = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                folder = self.folder(ident)
                request = dict(
                    source=source,
                    source_sha256=digest(source_path),
                    expression=body.get("expression", DEFAULT),
                    weights=weights,
                    seed=seed,
                    iterations=iterations,
                    frame_delay_ms=delay,
                    annealing=annealing,
                    engine_sources={
                        str(p.relative_to(self.engine)): digest(p)
                        for p in self.engine.rglob("*.py")
                    },
                    engine_mode="single-start global placement; authored constraints retained",
                    run_forever=body.get("run_forever", False),
                    timeout_seconds=None if body.get("run_forever", False) else 600,
                )
                if len(doc.get("graph", {}).get("components", [])) > 500:
                    raise ValueError("Local lab supports at most 500 components per experiment")
                # Freeze the actual implementation consumed by this worker.
                for name, sha in request["engine_sources"].items():
                    src = self.engine / name
                    if digest(src) != sha:
                        raise ValueError("Engine changed while snapshotting")
                    dest = folder / "engine" / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
                write(folder / "input.json", doc)
                write(folder / "request.json", request)
                write(
                    folder / "control.json",
                    dict(
                        expression=request["expression"],
                        annealing=annealing,
                        paused=body.get("paused", False),
                        stop=False,
                        revision=0,
                    ),
                )
                write(
                    folder / "status.json",
                    dict(
                        state="running",
                        title=doc.get("title", source),
                        step=0,
                        iterations=iterations,
                        run_forever=request["run_forever"],
                        seed=seed,
                    ),
                )
                attempt = experiments.begin(
                    self.project,
                    "pnr",
                    "Placement lab: " + doc.get("title", source),
                    inputs=[folder / "input.json", folder / "request.json"],
                    parameters=request,
                )
                write(folder / "experiment.json", dict(id=attempt["id"]))
                env = runtime.hermetic_env(self.engine)
                # No ambient campaign flags or cloud execution in this local lab.
                env = {k: v for k, v in env.items() if not k.startswith("PNR_")}
                env["PNR_COST_RUNTIME"] = str(folder / "engine")
                env["PYTHONHASHSEED"] = "0"
                env["YAPNR_PLACEMENT_PARENT_PID"] = str(os.getpid())
                log = (folder / "worker.log").open("w")
                self.process = subprocess.Popen(
                    runtime.module_command("yapnr.viewer.services.placement_worker", str(folder)),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                log.close()
                self.active = ident
                threading.Thread(
                    target=self._wait, args=(self.process, folder), daemon=True
                ).start()
                return dict(id=ident)
            ident = body.get("id")
            folder = self.folder(ident)
            if ident != self.active or self.process is None or self.process.poll() is not None:
                raise ValueError("This run is no longer active; start a reproducible new run")
            control = json.loads((folder / "control.json").read_text())
            if "expect_revision" in body and (
                type(body["expect_revision"]) is not int
                or body["expect_revision"] != control["revision"]
            ):
                raise ValueError("Stale control revision; read status before changing this run")
            if action == "apply":
                parse(body.get("expression"))
                control["expression"] = body["expression"]
            elif action == "annealing":
                status = json.loads((folder / "status.json").read_text())
                if status.get("state") not in ("running", "paused"):
                    raise ValueError("Rotation controls require an optimizing or paused run")
                control["annealing"] = parse_annealing(
                    body.get("annealing"), start_step=status["step"]
                )
            elif action in ("pause", "resume"):
                control["paused"] = action == "pause"
                control.pop("step_until", None)
            elif action == "step":
                status = json.loads((folder / "status.json").read_text())
                count = body.get("count", 1)
                if type(count) is not int or not 1 <= count <= 100:
                    raise ValueError("Step count must be an integer from 1 to 100")
                if (
                    status.get("state") != "paused"
                    or status.get("control_revision") != control["revision"]
                ):
                    raise ValueError(
                        "Wait for the worker to acknowledge a paused state before stepping"
                    )
                request = json.loads((folder / "request.json").read_text())
                limit = request["iterations"]
                if not request.get("run_forever", False) and status["step"] + count > limit:
                    raise ValueError(
                        "Step exceeds remaining optimizer iterations; resume to legalize"
                    )
                control.update(paused=False, step_until=status["step"] + count)
            elif action == "stop":
                control["stop"] = True
            else:
                raise ValueError("Unknown placement action")
            control["revision"] += 1
            write(folder / "control.json", control)
            if action == "stop":
                self.process.terminate()
            return dict(id=ident, control=control)

    def _wait(self, process, folder):
        try:
            request = json.loads((folder / "request.json").read_text())
            code = process.wait(timeout=None if request.get("run_forever", False) else 600)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            state = json.loads((folder / "status.json").read_text())
            state.update(
                state="timed_out",
                message="600-second local experiment budget reached; saved frames retained",
            )
            write(folder / "status.json", state)
            self._finish_experiment(folder)
            return
        state = json.loads((folder / "status.json").read_text())
        if code and state.get("state") not in ("failed", "stopped"):
            stopped = json.loads((folder / "control.json").read_text()).get("stop")
            state.update(
                state="stopped" if stopped else "failed",
                message=(
                    "Stopped by user; saved frames retained"
                    if stopped
                    else f"Worker exited {code}; inspect retained worker.log"
                ),
            )
            write(folder / "status.json", state)

        self._finish_experiment(folder)

    def _finish_experiment(self, folder):
        identity = folder / "experiment.json"
        if not identity.exists():
            return
        state = json.loads((folder / "status.json").read_text())
        terminal = (
            "passed"
            if state.get("state") == "complete" and state.get("legal")
            else (
                "timed out"
                if state.get("state") == "timed_out"
                else "cancelled" if state.get("state") == "stopped" else "failed"
            )
        )
        outputs = [
            folder / name
            for name in (
                "status.json",
                "runtime.json",
                "iterations.jsonl",
                "initial.json",
                "continuous.json",
                "placed.json",
                "review.json",
            )
            if (folder / name).exists()
        ]
        try:
            experiments.finish(
                self.project,
                json.loads(identity.read_text())["id"],
                terminal,
                outputs,
                summary="Local placement experiment only; native routing and electrical acceptance remain unverified",
            )
        except (ValueError, OSError) as exc:
            state["registry_error"] = str(exc)
            write(folder / "status.json", state)

    def close(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
