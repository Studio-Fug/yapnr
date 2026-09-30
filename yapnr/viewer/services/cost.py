"""Hash-bound asynchronous read-only component cost diagnostics."""

import hashlib
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class CostService:
    def __init__(self, root, repo, assets, *, runtime=None, contexts=None):
        self.root = root
        self.repo = repo
        self.assets = assets
        self.runtime = Path(runtime or repo / "hardware/pnr").resolve()
        self.contexts = json.loads(Path(contexts).read_text()) if contexts else {}
        self.cache = root / "component-costs"
        self.cache.mkdir(exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = {}
        self.lock = threading.RLock()
        self.events = {}
        self.model_hash = hashlib.sha256(
            (self.runtime / "pnr/place/cost_inspect.py").read_bytes()
        ).hexdigest()

    def request(self, event_id, ref=None):
        if not re.fullmatch(r"[0-9]+-[a-f0-9]+", event_id or ""):
            raise ValueError("Invalid event id")
        if ref is not None and (not isinstance(ref, str) or len(ref) > 80):
            raise ValueError("Invalid reference")
        event_path = self.root / "events" / (event_id + ".json")
        event = json.loads(event_path.read_text())
        if event.get("kind") == "placement_cost_capture":
            return self.recorded(event, ref)
        if "board_sha256" not in event:
            return dict(
                status="unavailable",
                reason="This draft has no immutable native board checkpoint yet.",
            )
        sha = event["board_sha256"]
        board = Path(event["board"])
        if not board.resolve().is_relative_to((self.root / "boards").resolve()) or not re.fullmatch(
            "[a-f0-9]{64}", sha
        ):
            raise ValueError("Invalid board binding")
        if (
            hashlib.sha256((self.runtime / "pnr/place/cost_inspect.py").read_bytes()).hexdigest()
            != self.model_hash
        ):
            raise ValueError("Cost model changed; restart viewer to preserve cache provenance")
        context = self.contexts.get(event["candidate"], self.contexts.get("*"))
        if context is None:
            return dict(
                status="unavailable",
                reason="No placement objective was recorded for this routed checkpoint. Select its recorded global or legalization phase.",
            )
        identity = dict(board_sha256=sha, context=context, model_sha256=self.model_hash, ref=ref)
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        result = self.cache / (key + ".json")
        with self.lock:
            if result.exists():
                report = json.loads(result.read_text())
                if report.get("model_sha256") != self.model_hash:
                    raise ValueError("Cached model hash mismatch")
                report["event_id"] = event_id
                return dict(status="ready", report=report)
            job = self.pending.get(key)
            if job and job.done():
                ex = job.exception()
                if ex:
                    return dict(status="error", error=str(ex))
            if not job:
                if sum(not f.done() for f in self.pending.values()) >= 4:
                    return dict(
                        status="busy",
                        reason="Waiting for earlier cost field; routing keeps priority.",
                    )
                self.pending[key] = self.pool.submit(self.compute, event, context, ref, key)
        return dict(status="pending", key=key)

    def compute(self, event, context, ref, key):
        sha = event["board_sha256"]
        board = Path(event["board"])
        assert hashlib.sha256(board.read_bytes()).hexdigest() == sha, "Native board hash mismatch"
        graph = self.cache / (sha + ".graph.json")
        ki = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
        py = Path(__import__("sys").executable)
        env = dict(
            os.environ,
            PYTHONPATH=str(self.runtime),
            PNR_COST_RUNTIME=str(self.runtime),
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
        )
        env.pop("PNR_LIVE_DIR", None)
        env.pop("PNR_PROFILE_DIR", None)
        with (self.cache / (key + ".log")).open("w") as log:
            if not graph.exists():
                subprocess.run(
                    [ki, str(self.assets / "cost_extract.py"), str(board), str(graph), sha],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=40,
                )
            request = self.cache / (key + ".request.json")
            request.write_text(
                json.dumps(
                    dict(
                        context=context,
                        graph=str(graph),
                        board_sha256=sha,
                        event_id=event["id"],
                        ref=ref,
                    )
                )
            )
            subprocess.run(
                [
                    str(py),
                    str(self.assets / "cost_compute.py"),
                    str(request),
                    str(self.cache / (key + ".json")),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=60,
            )

    def recorded(self, event, ref):
        if not isinstance(ref, str) or not ref:
            return dict(
                status="unavailable", reason="Select a component to inspect its placement cost."
            )
        if (
            hashlib.sha256((self.runtime / "pnr/place/cost_inspect.py").read_bytes()).hexdigest()
            != self.model_hash
        ):
            raise ValueError("Cost model changed; restart viewer")
        capture = event["data"]["cost_capture"]
        path = Path(capture["path"]).resolve()
        if not path.is_relative_to((self.repo / "output").resolve()):
            raise ValueError("Capture outside experiment outputs")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != capture["sha256"]:
            raise ValueError("Capture hash mismatch")
        document = json.loads(raw)
        if document.get("kind") == "routing-probe" and ref not in document.get(
            "components", {document["component"]["ref"]: document}
        ):
            return dict(
                status="unavailable",
                reason="This component was not evaluated in the selected routing-probe phase.",
            )
        for name, sha in document.get("probe_sources", {}).items():
            if name not in ("relocate.py", "batch_relocate.py"):
                raise ValueError("Invalid routing-probe source binding")
            if hashlib.sha256((self.runtime / "pnr/place" / name).read_bytes()).hexdigest() != sha:
                raise ValueError("Routing-probe runtime changed; refuse stale cost cache")
        sha = hashlib.sha256(
            json.dumps(event["layout"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        identity = dict(
            capture=capture,
            layout_sha256=sha,
            ref=ref,
            compute_sha=hashlib.sha256(
                (self.assets / "cost_compute_capture.py").read_bytes()
            ).hexdigest(),
            model=self.model_hash,
        )
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        result = self.cache / (key + ".json")
        with self.lock:
            if result.exists():
                report = json.loads(result.read_text())
                report["event_id"] = event["id"]
                return dict(status="ready", report=report)
            job = self.pending.get(key)
            if job and job.done() and job.exception():
                return dict(status="error", error=str(job.exception()))
            if not job:
                if sum(not f.done() for f in self.pending.values()) >= 4:
                    return dict(status="busy", reason="Waiting for earlier component cost field")
                self.pending[key] = self.pool.submit(self.compute_recorded, event, ref, key, sha)
        return dict(status="pending", key=key)

    def compute_recorded(self, event, ref, key, sha):
        request = self.cache / (key + ".request.json")
        request.write_text(
            json.dumps(
                dict(
                    capture=event["data"]["cost_capture"],
                    ref=ref,
                    event_id=event["id"],
                    layout_sha256=sha,
                )
            )
        )
        env = dict(
            os.environ,
            PNR_COST_RUNTIME=str(self.runtime),
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
        )
        env.pop("PNR_LIVE_DIR", None)
        env.pop("PNR_PROFILE_DIR", None)
        with (self.cache / (key + ".log")).open("w") as log:
            subprocess.run(
                [
                    __import__("sys").executable,
                    str(self.assets / "cost_compute_capture.py"),
                    str(request),
                    str(self.cache / (key + ".json")),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=60,
            )
