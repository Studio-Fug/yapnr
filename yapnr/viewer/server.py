"""The live viewer's HTTP server: explicit-interface PnR telemetry, immutable pins and snapshots.

A ``Viewer`` reads one live telemetry directory (the *root*: ``events/``, ``geometry/``,
``boards/``, ``control.json``) that a running experiment writes, replays its events into lanes and
serves them with the static front end. Optional services hang off it: component-cost replay,
the schematic view, the atopile source browser, design notes, the Ask agent (off by default,
paid), AI net labels (off by default, paid) and the 3D view.

Run it with ``bazel run //:viewer -- --root <live dir>`` or ``python -m yapnr.viewer``; the flags
and the config file are described in :mod:`yapnr.viewer.config`.
"""

from __future__ import annotations

import copy
import functools
import gzip
import hashlib
import json
import math
import os
import posixpath
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import yapnr
from yapnr.viewer import config as viewer_config
from yapnr.viewer import lane_tree, runtime
from yapnr.viewer.event_schema import phase_frame
from yapnr.viewer.progress import classify_lane
from yapnr.viewer.settings import save as save_settings
from yapnr.viewer.settings import seed as seed_settings
from yapnr.viewer.toolchain import Toolchain

PACKAGE = Path(__file__).parent
EXTRACT_SCRIPT = PACKAGE / "kicad_scripts" / "extract.py"
EXTRACT_TIMEOUT = 40
LICENSE = "AGPL-3.0-or-later"
# Written by ``yapnr exp live`` (yapnr.exp.live.FINISHED_MARKER) directly under the live root
# once every task the mirrored campaign actually submitted has a ``_DONE`` marker (not
# necessarily every task its plan names: a campaign that only submitted part of its plan is
# "finished" once that part is done -- yapnr.exp.live.required_task_ids); see
# Viewer._run_finished. Withdrawn by the same mirror if a later submission turns up more,
# unfinished work, so its mere existence is always safe to trust. Kept as a matching literal in
# both files rather than a cross-package import -- yapnr.viewer and yapnr.exp are independent
# Bazel targets.
MIRROR_FINISHED_MARKER = "campaign-finished.json"
# Third-party files served unmodified from the assembled dist; the browser may cache them.
PINNED_PREFIXES = ("elk.bundled.js", "vendor/", "third_party/")
CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
}
OBJECTIVE_KEYS = (
    "violations",
    "blocked",
    "reference",
    "subwidth",
    "unqualified_pairs",
    "unconnected",
)
# Event data too large for the event stream summary.
BULKY_EVENT_KEYS = (
    "tracks",
    "candidates",
    "alternatives",
    "probes",
    "electrical_audit",
    "pad_entry",
    "final",
)


class GeometryUnavailable(Exception):
    """Board geometry cannot be extracted here (no KiCad Python)."""


class Recent(dict):
    """Geometry cache holding the newest 48 boards.

    A full replay (thousands of boards) no longer keeps every one in memory. Lanes keep their own
    references; /api/geometry re-reads evicted files from disk.
    """

    def __setitem__(self, k, v):
        self.pop(k, None)
        super().__setitem__(k, v)
        while len(self) > 48:
            try:
                del self[next(iter(self))]
            except (KeyError, StopIteration, RuntimeError):
                break


def write_json_atomic(path, value):
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sweep_tmp(folder, age=600):
    """Remove temporary geometry files older than age s.

    They are left when a viewer was killed mid-extraction; extraction times out at 40 s.
    """
    now = time.time()
    for t in folder.glob("*.tmp"):
        try:
            if now - t.stat().st_mtime > age:
                t.unlink()
        except OSError:
            pass


def from_graph(g):
    """Viewer geometry (parts and pads only) from a placement layout (a graph.json document)."""
    parts = []
    for c in g["components"]:
        angle = math.radians(c["rot"])
        pads = []
        for p in c["pads"]:
            x, y = p["offset"]
            xy = [
                c["pos"][0] + x * math.cos(angle) - y * math.sin(angle),
                c["pos"][1] + x * math.sin(angle) + y * math.cos(angle),
            ]
            pads.append(
                dict(
                    number=p["name"],
                    net=p["net"],
                    xy=xy,
                    size=p["size"],
                    angle=c["rot"],
                    shape="rect",
                    layers=["F.Cu" if c["side"] == "top" else "B.Cu"],
                )
            )
        parts.append(dict(ref=c["ref"], xy=c["pos"], pads=pads))
    return dict(
        frame="mm-y-up",
        width=g["outline"]["width"],
        height=g["outline"]["height"],
        parts=parts,
        tracks=[],
        vias=[],
        zones=[],
    )


def find_dist(explicit: Optional[Path] = None):
    """(directory with the static files, missing third-party parts or None).

    The assembled dist (``//yapnr/viewer:dist``: the static files plus the pinned elkjs and
    three.js) is next to this package in Bazel runfiles; ``--dist`` names another copy (for example
    ``bazel-bin/yapnr/viewer/dist`` from a plain checkout). Without either, the bare static files
    are served and the schematic layout and the 3D view report that their libraries are missing.
    """
    candidates = [Path(explicit)] if explicit else [PACKAGE / "dist"]
    for d in candidates:
        if (d / "index.html").is_file():
            missing = [
                name
                for name in ("elk.bundled.js", "vendor/three/build/three.module.js")
                if not (d / name).is_file()
            ]
            return d, missing or None
    if explicit:
        raise viewer_config.ConfigError(f"--dist {explicit}: no index.html there")
    return PACKAGE / "static", ["elk.bundled.js", "vendor/three/build/three.module.js"]


def source_revision() -> Tuple[Optional[str], bool]:
    """(the source commit of this viewer, whether the checkout has uncommitted changes) for the
    Source link (AGPL section 13): the image's stamp, else git (tracked files only)."""
    stamped = os.environ.get("YAPNR_SOURCE_REVISION", "").strip()
    if stamped:
        return stamped, False

    def git(*args):
        here = Path(__file__).resolve().parent
        return subprocess.run(
            ["git", "-C", str(here), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        done = git("rev-parse", "HEAD")
        rev = done.stdout.strip()
        if done.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", rev):
            return None, False
        changed = git("status", "--porcelain", "--untracked-files=no")
        return rev, changed.returncode == 0 and bool(changed.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, False


def source_link(url: Optional[str], revision: Optional[str]) -> Optional[str]:
    """The browsable tree of ``revision`` for a GitHub repository URL, else the URL itself."""
    if not url:
        return None
    base = url.rstrip("/")
    if revision and re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", base):
        return f"{base}/tree/{revision}"
    return url


def static_path(dist: Path, url_path: str) -> Optional[Path]:
    """The file a URL path names inside dist, or None.

    Containment is checked lexically, never by resolving symlinks: in Bazel runfiles every file
    is a symlink into the source tree or the output tree.
    """
    rel = "index.html" if url_path in ("", "/") else url_path.lstrip("/")
    if "\\" in rel or "\0" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or posixpath.normpath(rel) != rel:
        return None
    f = dist.joinpath(*parts)
    return f if f.is_file() else None


class Viewer:
    """State and services of one viewer process (one live root)."""

    def __init__(self, cfg: viewer_config.ViewerConfig, log=None):
        self.cfg = cfg
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.root = Path(cfg.root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiment = Path(cfg.experiment).absolute()
        self.port = cfg.port
        self.lock = threading.RLock()
        self.state = dict(
            schema="pnr-live-state-v1",
            run=str(self.root.parent),
            revision=0,
            lanes={},
            events=[],
            search={},
            errors=[],
        )
        self.seen = set()
        self.cache = Recent()
        self.response_cache = {}
        self.gzip_cache = {}
        self.extracting = {}  # KiCad extractor -> its temporary output; removed on stop
        self.last_sweep = 0.0
        self.geometry_notice = None
        # Trial source paths stay out of /api/state; the schematic route alone reads them.
        self.lane_sources = {}
        # Native board paths / final objectives per lane, also outside /api/state: the Ask
        # context reads them.
        self.lane_meta = {}
        # First event time seen per lane. apply_event mirrors this onto the lane itself as
        # lane["started_at"] (so it IS visible in /api/state) because humanize() (progress.py)
        # is a pure function of the lane dict alone and has no access to server state; this dict
        # is the source of truth that survives across lane_started.setdefault calls regardless of
        # what apply_event later does to the lane dict's other fields.
        self.lane_started = {}
        self.dist, self.dist_missing = find_dist(cfg.dist)
        self.runtime = runtime.engine_runtime(cfg.engine_runtime)
        self.toolchain = Toolchain(
            cli=cfg.kicad_cli,
            python=cfg.kicad_python,
            machine_cli=cfg.machine_kicad_cli,
            machine_python=cfg.machine_kicad_python,
        )
        self.revision, self.revision_modified = source_revision()
        self._pins = functools.lru_cache(maxsize=16)(self._pin_summary)
        self._init_controls()
        self._init_cost()
        self._init_design()
        self._init_viewer3d()
        self._init_notes()
        self._init_agent()
        self._init_origins()

    # ------------------------------------------------------------------ services
    def _init_controls(self):
        from pnr.runtime_controls import LIMITS
        from pnr.runtime_controls import read as read_controls

        self.limits = LIMITS
        self.read_controls = read_controls
        self.preferences = Path(self.cfg.preferences)
        self.state["controls"] = seed_settings(self.root / "control.json", self.preferences)
        self.state["active_controls"] = None

    def _init_cost(self):
        from yapnr.viewer.services.cost import CostService

        self.cost_service = CostService(
            self.root,
            runtime=self.runtime,
            contexts=self.cfg.cost_contexts,
            capture_root=self.cfg.capture_root,
            toolchain=self.toolchain,
        )
        if self.cost_service.disabled:
            self.log("component costs: " + self.cost_service.disabled)

    def _init_design(self):
        from yapnr.viewer.services.schematic import SchematicService
        from yapnr.viewer.sources import open_sources

        cfg = self.cfg
        self.sources = open_sources(cfg)
        if self.sources.reason:
            self.log("source browser: " + self.sources.reason)
        self.source_service = self.sources.service
        parts = cfg.parts or self.sources.parts
        self.parts = Path(parts) if parts else None
        self.schematic_service = SchematicService(
            self.root,
            graph=cfg.graph,
            rules=cfg.rules,
            constraints=cfg.constraints,
            parts=self.parts,
            ato_src=self.sources.src,
            runtime=self.runtime,
            experiment=self.experiment,
            cache_dir=cfg.cache_dir("schematic"),
        )

    def _init_viewer3d(self):
        from yapnr.viewer.services.viewer3d import Viewer3DService

        self.viewer3d = None
        if not self.cfg.viewer3d:
            return
        cli, why = self.toolchain.cli()
        # One export at a time per viewer, and across the viewers of one experiment folder
        # (flock); requests never wait for it.
        self.viewer3d = Viewer3DService(
            self.cfg.cache_dir("viewer3d"),
            cli=cli,
            unavailable=why,
            parts=self.parts,
            timeout=self.cfg.viewer3d_timeout,
            max_bytes=int(self.cfg.viewer3d_max_mb * (1 << 20)),
            lock_path=self.experiment / "viewer3d-export.lock",
        )
        if self.viewer3d.disabled:
            self.log("3D view: " + self.viewer3d.disabled)
        if self.dist_missing and "vendor/three/build/three.module.js" in self.dist_missing:
            self.log("3D view: three.js is missing from the served files (use the Bazel dist)")

    def _init_notes(self):
        from yapnr.viewer.notes.store import NotesStore

        # Design notes: always on (also with the agent off); the Ask agent writes them only
        # through its per-turn MCP server (notes/mcp.py).
        self.notes_dir = Path(self.cfg.notes).absolute()
        resolver = self.source_service or (
            str(self.cfg.graph) if self.cfg.graph and Path(self.cfg.graph).is_file() else None
        )
        try:
            self.notes = NotesStore(self.notes_dir, resolver=resolver)
        except Exception as ex:  # the viewer still starts
            self.notes = None
            self.log(f"notes store unavailable ({self.notes_dir}): {type(ex).__name__}: {ex}")

    def _init_agent(self):
        from yapnr.viewer.agent.service import is_loopback
        from yapnr.viewer.agent.spend import SpendMeter

        cfg = self.cfg
        # One spend cap for every paid CLI call of this process: Ask turns and AI net labels.
        self.spend = SpendMeter(cfg.agent_total_usd or None)
        self.listen_hosts = list(cfg.listen)
        exposed = [h for h in self.listen_hosts if not is_loopback(h)]
        self.agent_service = None
        self.agent_off_reason = (
            "The assistant is off on this server (start it with --agent on; paid turns)."
        )
        if cfg.agent:
            from yapnr.viewer.agent.service import AgentService

            if exposed:
                self.log(
                    f"WARNING: --agent on with non-loopback listeners {exposed}: any client that"
                    " reaches them (and sends an allowlisted Origin header) can run paid"
                    " assistant turns"
                )
            # WebFetch deny rules, with this machine's addresses.
            own_names = [
                *self.listen_hosts,
                *cfg.allow_host,
                *(urlparse(o).hostname or "" for o in cfg.allow_origin),
            ]
            read_dirs = cfg.agent_read_dirs or [d for d in (self.sources.src, self.experiment) if d]
            self.agent_service = AgentService(
                cfg.agent_cwd,
                claude_bin=cfg.claude or "claude",
                default_model=cfg.agent_model,
                max_budget_usd=cfg.agent_budget_usd,
                source=self.source_service,
                state_fn=self.lane_brief,
                event_fn=self.event_brief,
                cache_dir=cfg.cache_dir("agent"),
                src_root=self.sources.src,
                entry=self.sources.entry_label,
                add_dirs=read_dirs,
                docs=cfg.agent_docs,
                description=cfg.agent_description,
                graph=cfg.graph,
                rules=cfg.rules,
                engine_runtime=self.runtime,
                meter=self.spend,
                notes=self.notes,
                workspace_dir=self.experiment,
                web=cfg.agent_web,
                viewer_port=lambda: self.port,
                local_names=own_names,
            )
        on = cfg.net_summaries and self.source_service is not None
        self.net_summary_status = dict(
            enabled=cfg.net_summaries,
            model=cfg.net_summary_model,
            budget_usd=cfg.net_summary_budget_usd,
            state="pending" if on else "off",
        )
        if not cfg.net_summaries:
            self.net_summary_status["reason"] = "off (start the viewer with --net-summaries on)"
        elif self.source_service is None:
            self.net_summary_status["reason"] = "no atopile source index to label"

    def _init_origins(self):
        cfg = self.cfg
        # DNS-rebinding guard: a page on attacker.example that re-resolves to this address still
        # sends Host: attacker.example.
        hosts = {"127.0.0.1", "localhost", "::1"}
        hosts |= {h.lower().strip("[]") for h in self.listen_hosts}
        hosts |= {h.lower() for h in cfg.allow_host}
        for o in cfg.allow_origin:
            h = (urlparse(o).hostname or "").lower()
            if h:
                hosts |= {h, h.split(".")[0]}  # also a short name (host for host.example.com)
        self.allowed_hosts = hosts

    # ------------------------------------------------------------------ origin checks
    def origins(self):
        return [f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"] + list(
            self.cfg.allow_origin
        )

    def host_allowed(self, value):
        if value is None:
            return True  # HTTP/1.0 clients; browsers always send Host
        v = value.strip().lower()
        if v.startswith("["):
            host = v[1 : v.find("]")]
        elif v.count(":") == 1:
            host = v.rsplit(":", 1)[0]
        else:
            host = v
        return host in self.allowed_hosts

    # ------------------------------------------------------------------ status
    def about(self):
        return dict(
            name="yapnr",
            title=self.cfg.title,
            version=yapnr.__version__,
            revision=self.revision,
            # The checkout has uncommitted changes: the revision alone is not the running source.
            modified=self.revision_modified,
            source_url=self.cfg.source_url,
            source_link=source_link(self.cfg.source_url, self.revision),
            license=LICENSE,
            missing_assets=self.dist_missing,
            # What the front end should offer (it skips requests that could only fail).
            features=dict(
                source=self.source_service is not None,
                source_reason=(
                    None
                    if self.source_service is not None
                    else self.sources.reason or "no atopile source configured"
                ),
                agent=self.agent_service is not None,
                viewer3d=self.viewer3d is not None,
            ),
        )

    def lane_brief(self, lane_id):
        """Compact lane facts for the Ask context: objective, opens, run dir, native board path."""
        with self.lock:
            lane = self.state["lanes"].get(lane_id)
            if lane is None:
                return dict(error="unknown lane")
            keys = ("status", "kind", "iteration", "phase", "opens", "violations", "board_sha256")
            out = {k: lane[k] for k in keys if lane.get(k) is not None}
            if lane.get("target"):
                out["routing_target"] = lane["target"]
            if lane.get("last_route"):
                out["last_route"] = {
                    k: lane["last_route"][k]
                    for k in ("accepted", "opens", "phase", "stage", "status", "scope")
                    if k in lane["last_route"]
                }
            frames = lane.get("frames") or []
            out["checkpoints"] = len(frames)
            if frames:
                out["last_checkpoint"] = {
                    k: frames[-1].get(k) for k in ("name", "opens", "violations")
                }
            meta = dict(self.lane_meta.get(lane_id) or {})
            source = self.lane_sources.get(lane_id)
        o = meta.get("objective")
        if isinstance(o, list) and len(o) == 6:
            o = dict(zip(OBJECTIVE_KEYS, o))
        if o is not None:
            out.update(
                objective=o,
                objective_scope=meta.get("score_scope"),
                qualified=meta.get("qualified"),
            )
        out.update(native_board=meta.get("board"), working_board=source)
        try:
            run = self.schematic_service.run_dir(lane_id, source)
        except (OSError, ValueError):
            run = None
        out["run_dir"] = str(run) if run else None
        return out

    def event_brief(self, event_id):
        """One event from the live stream for the Ask context (the data /api/state shows)."""
        with self.lock:
            e = next(
                (x for x in reversed(self.state["events"]) if x.get("id") == event_id),
                None,
            )
        return copy.deepcopy(e) if e else None

    def agent_status(self):
        if self.agent_service:
            s = self.agent_service.status()
        else:
            s = dict(
                available=False,
                reason=self.agent_off_reason,
                models=list(viewer_config.AGENT_MODELS),
                default=self.cfg.agent_model,
                busy=False,
                max_concurrent=0,
                web=False,
                notes=self.notes is not None,
            )
        return dict(
            s,
            enabled=self.agent_service is not None,
            spend=self.spend.snapshot(),
            net_summaries=self.net_summary_status,
            source=bool(self.source_service),
            src_root=str(self.sources.src) if self.sources.src else None,
        )

    # ------------------------------------------------------------------ AI net labels
    def net_summaries(self):
        """Background: label nets once per mechanical dossier, prompt version and model.

        One CLI call; nets a failed chunk left missing are retried alone with backoff, never a
        full re-run per restart. Never blocks serving.
        """
        import fcntl

        from yapnr.viewer.agent import net_llm

        status = self.net_summary_status
        source = self.source_service
        model = self.cfg.net_summary_model

        def current(f):
            try:
                doc = json.loads(f.read_text())
            except (OSError, ValueError):
                return False
            return net_llm.settled(doc, source.index()["dossier_sha"], model)

        try:
            out = source.llm_path
            out.parent.mkdir(parents=True, exist_ok=True)
            if not current(out):
                status.update(state="waiting")
                with open(out.parent / "source-llm.lock", "w") as lk:
                    # A second viewer on the same cache waits, then finds it current.
                    fcntl.flock(lk, fcntl.LOCK_EX)
                    if not current(out):
                        status.update(state="generating")
                        self.log(f"net summaries: generating with {model} -> {out}")
                        net_llm.generate(
                            source,
                            out,
                            model=model,
                            claude_bin=self.cfg.claude or "claude",
                            cache=self.cfg.cache_dir("agent"),
                            log=self.log,
                            budget=self.cfg.net_summary_budget_usd,
                            meter=self.spend,
                        )
            try:
                doc = json.loads(out.read_text())
                missing, capped = len(doc.get("missing") or []), bool(doc.get("capped"))
            except (OSError, ValueError, AttributeError):
                missing, capped = None, False
            if current(out):
                state = "incomplete" if missing else "current"
            else:
                state = "failed"
            if missing and capped:
                state = "capped"
                status["reason"] = (
                    "the spend cap (--agent-total-usd) stopped the labelling; restart to retry the"
                    " missing nets"
                )
            status.update(state=state, missing=missing, nets=source.merge_llm())
        except Exception as ex:
            status.update(state="failed", error=f"{type(ex).__name__}: {ex}"[:400])
            self.log("net summaries failed: " + status["error"])

    # ------------------------------------------------------------------ geometry
    def gzipped(self, raw):
        # One (raw, gz, etag) tuple, swapped atomically between request threads.
        c = self.gzip_cache.get("v")
        if c is None or c[0] is not raw:
            etag = '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'
            c = self.gzip_cache["v"] = (raw, gzip.compress(raw, 6), etag)
        return c[1], c[2]

    def extract(self, board, out, log):
        python, why = self.toolchain.python()
        if python is None:
            raise GeometryUnavailable(
                "board geometry needs KiCad's Python (--kicad-python or YAPNR_KICAD_PYTHON): "
                + str(why)
            )
        proc = subprocess.Popen(
            [str(python), str(EXTRACT_SCRIPT), str(board), str(out)],
            env=runtime.kicad_env(self.runtime),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.extracting[proc] = out
        try:
            code = proc.wait(timeout=EXTRACT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            self.extracting.pop(proc, None)
        if code:
            raise subprocess.CalledProcessError(code, "extract.py")

    def geometry(self, event):
        sha = event["board_sha256"]
        if sha not in self.cache:
            folder = self.root / "geometry"
            folder.mkdir(exist_ok=True)
            f = folder / (sha + ".json")
            if time.time() - self.last_sweep > 600:
                self.last_sweep = time.time()
                sweep_tmp(folder)
            if not f.exists():
                tmp = folder / (sha + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with (folder / (sha + ".log")).open("w") as log:
                        self.extract(event["board"], tmp, log)
                    tmp.replace(f)
                finally:
                    tmp.unlink(missing_ok=True)
            # Another viewer on the same root may still be writing f (older viewers write it in
            # place).
            for attempt in range(40):
                try:
                    self.cache[sha] = json.loads(f.read_text())
                    break
                except ValueError:
                    if attempt == 39:
                        raise
                    time.sleep(0.25)
        return self.cache[sha]

    def event_geometry(self, e):
        """(geometry or None, notice or None) for an event."""
        try:
            if "board" in e:
                return self.geometry(e), None
        except GeometryUnavailable as ex:
            if "layout" not in e:
                return None, str(ex)
        return (from_graph(e["layout"]) if "layout" in e else None), None

    # ------------------------------------------------------------------ ingest
    def ingest(self):
        event_dir = self.root / "events"
        event_dir.mkdir(exist_ok=True)
        last_mtime = None
        last_restart = None
        restart_file = self.experiment / "restart-status.json"
        while True:
            if restart_file.exists():
                stamp_restart = restart_file.stat().st_mtime_ns
                if stamp_restart != last_restart:
                    with self.lock:
                        self.state["restart_status"] = json.loads(restart_file.read_text())
                        self.state["revision"] += 1
                    last_restart = stamp_restart
            stamp = event_dir.stat().st_mtime_ns
            if stamp == last_mtime:
                time.sleep(0.25)
                continue
            # Files are atomically renamed into this immutable event directory. A rename during
            # this scan changes mtime and is discovered on the next pass.
            last_mtime = stamp
            with os.scandir(event_dir) as entries:
                pending = sorted(
                    entry.name
                    for entry in entries
                    if entry.name.endswith(".json") and entry.name not in self.seen
                )
            for name in pending:
                if name not in self.seen:
                    self.ingest_file(event_dir / name)
            time.sleep(0.25)

    def ingest_file(self, f):
        try:
            e = json.loads(f.read_text())
            frame = phase_frame(e) if e["kind"] == "phase_complete" else None
            geo, notice = self.event_geometry(e)
            if notice and notice != self.geometry_notice:
                self.geometry_notice = notice
                self.log(notice)
                with self.lock:
                    self.state["errors"].append(dict(file=f.name, error=notice))
            layout_sha = None
            if e["kind"] == "placement_cost_capture":
                layout_sha = hashlib.sha256(
                    json.dumps(e["layout"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                self.cache[layout_sha] = geo
                write_json_atomic(self.root / "geometry" / (layout_sha + ".json"), geo)
                frame = dict(
                    name=e["data"]["phase"] + " · recorded placement cost",
                    kind="placement-cost",
                    layout_sha256=layout_sha,
                    event_id=e["id"],
                    opens=None,
                    violations=None,
                    label_source="data.phase",
                    metric_sources={},
                    cost_capture=e["data"]["cost_capture"],
                )
            with self.lock:
                self.apply_event(e, frame, geo, layout_sha)
            self.seen.add(f.name)
        except Exception as ex:
            with self.lock:
                self.state["errors"].append(dict(file=f.name, error=str(ex)))
                self.state["errors"] = self.state["errors"][-10:]
                self.state["revision"] += 1
            self.seen.add(f.name)

    def apply_event(self, e, frame, geo, layout_sha):
        """Fold one event into its lane (lock held)."""
        state = self.state
        cand = e["candidate"]
        kind = e["kind"]
        data = e["data"]
        lane = state["lanes"].setdefault(cand, dict(id=cand, draft={}, costs={}, frames=[]))
        lane.update(event_id=e["id"], time=e["time"], iteration=e["iteration"], kind=kind)
        started_at = self.lane_started.setdefault(cand, e["time"])
        lane["started_at"] = started_at
        if isinstance(e.get("source"), str):
            self.lane_sources[cand] = e["source"]
        if isinstance(e.get("board"), str):
            self.lane_meta.setdefault(cand, {})["board"] = e["board"]
        if kind == "candidate_complete":
            self.lane_meta.setdefault(cand, {}).update(
                {k: data.get(k) for k in ("objective", "qualified", "score_scope")}
            )
        if data.get("phase"):
            lane["phase"] = data["phase"]
        if isinstance(data.get("source_round"), (int, float)):
            # yapnr.viewer.progress's lap-compounding model: a feedback round restarting the
            # whole pipeline (pnr.route.feedback's "source P/R round N") must not visibly drop
            # the lane's progress bar back toward zero every round.
            lane["round"] = data["source_round"]
        if kind == "controls_applied":
            state["active_controls"] = data
        if kind == "worker_config_applied":
            lane["worker_config"] = data
        if frame:
            lane["phase"] = frame["name"]
            lane["opens"] = frame["opens"]
            lane["violations"] = frame["violations"]
            lane["phase_label_source"] = frame["label_source"]
            lane["phase_metric_sources"] = frame["metric_sources"]
            lane["phase_accepted"] = frame.get("accepted")
        if geo:
            if lane.get("geometry") and e.get("board_sha256") != lane.get("board_sha256"):
                lane["previous"] = lane["geometry"]
            lane["geometry"] = geo
            lane["board_sha256"] = e.get("board_sha256")
            lane["layout_sha256"] = layout_sha
            lane["geometry_event_id"] = e["id"]
            lane["draft"] = {}
        if frame:
            lane["frames"].append(frame)
        if kind == "route_result":
            lane["opens"] = data.get("opens")
            lane["last_route"] = data
            if data.get("accepted"):
                lane["copper_changed_at"] = time.time()
            else:
                lane["copper_changed_at"] = lane.get("copper_changed_at", 0)
        if kind == "candidate_queued":
            lane["moves"] = data.get("moves", [])
            lane["cost"] = data.get("cost")
        if kind == "route_start":
            lane["target"] = data["target"]
        if kind == "signal_net_added":
            lane["draft"][data["net"]] = data["tracks"]
        if kind == "signal_net_removed":
            lane["draft"].pop(data["net"], None)
        if kind == "placement_costs":
            lane["costs"][data["ref"]] = data
        if kind in ("candidate_complete", "candidate_failed", "case_complete", "case_failed"):
            # The full payload, for yapnr.viewer.progress's status text: a screening-only
            # candidate reports its unconnected count as data.missing_connections (no DRC stage
            # ever ran), while a later, fuller evaluation can report data.opens/data.violations
            # the same way a route_result frame does; progress.py checks both names. A case
            # lane's own case_complete/case_failed (hardware/pnr/regression/run.py) always has
            # opens/violations, the same shape a fuller candidate evaluation does.
            lane["last_candidate"] = data
        if kind == "batch_alternatives":
            state["search"][str(e["iteration"])] = data
        if kind in (
            "candidate_queued",
            "candidate_start",
            "candidate_complete",
            "candidate_failed",
        ):
            lane["status"] = kind.removeprefix("candidate_")
        if kind in ("case_complete", "case_failed"):
            # The ladder runner's terminal event for a case/seed lane (run.py emits it once, when
            # the case ends): a real verdict, not something yapnr.viewer.lane_tree needs to derive
            # from this lane's candidate children once they have all ended.
            lane["status"] = "complete" if kind == "case_complete" else "failed"
        if kind == "task_complete":
            # yapnr.exp.live's mirror-synthesized terminal event (its own SYNTHETIC_KIND -- a
            # plain string here too, same reason MIRROR_FINISHED_MARKER is: yapnr.viewer and
            # yapnr.exp are independent targets) for a task the engine itself never reported a
            # terminal event for at all. Its verdict (from the task's own _DONE marker, which the
            # wrapper always writes regardless of what pnr.live heard) is authoritative, same as
            # case_complete/case_failed above -- yapnr.exp.task._verdict's "pass"/"done" mean
            # success, anything else ("fail", "timeout", "error", or an arbitrary custom string)
            # does not.
            lane["last_candidate"] = data
            lane["status"] = "complete" if data.get("verdict") in ("pass", "done") else "failed"
        if kind == "iteration_complete":
            lane["status"] = "accepted" if data.get("accepted") else "rejected"
        summary = {k: e[k] for k in ("id", "time", "kind", "candidate", "iteration")}
        summary["data"] = {k: v for k, v in data.items() if k not in BULKY_EVENT_KEYS}
        state["events"].append(summary)
        state["events"] = state["events"][-300:]
        state["revision"] += 1

    # ------------------------------------------------------------------ state
    def _run_finished(self) -> bool:
        """Whether this run is already known to be over, so an idle lane with no terminal event
        of its own reads "finished" (yapnr.viewer.progress's ``finished`` flag) rather than
        "stalled" forever -- a mirrored campaign that is simply done looks identical to a dead
        worker otherwise. Two sources, cheap enough to re-check every poll:

        - :data:`MIRROR_FINISHED_MARKER` directly under ``self.root``, written by ``yapnr exp
          live`` (:mod:`yapnr.exp.live`'s own ``FINISHED_MARKER``, same name -- kept as a plain
          string in both rather than a cross-package import so the viewer and the exp CLI stay
          independently buildable) once every task the campaign actually *submitted* has a
          ``_DONE`` marker (its ``tasks.jsonl`` plan only when nothing was submitted yet), and
          withdrawn by the same mirror the moment that stops being true, so its presence alone is
          enough -- this method never has to reason about partial submissions itself.
        - for a local (non-mirrored) run, a ``summary.json`` with ``"complete": true`` in ``root``
          or one of its first few parents -- ``hardware/pnr/regression/run.py --out`` always
          writes one there when it exits, campaign machinery or not (``PNR_LIVE_DIR`` is commonly
          a dot-directory a level or two below that same ``--out``, e.g. ``<out>/.yapnr/live``).
        """
        if (self.root / MIRROR_FINISHED_MARKER).exists():
            return True
        probe = self.root
        for _ in range(4):
            candidate = probe / "summary.json"
            if candidate.is_file():
                try:
                    if json.loads(candidate.read_text()).get("complete"):
                        return True
                except (OSError, ValueError):
                    pass
            if probe.parent == probe:
                break
            probe = probe.parent
        return False

    def _annotate_lanes(self):
        """Fold each lane's raw telemetry into the phase/progress model (yapnr.viewer.progress),
        in place on ``self.state["lanes"]`` (lock held), so a lane with no classifiable event in
        this poll still has its last-known phase to hold over into the next one. Cheap (a handful
        of dict lookups and string comparisons per lane); called at the top of every
        :func:`current`, not from :func:`apply_event`, so it runs at most once per served
        revision rather than once per raw event folded into that revision -- a lane that got
        several raw events in between two polls only has its *last* one's phase visible to the
        held-over fallback, not each intermediate one; see yapnr.viewer.progress's module
        docstring. ``now`` (``time.time()``) is threaded through so a lane with no event in the
        last :data:`yapnr.viewer.progress.STALE_SECONDS` classifies ``stalled`` (or, once
        :func:`_run_finished`, ``finished``) instead of ``running`` forever."""
        now = time.time()
        finished = self._run_finished()
        self.state["campaign_finished"] = finished
        for lane in self.state["lanes"].values():
            classified = classify_lane(lane, now=now, finished=finished)
            lane["status_text"] = classified.pop("status_text")
            lane["progress"] = classified

    def current(self, selected=None, full=True):
        with self.lock:
            self._annotate_lanes()
            # The experiment browser's hierarchy (yapnr.viewer.lane_tree), from the same lanes:
            # one place builds it, so the front end never re-derives the tree from raw ids itself.
            tree = lane_tree.build_tree(self.state["lanes"])
            if full:
                return copy.deepcopy(dict(self.state, tree=tree, server_time=time.time()))
            # Filter before copying: unselected board geometry never enters the copy.
            lanes = {
                key: {
                    k: v
                    for k, v in lane.items()
                    if key == selected or k not in ("geometry", "previous", "draft")
                }
                for key, lane in self.state["lanes"].items()
            }
            return copy.deepcopy(dict(self.state, lanes=lanes, tree=tree, server_time=time.time()))

    def state_response(self, query):
        with self.lock:
            selected = query.get("lane", [None])[0]
            selected = selected or next(
                (k for k in self.state["lanes"] if not k.endswith("/search")), None
            )
            # _run_finished() is cheap (a file-existence check, occasionally a tiny read) and
            # changes outside ingest: a quiescent campaign's MIRROR_FINISHED_MARKER can appear (or
            # be withdrawn -- yapnr.exp.live.synthesize_task_events) with no new event ever
            # following it. A flip bumps "revision" here, the same way a restart-status change
            # does in the poll loop, so the per-revision response cache is rebuilt *and* every
            # client polling with ?since=<old revision> -- not just whichever one asked first --
            # gets the new state instead of the "unchanged" shortcut, so its lanes stop reading
            # "stalled" once the campaign is actually over.
            finished = self._run_finished()
            # Compared with its own last-served value, not state["campaign_finished"]: that one
            # is refreshed by every current() call (_annotate_lanes), so another caller could
            # update it first and hide the flip from this check.
            if finished != getattr(self, "_served_finished", False):
                self._served_finished = finished
                self.state["revision"] += 1
            rev = self.state["revision"]
            run = self.state["run"]
            if query.get("since", [None])[0] == str(rev) and query.get("run", [None])[0] == run:
                return json.dumps(
                    dict(unchanged=True, revision=rev, run=run), separators=(",", ":")
                ).encode()
            key = (rev, selected)
            if key not in self.response_cache:
                # Cache only the current revision; no retained historical geometry copies.
                for old in list(self.response_cache):
                    if old[0] != rev:
                        del self.response_cache[old]
                self.response_cache[key] = json.dumps(
                    self.current(selected, full=False), separators=(",", ":")
                ).encode()
            return self.response_cache[key]

    # ------------------------------------------------------------------ pins and snapshots
    def _pin_summary(self, pin):
        # Immutable pins can be large; repeated keystrokes need only compact identity metadata.
        # Bound this cache and load full geometry only for a final bundle.
        snapshot = json.loads((self.root / "pins" / (pin + ".json")).read_text())
        return dict(
            run=snapshot["run"],
            lanes={
                key: dict(board_sha256=lane.get("board_sha256"), frames=lane.get("frames", []))
                for key, lane in snapshot["lanes"].items()
            },
        )

    def read_annotation(self, body, full=False):
        pin = body["pin_id"]
        if not isinstance(pin, str) or not re.fullmatch("[a-f0-9]{32}", pin):
            raise ValueError("invalid pin")
        summary = self._pins(pin)
        snapshot = None
        if full:
            snapshot = json.loads((self.root / "pins" / (pin + ".json")).read_text())
        selected = body.get("view", {})
        lane = summary["lanes"].get(selected.get("lane"))
        phase = selected.get("phase", "live")
        if lane is None:
            raise ValueError("selected lane is absent from the immutable pin")
        if phase != "live":
            frames = lane.get("frames", [])
            if not isinstance(phase, str) or not phase.isdigit() or int(phase) >= len(frames):
                raise ValueError("selected phase is absent from the immutable pin")
            frame = frames[int(phase)]
            sha = frame.get("board_sha256") or frame["layout_sha256"]
            # A server restart may not have replayed this old frame yet. The exact immutable
            # geometry is already on disk; never substitute a live board.
            if full:
                snapshot["selected_geometry"] = self.cache.get(sha) or json.loads(
                    (self.root / "geometry" / (sha + ".json")).read_text()
                )
        rects = body.get("annotations", [])
        if not isinstance(rects, list) or len(rects) > 500:
            raise ValueError("invalid rectangle list")
        for rect in rects:
            bounds = rect["bounds"]
            if len(bounds) != 4 or not all(
                type(v) in (int, float) and math.isfinite(v) and abs(v) < 100000 for v in bounds
            ):
                raise ValueError("invalid bounds")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise ValueError("invalid note")
        revision = body.get("draft_revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid draft revision")
        payload = dict(
            pin_id=pin,
            run=summary["run"],
            annotations=rects,
            view=selected,
            note=note[:10000],
            draft_revision=revision,
        )
        return pin, snapshot, payload

    # ------------------------------------------------------------------ lifecycle
    def stop(self, signum, frame):
        # Kill a running KiCad extractor and drop its temporary file (a SIGKILLed viewer leaves
        # them to the next sweep).
        for proc, tmp in list(self.extracting.items()):
            try:
                proc.kill()
            except OSError:
                pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if self.agent_service:
            self.agent_service.shutdown()
        if self.viewer3d:
            self.viewer3d.shutdown()  # kills a running export's process group (kicad-cli too)
        os._exit(128 + signum)


class Handler(BaseHTTPRequestHandler):
    viewer: Viewer  # set on the per-server subclass

    def log_message(self, *args):
        pass

    def misdirected(self):
        if self.viewer.host_allowed(self.headers.get("Host")):
            return False
        self.send(
            {
                "error": "unexpected Host header (DNS-rebinding guard); add --allow-host NAME"
                " for another name of this server"
            },
            421,
        )
        return True

    def send(self, obj, status=200):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_raw(self, raw, content_type, cache, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def query(self):
        return parse_qs(urlparse(self.path).query)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if self.misdirected():
            return
        path = self.path.split("?")[0]
        route = GET_ROUTES.get(path)
        if route:
            return route(self, path)
        for prefix, route in GET_PREFIXES:
            if path.startswith(prefix):
                return route(self, path)
        return self.get_static(path)

    def get_about(self, path):
        return self.send(self.viewer.about())

    def get_component_cost(self, path):
        q = self.query()
        try:
            return self.send(
                self.viewer.cost_service.request(
                    q.get("event_id", [""])[0], q.get("ref", [None])[0]
                )
            )
        except (ValueError, KeyError, IndexError, TypeError, FileNotFoundError) as ex:
            return self.send(dict(error=str(ex)), 400)

    def get_schematic(self, path):
        v = self.viewer
        q = self.query()
        lane_id = q.get("lane", [""])[0]
        scope = q.get("scope", ["auto"])[0]
        with v.lock:
            entry = v.state["lanes"].get(lane_id)
            if entry is None:
                return self.send({"error": "unknown lane"}, 404)
            refs = [
                p["ref"]
                for p in (entry.get("geometry") or {}).get("parts", [])
                if isinstance(p.get("ref"), str)
            ]
            source = v.lane_sources.get(lane_id)
        try:
            return self.send(
                v.schematic_service.request(
                    lane_id, refs, source, "board" if scope == "board" else "auto"
                )
            )
        except (ValueError, KeyError, IndexError, TypeError, OSError) as ex:
            return self.send(dict(error=str(ex)), 400)

    def get_schematic_payload(self, path):
        try:
            raw = self.viewer.schematic_service.payload(path.rsplit("/", 1)[-1])
        except ValueError as ex:
            return self.send({"error": str(ex)}, 400)
        except FileNotFoundError as ex:
            return self.send({"error": str(ex)}, 404)
        # Content-addressed by every build input: safe to cache in the browser.
        return self.send_raw(raw, "application/json", "private, max-age=86400, immutable")

    def no_sources(self):
        reason = self.viewer.sources.reason or "no atopile source configured"
        return self.send({"error": reason}, 503)

    def get_source_index(self, path):
        source = self.viewer.source_service
        if source is None:
            return self.no_sources()
        try:
            raw = source.index_bytes()
        except Exception as ex:
            return self.send(
                {"error": f"source index failed: {type(ex).__name__}: {ex}"[:400]}, 500
            )
        gz, etag = self.viewer.gzipped(raw)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        zipped = "gzip" in (self.headers.get("Accept-Encoding") or "")
        extra = [("ETag", etag), ("Vary", "Accept-Encoding")]
        if zipped:
            extra.append(("Content-Encoding", "gzip"))
        return self.send_raw(gz if zipped else raw, "application/json", "no-cache", extra)

    def get_source_file(self, path):
        from yapnr.viewer.sources.service import SourceNotFound

        source = self.viewer.source_service
        if source is None:
            return self.no_sources()
        try:
            return self.send(source.file(self.query().get("path", [""])[0]))
        except SourceNotFound as ex:
            return self.send({"error": str(ex)}, 404)
        except (ValueError, OSError) as ex:
            return self.send({"error": str(ex)}, 400)

    def get_notes(self, path):
        notes = self.viewer.notes
        if notes is None:
            return self.send({"error": "the notes store is not available on this server"}, 404)
        q = self.query()
        if path == "/api/notes":  # {rev, unchanged:true} when since == rev: one stat per poll
            try:
                since = int(q.get("since", [""])[0])
            except ValueError:
                since = None
            return self.send(notes.payload(since))
        fmt = q.get("format", ["md"])[0]
        try:
            raw, ctype = notes.export_data(fmt)
        except ValueError as ex:
            return self.send({"error": str(ex)}, 400)
        extra = [
            ("Content-Disposition", f'inline; filename="design-notes.{fmt}"'),
            ("X-Content-Type-Options", "nosniff"),
        ]
        return self.send_raw(raw, ctype, "no-store", extra)

    def agent_disabled(self):
        return self.send({"error": self.viewer.agent_off_reason}, 503)

    def get_conversations(self, path):
        agent = self.viewer.agent_service
        if agent is None:
            return self.agent_disabled()
        if path == "/api/agent/conversations":
            return self.send(agent.conversations())
        try:
            return self.send(agent.conversation(path[len("/api/agent/conversations/") :]))
        except ValueError as ex:
            return self.send({"error": str(ex)}, 400)
        except KeyError:
            return self.send({"error": "no such conversation"}, 404)

    def local_only(self, doc, keys):
        """doc without the machine paths under keys for a client that is not on this machine."""
        from yapnr.viewer.agent.service import is_loopback

        if is_loopback(self.client_address[0]):
            return doc
        return {k: (None if k in keys else v) for k, v in doc.items()}

    def get_agent_status(self, path):
        return self.send(self.viewer.agent_status())

    def get_3d(self, path):
        # ?peek=1 reports without enqueueing ('idle'): the pane enqueues once its board has
        # stayed put for a moment.
        v = self.viewer
        if v.viewer3d is None:
            return self.send(
                dict(status="unavailable", error="3D view disabled on this server (--viewer3d off)")
            )
        if path == "/api/3d/status":
            return self.send(self.local_only(v.viewer3d.status(), ("cli", "parts", "env")))
        q = self.query()
        lane_id = q.get("lane", [""])[0]
        ph = q.get("phase", ["live"])[0]
        sha = q.get("sha", [""])[0]
        if sha and not re.fullmatch("[a-f0-9]{64}", sha):
            return self.send({"error": "invalid sha"}, 400)
        with v.lock:
            lane = v.state["lanes"].get(lane_id)
            boards = [(v.lane_meta.get(lane_id) or {}).get("board"), v.lane_sources.get(lane_id)]
            if not sha and lane is not None:
                frames = lane.get("frames") or []
                if ph == "live":
                    sha = lane.get("board_sha256")
                elif ph.isdigit() and int(ph) < len(frames):
                    sha = frames[int(ph)].get("board_sha256")
                else:
                    sha = None
        if not sha and lane is None:
            return self.send({"status": "unavailable", "error": "unknown lane"}, 404)
        # sha from the browser (pinned/phase state) or the lane: the immutable boards/<sha> copy,
        # else the lane's board if it still hashes to sha.
        return self.send(
            v.viewer3d.request(
                sha or "",
                [v.root / "boards" / (sha + ".kicad_pcb") if sha else None, *boards],
                retry=q.get("retry", [""])[0] == "1",
                peek=q.get("peek", [""])[0] == "1",
            )
        )

    def get_glb(self, path):
        v = self.viewer
        if v.viewer3d is None:
            return self.send({"error": "3D view disabled"}, 404)
        try:
            raw, enc = v.viewer3d.glb(
                path.rsplit("/", 1)[-1], "gzip" in (self.headers.get("Accept-Encoding") or "")
            )
        except ValueError as ex:
            return self.send({"error": str(ex)}, 400)
        except FileNotFoundError as ex:
            return self.send({"error": str(ex)}, 404)
        # Content addressed (placement fingerprint + export version): immutable in the browser.
        extra = [("Vary", "Accept-Encoding")] + ([("Content-Encoding", enc)] if enc else [])
        return self.send_raw(raw, "model/gltf-binary", "private, max-age=86400, immutable", extra)

    def get_controls(self, path):
        v = self.viewer
        return self.send(
            dict(
                requested=v.read_controls(v.root / "control.json"),
                active=v.state.get("active_controls"),
                limits=v.limits,
                max_total_workers=16,
            )
        )

    def get_state(self, path):
        return self.send(self.viewer.state_response(self.query()))

    def get_timing(self, path):
        from yapnr.viewer.timing import aggregate

        scope = (self.query().get("scope") or [""])[0]
        return self.send(aggregate(self.viewer.root, scope=scope, now=time.time()))

    def get_geometry(self, path):
        v = self.viewer
        sha = path.rsplit("/", 1)[-1]
        if not re.fullmatch("[a-f0-9]{64}", sha):
            return self.send({"error": "not found"}, 404)
        if sha not in v.cache:
            f = v.root / "geometry" / (sha + ".json")
            if not f.exists():
                return self.send({"error": "not found"}, 404)
            v.cache[sha] = json.loads(f.read_text())
        return self.send(v.cache[sha])

    def get_stored(self, path):
        for prefix, directory in (
            ("/api/pins/", "pins"),
            ("/api/drafts/", "drafts"),
            ("/api/snapshots/", "snapshots"),
        ):
            if path.startswith(prefix):
                name = path[len(prefix) :]
                if not re.fullmatch("[a-f0-9]{32}", name):
                    return self.send({"error": "invalid id"}, 400)
                f = self.viewer.root / directory / (name + ".json")
                if not f.exists():
                    return self.send({"error": "not found"}, 404)
                return self.send(json.loads(f.read_text()))
        return self.send({"error": "not found"}, 404)

    def get_static(self, path):
        f = static_path(self.viewer.dist, path)
        if f is None:
            return self.send({"error": "not found"}, 404)
        rel = path.lstrip("/")
        # Pinned third-party files may be cached; our own files never are.
        pinned = rel.startswith(PINNED_PREFIXES)
        ctype = CONTENT_TYPES.get(f.suffix, "text/plain; charset=utf-8")
        return self.send_raw(
            f.read_bytes(), ctype, "private, max-age=86400" if pinned else "no-store"
        )

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if self.misdirected():
            return
        v = self.viewer
        origin = self.headers.get("Origin")
        # The assistant and every notes write need an allowlisted Origin; a missing one is
        # rejected too (other POSTs allow it).
        if self.path == "/api/agent/chat" and origin not in v.origins():
            return self.send({"error": "origin required for the assistant"}, 403)
        notes_path = self.path == "/api/notes" or self.path.startswith("/api/notes/")
        if notes_path and origin not in v.origins():
            return self.send({"error": "origin required for notes"}, 403)
        if origin not in [None, *v.origins()]:
            return self.send({"error": "origin rejected"}, 403)
        size = int(self.headers.get("Content-Length", 0))
        if size > 1000000:
            return self.send({"error": "request too large"}, 413)
        try:
            body = json.loads(self.rfile.read(size) or "{}")
            if not isinstance(body, dict):
                return self.send({"error": "invalid body"}, 400)
            if self.path == "/api/agent/chat":
                return self.chat(body)
            if notes_path:
                return self.notes_write(body)
            if self.path == "/api/agent/cancel":
                if v.agent_service is None:
                    return self.agent_disabled()
                return self.send(v.agent_service.cancel(body.get("session")))
            if self.path == "/api/controls":
                return self.post_controls(body)
            if self.path == "/api/pin":
                key = uuid.uuid4().hex
                snapshot = v.current()
                folder = v.root / "pins"
                folder.mkdir(exist_ok=True)
                (folder / (key + ".json")).write_text(json.dumps(snapshot))
                return self.send(dict(pin_id=key, state=snapshot))
            if self.path in ("/api/draft", "/api/snapshot"):
                return self.post_annotation(body)
            self.send({"error": "not found"}, 404)
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            FileNotFoundError,
        ) as ex:
            self.send({"error": str(ex)}, 400)

    def post_controls(self, body):
        v = self.viewer
        with v.lock:
            updated = save_settings(
                v.root / "control.json",
                v.preferences,
                body["values"],
                body.get("expected_revision"),
                body.get("apply_mode", "boundary"),
            )
            v.state["controls"] = updated
            v.state["revision"] += 1
            folder = v.root / "control-history"
            folder.mkdir(exist_ok=True)
            (folder / (str(updated["revision"]) + ".json")).write_text(
                json.dumps(updated, indent=2)
            )
        return self.send(updated)

    def post_annotation(self, body):
        v = self.viewer
        pin, snapshot, payload = v.read_annotation(body, full=self.path == "/api/snapshot")
        if self.path == "/api/draft":
            path = v.root / "drafts" / (pin + ".json")
            with v.lock:
                old = json.loads(path.read_text()) if path.exists() else {}
                if old.get("draft_revision", -1) > payload["draft_revision"]:
                    return self.send({"error": "newer draft already saved"}, 409)
                draft = dict(payload, schema="pnr-annotation-draft-v1", updated_at=time.time())
                write_json_atomic(path, draft)
            return self.send(
                dict(pin_id=pin, draft_revision=payload["draft_revision"], path=str(path))
            )
        key = uuid.uuid4().hex
        bundle = dict(
            schema="pnr-annotated-snapshot-v1",
            id=key,
            created_at=time.time(),
            coordinate_frame="mm-y-up",
            state=snapshot,
            annotations=payload["annotations"],
            view=payload["view"],
            note=payload["note"],
        )
        dest = v.root / "snapshots" / (key + ".json")
        write_json_atomic(dest, bundle)
        return self.send(dict(id=key, path=str(dest), url="/api/snapshots/" + key))

    def notes_write(self, body):
        """POST /api/notes (create), /api/notes/<id> (fields, comment, expect_rev) and
        /api/notes/<id>/delete.

        Always a user actor: the assistant writes only through its MCP server, so nothing here can
        act as the agent (or set its provenance).
        """
        from yapnr.viewer.notes.store import NoteConflict, NoteNotFound

        notes = self.viewer.notes
        if notes is None:
            return self.send({"error": "the notes store is not available on this server"}, 404)
        m = re.fullmatch(r"/api/notes(?:/(N-\d{4,6})(/delete)?)?", self.path)
        if not m:
            return self.send({"error": "not found"}, 404)
        actor = dict(kind="user", remote=self.client_address[0])
        try:
            if m[2]:
                return self.send(dict(ok=True, **notes.delete(m[1], actor)))
            n = notes.request(m[1], body, actor) if m[1] else notes.create(body, actor)
            return self.send(dict(ok=True, rev=notes.rev, note=n))
        except NoteConflict as ex:  # subclasses ValueError
            return self.send({"error": str(ex)}, 409)
        except PermissionError as ex:
            return self.send({"error": str(ex)}, 403)
        except NoteNotFound as ex:
            return self.send({"error": str(ex)}, 404)
        except ValueError as ex:
            return self.send({"error": str(ex)}, 400)

    def chat(self, body):
        """SSE: one CLI turn on this request thread (other requests keep flowing)."""
        agent = self.viewer.agent_service
        if agent is None:
            return self.agent_disabled()
        agent._validate(body)  # ValueError -> 400 before the stream starts; chat() validates again
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event, data):
            payload = json.dumps(data, separators=(",", ":"))
            try:
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False

        sock = self.connection

        def gone():
            """The browser closed the stream: the socket reads as EOF (the body was consumed)."""
            try:
                if not select.select([sock], [], [], 0)[0]:
                    return False
                return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
            except (BlockingIOError, InterruptedError):
                return False
            except (OSError, ValueError):
                return True

        agent.chat(body, emit, gone=gone)


GET_ROUTES = {
    "/api/about": Handler.get_about,
    "/api/component-cost": Handler.get_component_cost,
    "/api/schematic": Handler.get_schematic,
    "/api/source/index": Handler.get_source_index,
    "/api/source/file": Handler.get_source_file,
    "/api/notes": Handler.get_notes,
    "/api/notes/export": Handler.get_notes,
    "/api/agent/conversations": Handler.get_conversations,
    "/api/agent/status": Handler.get_agent_status,
    "/api/3d": Handler.get_3d,
    "/api/3d/status": Handler.get_3d,
    "/api/controls": Handler.get_controls,
    "/api/state": Handler.get_state,
    "/api/timing": Handler.get_timing,
}
GET_PREFIXES = (
    ("/api/schematic/payload/", Handler.get_schematic_payload),
    ("/api/agent/conversations/", Handler.get_conversations),
    ("/api/3d/glb/", Handler.get_glb),
    ("/api/geometry/", Handler.get_geometry),
    ("/api/pins/", Handler.get_stored),
    ("/api/drafts/", Handler.get_stored),
    ("/api/snapshots/", Handler.get_stored),
)


class Server(ThreadingHTTPServer):
    # Default 5: a page load's parallel fetches were reset under heavy machine load.
    request_queue_size = 64


def serve(viewer: Viewer):
    """Bind every listener, start the background threads and serve until stopped."""
    handler = type("BoundHandler", (Handler,), {"viewer": viewer})
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, viewer.stop)
    geometry = viewer.root / "geometry"
    geometry.mkdir(exist_ok=True)
    sweep_tmp(geometry)
    servers = [Server((host, viewer.port), handler) for host in viewer.listen_hosts]
    viewer.port = servers[0].server_address[1]  # --port 0: the origin allowlist uses it
    threading.Thread(target=viewer.ingest, daemon=True).start()
    for server in servers[:-1]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for server in servers:
        host, port = server.server_address[:2]
        print(f"yapnr viewer: http://{host}:{port}", flush=True)
    if viewer.net_summary_status["state"] == "pending":
        threading.Thread(target=viewer.net_summaries, daemon=True).start()
    if viewer.agent_service:
        # The WebFetch guard's self-test, off the first /api/agent/status request.
        threading.Thread(target=viewer.agent_service.web_status, daemon=True).start()
    viewer.log(f"notes: {viewer.notes_dir}" if viewer.notes else "notes: off")
    servers[-1].serve_forever()


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        cfg = viewer_config.load(argv)
        viewer = Viewer(cfg)
    except viewer_config.ConfigError as ex:
        print(f"yapnr viewer: {ex}", file=sys.stderr)
        return 2
    serve(viewer)
    return 0


if __name__ == "__main__":
    # `python yapnr/viewer/server.py` from a checkout (with the repository root and the engine on
    # PYTHONPATH) runs the same entry point as `python -m yapnr.viewer`, in the package's module.
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PACKAGE.resolve()]
    from yapnr.viewer.server import main as package_main

    raise SystemExit(package_main())
