"""Lane -> schematic scope, and a cached asynchronous schematic builder.

A lane's scope is the set of parts on its board: the refs of its latest
geometry, else the trial run directory's keep.json, else the whole board.
Payloads are content addressed: the key hashes every input (netlist, rules,
constraints, part libraries, atopile source, builder and runtime code) plus
the scope's refs, so every trial of one block template shares one payload and
one browser layout. Lane-specific data (trial run dir, routed hot-loop quality)
is returned separately as a small overlay.
"""

import hashlib
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from yapnr.viewer import runtime as viewer_runtime

HERE = Path(__file__).parent
BUILDER = "yapnr.viewer.services.schematic_build"

NAME = re.compile(r"[A-Za-z0-9_.:+@-]{1,200}")
RUNTIME_FILES = (
    "pnr/power_topology.py",
    "pnr/hier/blocks.py",
    "pnr/constraints.py",
    "pnr/graph.py",
    "pnr/electrical.py",
)


class SchematicService:
    def __init__(
        self,
        root,
        *,
        graph=None,
        rules=None,
        constraints=None,
        parts=None,
        ato_src=None,
        runtime=None,
        experiment=None,
        cache_dir=None,
    ):
        """experiment: trial run directories must lie under it (default: the root's parent)."""
        self.root = Path(root)
        self.graph = Path(graph) if graph else None
        self.rules = (
            Path(rules) if rules else (self.graph.parent / "rules.json" if self.graph else None)
        )
        self.constraints = Path(constraints) if constraints else None
        self.parts = Path(parts) if parts else None
        self.ato_src = Path(ato_src) if ato_src else (self.parts.parent if self.parts else None)
        self.runtime = Path(runtime) if runtime else None
        self.experiment = Path(experiment or self.root.parent).resolve()
        self.cache = Path(cache_dir or self.root / "schematic")
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.RLock()
        self.pending, self.failed, self.memo = {}, {}, {}
        self._fp = (0.0, None)
        self._graph_refs = (None, None)
        self._pq = {}

    # ------------------------------------------------------------------ configuration
    def configured(self):
        return bool(self.graph and self.graph.is_file())

    def describe(self):
        return dict(
            graph=str(self.graph) if self.graph else None,
            rules=str(self.rules) if self.rules else None,
            constraints=str(self.constraints) if self.constraints else None,
            parts=str(self.parts) if self.parts else None,
            runtime=str(self.runtime) if self.runtime else None,
            cache=str(self.cache),
            configured=self.configured(),
        )

    @staticmethod
    def _sha(path):
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None

    def fingerprint(self):
        """Hash of every build input; re-stat at most every 5 s."""
        now = time.monotonic()
        if self._fp[1] is not None and now - self._fp[0] < 5:
            return self._fp[1]
        stats = []
        for base, pats in ((self.parts, ("*/*.kicad_sym", "*/*.ato")), (self.ato_src, ("*.ato",))):
            if base and base.is_dir():
                for pat in pats:
                    for f in sorted(base.glob(pat)):
                        st = f.stat()
                        stats.append((str(f.relative_to(base)), st.st_size, st.st_mtime_ns))
        fp = dict(
            graph=self._sha(self.graph),
            rules=self._sha(self.rules) if self.rules else None,
            constraints=self._sha(self.constraints) if self.constraints else None,
            libraries=hashlib.sha256(json.dumps(stats).encode()).hexdigest(),
            builder={n: self._sha(HERE / n) for n in ("schematic_build.py", "schematic_sym.py")},
            runtime=(
                {n: self._sha(self.runtime / n) for n in RUNTIME_FILES} if self.runtime else None
            ),
        )
        self._fp = (now, fp)
        return fp

    def graph_refs(self):
        st = self.graph.stat().st_mtime_ns
        if self._graph_refs[0] != st:
            g = json.loads(self.graph.read_text())
            self._graph_refs = (st, {c["ref"] for c in g["components"]})
        return self._graph_refs[1]

    # ------------------------------------------------------------------ lane resolution
    def run_dir(self, lane_id, source):
        """The trial/candidate run directory: nearest ancestor of the event source
        holding placed.json, else blocks/<run>/*/native/<tag>/ for block lanes."""
        if isinstance(source, str) and source:
            p = Path(source)
            if not p.is_absolute():
                p = self.experiment / p
            try:
                p = p.resolve()
            except OSError:
                p = None
            while p is not None and p.is_relative_to(self.experiment) and p != self.experiment:
                if (p / "placed.json").is_file():
                    return p
                p = p.parent
        run, _, tag = lane_id.partition("/")
        if (
            not tag
            or not NAME.fullmatch(run)
            or not NAME.fullmatch(tag)
            or "/" in tag
            or {run, tag} & {".", ".."}
        ):
            return None
        pattern = ("*" if run == "blocks" else run) + "/*/native/" + tag
        hits = [
            d for d in (self.experiment / "blocks").glob(pattern) if (d / "keep.json").is_file()
        ]
        hits = [d for d in hits if d.resolve().is_relative_to(self.experiment)]
        return max(hits, key=lambda d: d.stat().st_mtime) if hits else None

    def overlay(self, lane_id, run_dir):
        out = dict(lane=lane_id, run_dir=None, template=None, tag=lane_id.partition("/")[2] or None)
        if run_dir is None:
            return out
        out["run_dir"] = str(run_dir.relative_to(self.experiment))
        lib = run_dir.parent.parent / "library.json"
        if (run_dir / "keep.json").is_file() and lib.is_file():
            try:
                doc = json.loads(lib.read_text())
                out["template"] = doc.get("template_id") or run_dir.parent.parent.name
                out["library_blocks"] = doc.get("blocks")
            except (OSError, ValueError):
                pass
        pq = run_dir / "power-quality.json"
        if pq.is_file():
            st = pq.stat().st_mtime_ns
            cached = self._pq.get(pq)
            if not cached or cached[0] != st:
                try:
                    doc = json.loads(pq.read_text())
                    loops = [
                        dict(
                            labels=l.get("labels"),
                            nets=l.get("nets"),
                            parts=l.get("parts"),
                            routed_mm=l.get("routed_mm"),
                            vias=l.get("vias"),
                            open_links=l.get("open_links"),
                            complete=l.get("complete"),
                            links=[
                                dict(
                                    net=k.get("net"),
                                    length_mm=k.get("length_mm"),
                                    straight_mm=k.get("straight_mm"),
                                    vias=k.get("vias"),
                                    open=k.get("open"),
                                    a=k.get("a"),
                                    b=k.get("b"),
                                )
                                for k in l.get("links", [])
                            ],
                        )
                        for l in doc.get("loops", [])
                    ]
                    cached = (
                        st,
                        dict(
                            valid=doc.get("valid"),
                            hot_loops_open=doc.get("hot_loops_open"),
                            loops=loops,
                        ),
                    )
                except (OSError, ValueError):
                    cached = (st, None)
                if len(self._pq) > 256:
                    self._pq.clear()
                self._pq[pq] = cached
            out["power_quality"] = cached[1]
        return out

    def scope_refs(self, lane_id, lane_refs, run_dir):
        refs = sorted(set(lane_refs)) if lane_refs else None
        if refs is None and run_dir is not None and (run_dir / "keep.json").is_file():
            try:
                keep = json.loads((run_dir / "keep.json").read_text())
                refs = sorted({r for r in keep if isinstance(r, str)}) or None
            except (OSError, ValueError):
                refs = None
        if refs is not None:
            board = self.graph_refs()
            if len(set(refs) & board) >= 0.95 * len(board):
                refs = None
        return refs

    # ------------------------------------------------------------------ requests
    def request(self, lane_id, lane_refs, source, scope="auto"):
        if not self.configured():
            return dict(
                status="unavailable", reason="No netlist configured for the schematic (--graph)."
            )
        run_dir = self.run_dir(lane_id, source)
        refs = self.scope_refs(lane_id, lane_refs, run_dir)
        overlay = self.overlay(lane_id, run_dir)
        overlay["lane_refs"] = refs
        if scope == "board":
            refs = None
        identity = dict(inputs=self.fingerprint(), refs=refs, schema="pnr-schematic-v1")
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        out = dict(key=key, scope="board" if refs is None else "block", overlay=overlay)
        path = self.cache / (key + ".json")
        with self.lock:
            if key in self.failed:
                return dict(out, status="error", error=self.failed[key])
            if key in self.memo or path.is_file():
                return dict(out, status="ready", url="/api/schematic/payload/" + key)
            job = self.pending.get(key)
            if job is None or job.done():
                if sum(not f.done() for f in self.pending.values()) >= 4:
                    return dict(
                        out, status="busy", reason="Earlier schematic builds are still running."
                    )
                self.pending[key] = self.pool.submit(self.build, key, refs)
        return dict(out, status="pending")

    def build(self, key, refs):
        self.cache.mkdir(parents=True, exist_ok=True)
        req = self.cache / (key + ".request.json")
        out = self.cache / (key + ".json")
        tmp = self.cache / (key + ".partial.json")
        req.write_text(
            json.dumps(
                dict(
                    graph=str(self.graph),
                    rules=str(self.rules) if self.rules and self.rules.is_file() else None,
                    constraints=(
                        str(self.constraints)
                        if self.constraints and self.constraints.is_file()
                        else None
                    ),
                    parts=str(self.parts) if self.parts else None,
                    ato_src=str(self.ato_src) if self.ato_src else None,
                    symbol_cache=str(self.cache / "symbols"),
                    refs=refs,
                ),
                indent=1,
            )
        )
        env = viewer_runtime.hermetic_env(self.runtime)
        try:
            with (self.cache / (key + ".log")).open("w") as log:
                subprocess.run(
                    viewer_runtime.module_command(BUILDER, str(req), str(tmp)),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=90,
                    check=False,
                )
            raw = tmp.read_bytes()
            doc = json.loads(raw)
        except Exception as ex:
            with self.lock:
                self.failed[key] = "schematic build failed: %s" % ex
            return
        finally:
            req.unlink(missing_ok=True)
        if "error" in doc:
            tmp.unlink(missing_ok=True)
            with self.lock:
                self.failed[key] = doc["error"]
            return
        tmp.replace(out)
        with self.lock:
            self.memo[key] = raw
            while len(self.memo) > 8:
                self.memo.pop(next(iter(self.memo)))

    def payload(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key or ""):
            raise ValueError("invalid schematic key")
        with self.lock:
            if key in self.memo:
                return self.memo[key]
        path = self.cache / (key + ".json")
        if not path.is_file():
            raise FileNotFoundError("schematic not built")
        raw = path.read_bytes()
        with self.lock:
            self.memo[key] = raw
            while len(self.memo) > 8:
                self.memo.pop(next(iter(self.memo)))
        return raw
