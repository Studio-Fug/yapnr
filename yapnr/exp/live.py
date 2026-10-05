"""The live viewer mirror: pull a GCP-resident (or Slurm-resident) campaign's live bundles into a
local live directory the viewer reads (docs/viewer.md, docs/cloud-experiments.md "Live viewer
mirror").

A task whose campaign has ``[live] enabled = true`` points ``PNR_LIVE_DIR`` at a scratch
directory and ``PNR_LIVE_CANDIDATE`` at its task id (``yapnr.exp.task.task_environment``); the
wrapper's ``LiveUploader`` packs the new events (and the boards they reference) written since the
last upload into one bundle, uploaded to the runs store every ``interval_s`` and once at the end,
at::

    campaigns/<cid>/live/<task key>/<attempt>/<seq>.tar.gz

``mirror`` (``yapnr exp live``) polls the store for new bundles and unpacks them into one shared
local directory's ``events/`` and ``boards/`` (the layout ``bazel run //:viewer -- --root`` reads):
every task's events carry its own ``candidate`` (its task id), so the viewer's existing lane
grouping gives each task its own lane with no further bookkeeping. Unpacking is idempotent (an
event or a board already on disk is left alone; boards are content-addressed) and the mirror's
per-lane progress is saved after every bundle, so it resumes cleanly if interrupted.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from yapnr.exp import bundle
from yapnr.exp.backends.base import campaign_prefix
from yapnr.exp.store import Store

EVENT_SCHEMA = "pnr-live-event-v1"
STATE_FILE = ".yapnr-live-state.json"
LIVE_GLOB = "live/*/*/*.tar.gz"
BUNDLE_RE = re.compile(r"^(?P<task_key>[^/]+)/(?P<attempt>[^/]+)/(?P<seq>[0-9]{6})\.tar\.gz$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def event_errors(event: Any) -> List[str]:
    """Every way ``event`` differs from ``pnr-live-event-v1``; empty when it is valid."""
    if not isinstance(event, dict):
        return ["a live event is a JSON object"]
    errors = []
    if event.get("schema") != EVENT_SCHEMA:
        errors.append("schema must be %r" % EVENT_SCHEMA)
    for key in ("id", "kind", "candidate"):
        if not isinstance(event.get(key), str) or not event[key]:
            errors.append("%s is a non-empty string" % key)
    if not isinstance(event.get("time"), (int, float)) or isinstance(event.get("time"), bool):
        errors.append("time is a number")
    if not isinstance(event.get("data"), dict):
        errors.append("data is an object")
    if "board_sha256" in event and not SHA256_RE.match(str(event["board_sha256"])):
        errors.append("board_sha256 is a sha256 hex digest")
    return errors


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    tmp.write_bytes(data)
    os.replace(tmp, path)


def unpack_bundle(data: bytes, dest: Path) -> Dict[str, int]:
    """Extract one bundle's events and boards into ``dest`` (``events/``, ``boards/``).

    Idempotent: an event or board file already present is left as is. Every event's ``board``
    field is rewritten to the local mirror's copy (the path the uploading task wrote is
    meaningless anywhere else); the board itself is matched by its ``board_sha256``, not by name.
    """
    dest = Path(dest)
    events_dir, boards_dir = dest / "events", dest / "boards"
    added = {"events": 0, "boards": 0}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        for member in members:
            bundle.unsafe_member(member)
        boards = {m.name: m for m in members if m.name.startswith("boards/")}
        events = {m.name: m for m in members if m.name.startswith("events/")}
        for name, member in boards.items():
            target = boards_dir / Path(name).name
            if target.exists():
                continue
            _write_atomic(target, tar.extractfile(member).read())
            added["boards"] += 1
        for name, member in events.items():
            target = events_dir / Path(name).name
            if target.exists():
                continue
            try:
                event = json.loads(tar.extractfile(member).read().decode())
            except (ValueError, UnicodeDecodeError):
                continue
            if event_errors(event):
                continue
            sha = event.get("board_sha256")
            if sha:
                event["board"] = str((boards_dir / (sha + ".kicad_pcb")).resolve())
            _write_atomic(target, json.dumps(event, separators=(",", ":"), default=str).encode())
            added["events"] += 1
    return added


class LiveState:
    """Per-lane progress (``<task key>/<attempt>`` -> the next seq to fetch), saved in the mirror."""

    def __init__(self, lanes: Optional[Dict[str, int]] = None):
        self.lanes: Dict[str, int] = dict(lanes or {})

    @classmethod
    def load(cls, dest: Path) -> "LiveState":
        try:
            data = json.loads((Path(dest) / STATE_FILE).read_text())
        except (OSError, ValueError):
            data = {}
        return cls(data.get("lanes") if isinstance(data, dict) else None)

    def save(self, dest: Path) -> None:
        _write_atomic(
            Path(dest) / STATE_FILE,
            json.dumps({"lanes": self.lanes}, sort_keys=True).encode(),
        )

    @staticmethod
    def _key(lane_task_key: str, attempt: str) -> str:
        return "%s/%s" % (lane_task_key, attempt)

    def next_seq(self, lane_task_key: str, attempt: str) -> int:
        return int(self.lanes.get(self._key(lane_task_key, attempt), 0))

    def advance(self, lane_task_key: str, attempt: str, seq: int) -> None:
        self.lanes[self._key(lane_task_key, attempt)] = seq


def list_bundles(runs: Store, cid: str) -> List[Tuple[str, str, int, str]]:
    """Every live bundle of a campaign: ``(task key, attempt, seq, store path)``."""
    prefix = "%s/%s" % (campaign_prefix(cid), LIVE_GLOB)
    base = "%s/live/" % campaign_prefix(cid)
    out = []
    for path in runs.list(prefix):
        match = BUNDLE_RE.match(path[len(base) :]) if path.startswith(base) else None
        if match:
            out.append((match["task_key"], match["attempt"], int(match["seq"]), path))
    return out


def mirror_once(runs: Store, cid: str, dest: Path, state: LiveState) -> Dict[str, int]:
    """Fetch and unpack every bundle not yet in ``state``; returns bundle/event/board counts."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    by_lane: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
    for lane_task_key, attempt, seq, path in list_bundles(runs, cid):
        by_lane.setdefault((lane_task_key, attempt), []).append((seq, path))
    counts = {"bundles": 0, "events": 0, "boards": 0}
    for (lane_task_key, attempt), items in by_lane.items():
        next_seq = state.next_seq(lane_task_key, attempt)
        for seq, path in sorted(items):
            if seq < next_seq:
                continue
            added = unpack_bundle(runs.read_bytes(path), dest)
            counts["bundles"] += 1
            counts["events"] += added["events"]
            counts["boards"] += added["boards"]
            state.advance(lane_task_key, attempt, seq + 1)
            state.save(dest)
    return counts


def mirror(
    runs: Store,
    cid: str,
    dest: Path,
    *,
    interval_s: float = 20.0,
    once: bool = False,
    say: Optional[Callable[[str], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, int]:
    """Poll ``runs`` for a campaign's live bundles until ``once`` is satisfied or interrupted."""
    say = say or (lambda _msg: None)
    dest = Path(dest)
    state = LiveState.load(dest)
    total = {"polls": 0, "bundles": 0, "events": 0, "boards": 0}
    while True:
        found = mirror_once(runs, cid, dest, state)
        total["polls"] += 1
        for key in ("bundles", "events", "boards"):
            total[key] += found[key]
        if found["bundles"]:
            say(
                "mirrored %d bundle(s): %d event(s), %d board(s)"
                % (found["bundles"], found["events"], found["boards"])
            )
        if once:
            return total
        sleep(interval_s)
