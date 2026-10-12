"""Bounded, asynchronous, hash-bound surface jobs, separate from the optimizer."""

import hashlib
import json
import math
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from yapnr import experiments
from yapnr.viewer import runtime

from .placement_lab import digest, write
from .placement_surface_expression import expression


class Surfaces:
    def __init__(self, lab):
        self.lab = lab
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = {}
        self.lock = threading.Lock()

    def result(self, ident, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid surface key")
        folder = self.lab.folder(ident) / "surfaces" / key
        if (folder / "result.json").exists():
            return dict(
                status="ready",
                key=key,
                report=json.loads((folder / "result.json").read_text()),
                sha256=digest(folder / "result.json"),
            )
        future = self.pending.get((ident, key))
        if future and future.done() and future.exception():
            return dict(status="error", key=key, error=str(future.exception()))
        if future:
            return dict(status="pending", key=key)
        raise ValueError("Unknown surface; request it again after a server restart")

    def request(self, b):
        expression(b["expression"])
        pitch = b.get("pitch", 1.5)
        if type(pitch) not in (int, float) or not math.isfinite(pitch) or not 0.5 <= pitch <= 5:
            raise ValueError("Surface pitch must be 0.5–5 mm")
        folder = self.lab.folder(b["id"])
        frame_raw = (folder / "frame.json").read_bytes()
        frame = json.loads(frame_raw)
        if b["ref"] not in [c["ref"] for c in frame["graph"]["components"]]:
            raise ValueError("Unknown component")
        request = json.loads((folder / "request.json").read_text())
        if not (folder / "engine").is_dir():
            raise ValueError(
                "This archived run has no frozen engine; start a lab run from its source"
            )
        source_raw = (folder / "input.json").read_bytes()
        if hashlib.sha256(source_raw).hexdigest() != request["source_sha256"]:
            raise ValueError("Frozen source changed")
        status = json.loads((folder / "status.json").read_text())
        payload = dict(
            frame=frame,
            source=json.loads(source_raw),
            frame_sha256=hashlib.sha256(frame_raw).hexdigest(),
            engine_sources=request["engine_sources"],
            ref=b["ref"],
            pitch=pitch,
            expression=b["expression"],
            objective_expression=status.get("expression", request["expression"]),
            evaluator_sha256=digest(Path(__file__).with_name("placement_surface_worker.py")),
            expression_sha256=digest(Path(__file__).with_name("placement_surface_expression.py")),
        )
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        jobdir = folder / "surfaces" / key
        with self.lock:
            if (jobdir / "result.json").exists() or (b["id"], key) in self.pending:
                return self.result(b["id"], key)
            if sum(not f.done() for f in self.pending.values()) >= 2:
                raise ValueError("Surface worker busy; wait for the previous field")
            write(jobdir / "request.json", payload)
            evaluator_inputs = []
            for name in ("placement_surface_worker.py", "placement_surface_expression.py"):
                path = jobdir / name
                path.write_bytes(Path(__file__).with_name(name).read_bytes())
                evaluator_inputs.append(path)
            parent = json.loads((folder / "experiment.json").read_text())["id"]
            attempt = experiments.begin(
                self.lab.project,
                "discovery",
                "Placement cost surface: " + b["ref"],
                inputs=[jobdir / "request.json", *evaluator_inputs],
                parent=parent,
                parameters=dict(
                    expression=b["expression"], pitch=pitch, frame_sha256=payload["frame_sha256"]
                ),
            )
            write(jobdir / "experiment.json", dict(id=attempt["id"]))
            self.pending[b["id"], key] = self.pool.submit(
                self.recorded_compute, folder / "engine", jobdir, attempt["id"]
            )
        return dict(status="pending", key=key)

    def recorded_compute(self, engine, folder, attempt):
        try:
            self.compute(engine, folder)
        except Exception as exc:
            write(folder / "error.json", dict(error=str(exc)))
            experiments.finish(self.lab.project, attempt, "failed", [folder / "error.json"])
            raise
        experiments.finish(
            self.lab.project,
            attempt,
            "passed",
            [folder / "result.json"],
            summary="Read-only sampled cost surface; not placement or electrical validation",
        )

    @staticmethod
    def compute(engine, folder):
        env = runtime.hermetic_env(engine)
        env = {k: v for k, v in env.items() if not k.startswith("PNR_")}
        env["PNR_COST_RUNTIME"] = str(engine)
        with (folder / "worker.log").open("w") as log:
            result = subprocess.run(
                runtime.module_command(
                    "yapnr.viewer.services.placement_surface_worker", str(folder)
                ),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
        if result.returncode:
            lines = (folder / "worker.log").read_text().splitlines()
            raise ValueError(lines[-1] if lines else "Surface evaluation failed")
