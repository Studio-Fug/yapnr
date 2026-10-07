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
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from yapnr.exp import bundle
from yapnr.exp.backends.base import campaign_prefix, done_markers, submissions, task_key
from yapnr.exp.store import Store

EVENT_SCHEMA = "pnr-live-event-v1"
STATE_FILE = ".yapnr-live-state.json"
LIVE_GLOB = "live/*/*/*.tar.gz"
BUNDLE_RE = re.compile(r"^(?P<task_key>[^/]+)/(?P<attempt>[^/]+)/(?P<seq>[0-9]{6})\.tar\.gz$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Written into the local mirror directory, next to events/ and boards/, once every task the
# campaign actually submitted (required_task_ids -- its submissions when it has any, else the
# full tasks.jsonl plan) has a _DONE marker; yapnr.viewer.server's own copy of this name
# (MIRROR_FINISHED_MARKER) must match -- see synthesize_task_events and that module's
# Viewer._run_finished.
FINISHED_MARKER = "campaign-finished.json"
# The event kind a synthesized terminal event carries; distinct from any real engine kind so a
# viewer (or a person reading raw telemetry) can tell it was reconstructed from a _DONE marker,
# not emitted live by the task itself.
SYNTHETIC_KIND = "task_complete"


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
            # "source" is the uploading host's absolute path to the original board file; it is
            # meaningless (and, off GCP, potentially a user home path) once mirrored elsewhere.
            event.pop("source", None)
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


def mirror_once(
    runs: Store,
    cid: str,
    dest: Path,
    state: LiveState,
    *,
    say: Optional[Callable[[str], None]] = None,
) -> Dict[str, int]:
    """Fetch and unpack every bundle not yet in ``state``; returns bundle/event/board counts.

    A bundle that fails to read or unpack (truncated upload, interrupted write that still
    happened to match the ``*.tar.gz`` glob) is skipped with a warning rather than raised: state
    still advances past it, so one bad bundle cannot wedge the mirror loop on every later poll.
    """
    say = say or (lambda _msg: None)
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
            try:
                added = unpack_bundle(runs.read_bytes(path), dest)
            except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
                say("live: skipping unreadable bundle %s: %s" % (path, error))
                state.advance(lane_task_key, attempt, seq + 1)
                state.save(dest)
                continue
            counts["bundles"] += 1
            counts["events"] += added["events"]
            counts["boards"] += added["boards"]
            state.advance(lane_task_key, attempt, seq + 1)
            state.save(dest)
    return counts


def task_ids(runs: Store, cid: str) -> List[str]:
    """Every task id the campaign's own plan names (``tasks.jsonl``, uploaded once at submit
    time -- the same file :func:`yapnr.exp.fetch.load_campaign` reads), or ``[]`` if it has not
    been uploaded yet (too early to poll, or a campaign this mirror does not actually own)."""
    try:
        lines = runs.read_text("%s/tasks.jsonl" % campaign_prefix(cid)).splitlines()
    except Exception:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line)["id"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _submission_task_ids(
    runs: Store, cid: str, ordinal_ids: List[str], number: int
) -> Optional[List[str]]:
    """The task ids one submission covers, resolved from its ``<n>.indices`` file (line numbers
    into the plan, in ``tasks.jsonl`` order) against ``ordinal_ids``; ``None`` when the indices
    file cannot be read yet (a submission whose record just landed but whose indices write has
    not; ``Backend.submit`` writes indices first, so this should be momentary) or at all."""
    rel = "%s/submissions/%d.indices" % (campaign_prefix(cid), number)
    try:
        text = runs.read_text(rel)
    except Exception:
        return None
    out = []
    for line in text.split():
        line = line.strip()
        if not line:
            continue
        try:
            index = int(line)
        except ValueError:
            continue
        if 0 <= index < len(ordinal_ids):
            out.append(ordinal_ids[index])
    return out


def required_task_ids(runs: Store, cid: str) -> Tuple[List[str], List[int], bool]:
    """The tasks a campaign must finish before :data:`FINISHED_MARKER` is written.

    A campaign rarely submits its whole plan at once (``yapnr exp submit`` re-run after a
    partial failure, ``--only``, a class added later): the set that must all be ``_DONE`` is the
    *union of what every submission actually covers*, not everything :func:`task_ids` names --
    ``tasks.jsonl`` is the full plan, which may hold many tasks no submission has ever launched
    and that may never run. Each submission's own record (``submissions/<n>.json``, read by
    :func:`yapnr.exp.backends.base.submissions`) only carries a task *count*; which tasks those
    are comes from its ``<n>.indices`` file, resolved against ``tasks.jsonl``'s order.

    Falls back to every id :func:`task_ids` names when the campaign has not written a single
    submission record yet -- a plan this mirror cannot tell apart from "about to submit
    everything in one call" without one, so requiring the full plan is the only sound default.

    A ``dry_run`` submission (``yapnr exp submit --dry-run``, previewing cost/placement) never
    actually launches anything -- the same reason :func:`yapnr.exp.backends.base.Backend.
    live_overlap` ignores them -- so it never counts toward what the campaign must finish either;
    counting it would make a campaign that was only ever dry-run-previewed, then really submitted
    in full elsewhere, wait forever on tasks nothing real ever touched.

    Returns ``(ids, submission numbers, complete)``; ``complete`` is false when some submission's
    indices could not be read, in which case the ids returned are a partial union and the caller
    must not treat them as the whole story (never finished on an incomplete read)."""
    ordinal_ids = task_ids(runs, cid)
    records = [r for r in submissions(runs, cid) if not r.get("dry_run")]
    if not records:
        return ordinal_ids, [], True
    seen: Dict[str, None] = {}
    numbers: List[int] = []
    complete = True
    for record in records:
        number = record.get("submission")
        if not isinstance(number, int):
            continue
        numbers.append(number)
        found = _submission_task_ids(runs, cid, ordinal_ids, number)
        if found is None:
            complete = False
            continue
        for tid in found:
            seen[tid] = None
    return list(seen), numbers, complete


def _record_extra(runs: Store, cid: str, task_id: str, marker: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort extra fields (``exit_code``, ``wall_s``, ``timed_out``) from the task's own
    ``record.json``, read directly from the store at the attempt the ``_DONE`` marker names --
    never the full ``raw/`` sync :func:`yapnr.exp.fetch.fetch` does, which costs real egress.
    The marker alone (``verdict``, ``attempt``) is already enough to synthesize a usable event;
    this is purely extra detail for the status line, so any failure to read it is silent."""
    attempt = marker.get("attempt")
    if not attempt:
        return {}
    rel = "%s/tasks/%s/%s/record.json" % (campaign_prefix(cid), task_key(task_id), attempt)
    try:
        record = json.loads(runs.read_text(rel))
    except Exception:
        return {}
    if not isinstance(record, dict):
        return {}
    return {k: record[k] for k in ("exit_code", "wall_s", "timed_out") if k in record}


def _marker_submissions(path: Path) -> Optional[List[int]]:
    """The ``submissions`` an existing :data:`FINISHED_MARKER` names, or ``None`` when there is
    no marker (or it cannot be read, so it gets rewritten)."""
    try:
        value = json.loads(path.read_text()).get("submissions")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, list) else None


def synthesize_task_events(runs: Store, cid: str, dest: Path) -> Dict[str, Any]:
    """Give every finished task lane a terminal event, even one the engine itself never emitted.

    The ladder runner's parent "case" lanes (``ladder/<case>/sN``) are the real-world reason this
    exists: before ``hardware/pnr/regression/run.py`` learned to emit its own ``case_complete``/
    ``case_failed``, a campaign's task-level verdict (the ``_DONE`` marker the wrapper always
    writes, independent of what the engine chose to tell ``pnr.live``) was the *only* place that
    lane's outcome existed at all. One synthetic event per task with a ``_DONE`` marker is written
    into ``dest/events`` (named deterministically from the task id, so a repeat call never
    duplicates one -- idempotent and safe to call every poll), with ``kind`` ``SYNTHETIC_KIND``,
    ``data.verdict``/``data.attempt`` plus whatever :func:`_record_extra` found, and a top-level
    ``synthetic: true``. Once every task :func:`required_task_ids` names has one,
    :data:`FINISHED_MARKER` is written into ``dest`` as well, which is how
    :mod:`yapnr.viewer.server` tells an otherwise-idle lane with no terminal event of its own (and
    no children to derive one from) "finished" instead of "stalled" forever.

    A campaign that only ever submitted part of its plan (``tasks.jsonl`` names more tasks than
    any submission covers) finishes once its *submitted* tasks are all done -- never waiting on
    tasks nothing ever launched. Should a later poll see a submission (``yapnr exp submit`` run
    again, adding more of the plan) that is not yet all done, an already-written marker is
    withdrawn (deleted) rather than left to claim a campaign is finished when it no longer is; it
    is rewritten once that submission's tasks finish too. An inconclusive poll (a submission's own
    ``.indices`` file could not be read) touches no marker either way and reports the previous
    on-disk state, rather than risk flapping on a transient store error.

    Resumable the same way :func:`mirror_once` is: nothing here depends on having run before, a
    task whose marker has not landed yet is simply picked up on the next call, and an interrupted
    call leaves nothing half-written (``_write_atomic``).
    """
    dest = Path(dest)
    events_dir = dest / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    markers = done_markers(runs, cid)
    added = 0
    for task_id, marker in markers.items():
        path = events_dir / ("synthetic-" + task_key(task_id) + ".json")
        if path.exists():
            continue
        data: Dict[str, Any] = dict(verdict=marker.get("verdict"), attempt=marker.get("attempt"))
        data.update(_record_extra(runs, cid, task_id, marker))
        event = dict(
            schema=EVENT_SCHEMA,
            id=path.stem,
            time=time.time(),
            kind=SYNTHETIC_KIND,
            candidate=task_id,
            iteration=None,
            data=data,
            synthetic=True,
        )
        _write_atomic(path, json.dumps(event, separators=(",", ":"), default=str).encode())
        added += 1
    ids, submission_numbers, complete = required_task_ids(runs, cid)
    marker_path = dest / FINISHED_MARKER
    if complete:
        finished = bool(ids) and all(tid in markers for tid in ids)
        if finished:
            # Idempotent: only (re)written when absent or when it names a different set of
            # submissions than the ones it now covers, so its "submissions" never goes stale.
            if _marker_submissions(marker_path) != sorted(submission_numbers):
                _write_atomic(
                    marker_path,
                    json.dumps(
                        {
                            "campaign": cid,
                            "tasks": len(ids),
                            "submissions": sorted(submission_numbers),
                            "finished_at": time.time(),
                        },
                        sort_keys=True,
                    ).encode(),
                )
        elif marker_path.exists():
            # A newer submission (or one whose indices only just became readable) added tasks
            # that are not all done yet: the marker's claim no longer holds, so withdraw it
            # rather than let the viewer keep reporting a finished campaign that is not.
            marker_path.unlink()
    else:
        # Could not tell this poll (an indices file failed to read): report the marker's
        # existing on-disk state rather than guess, and leave it untouched either way.
        finished = marker_path.exists()
    return {"tasks": len(markers), "synthetic_events": added, "finished": finished}


def mirror(
    runs: Store,
    cid: str,
    dest: Path,
    *,
    interval_s: float = 20.0,
    once: bool = False,
    say: Optional[Callable[[str], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Poll ``runs`` for a campaign's live bundles until ``once`` is satisfied or interrupted."""
    say = say or (lambda _msg: None)
    dest = Path(dest)
    state = LiveState.load(dest)
    total = {
        "polls": 0,
        "bundles": 0,
        "events": 0,
        "boards": 0,
        "synthetic_events": 0,
        "finished": False,
    }
    while True:
        found = mirror_once(runs, cid, dest, state, say=say)
        total["polls"] += 1
        for key in ("bundles", "events", "boards"):
            total[key] += found[key]
        if found["bundles"]:
            say(
                "mirrored %d bundle(s): %d event(s), %d board(s)"
                % (found["bundles"], found["events"], found["boards"])
            )
        synth = synthesize_task_events(runs, cid, dest)
        total["synthetic_events"] += synth["synthetic_events"]
        if synth["finished"] and not total["finished"]:
            say("live: campaign %s finished (%d task(s))" % (cid, synth["tasks"]))
        total["finished"] = synth["finished"]
        if once:
            return total
        sleep(interval_s)
