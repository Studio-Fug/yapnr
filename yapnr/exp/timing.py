"""Timing support for ``yapnr exp``: resolving a campaign to its live directory for ``yapnr exp
timing`` (CLI), and turning a GCP Batch task's own status history into ``task_timing`` live
events for the mirror (``yapnr exp live``) -- queue wait, boot+fetch, run, each a real spans with
real seconds, not derived from event timestamps the way ``yapnr.viewer.timing``'s fallback
estimator has to for a campaign with no stage events at all.

Batch's ``tasks.list`` response (``yapnr/exp/backends/gcp_batch.py``'s ``Gcp.state``) gives each
task a ``status.statusEvents`` list: ``{"taskState": "QUEUED"|"SCHEDULED"|"RUNNING"|"SUCCEEDED"|
"FAILED"|..., "eventTime": RFC3339}``, in the order Batch reports them, which a preempted and
retried task repeats (another QUEUED, another SCHEDULED, ...) rather than replacing. This module
reads exactly that shape, tested against a fake list of status events (``tests/unit/exp/
test_timing.py``) rather than a live GCP call.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

EVENT_SCHEMA = "pnr-live-event-v1"

# Batch's taskState values that open one of our three observable spans; SUCCEEDED/FAILED/ASSIGNED
# and friends close whichever span was open without opening a new one (`None`).
_STAGE_FOR_STATE = {
    "PENDING": "queue-wait",
    "QUEUED": "queue-wait",
    "SCHEDULED": "boot-fetch",
    "ASSIGNED": "boot-fetch",
    "RUNNING": "run",
}

# The Batch states that mean the task is genuinely done -- as opposed to a "run" span merely
# closing because the task was preempted and went back to QUEUED for a retry. Only a `run` span
# closed by one of these should ever read as the lane finishing
# (``yapnr.viewer.timing``'s ``TERMINAL_KINDS``/`_lane_terminal`).
_TERMINAL_TASK_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "DELETED"})

_TASK_INDEX_RE = re.compile(r"/tasks/(\d+)$")


def _parse_rfc3339(value: str) -> float:
    # Batch emits RFC3339 with a trailing "Z"; datetime.fromisoformat wants "+00:00" pre-3.11.
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()


def task_state_spans(
    status_events: Sequence[Dict[str, Any]],
    now: Optional[float] = None,
    include_open: bool = True,
) -> List[Dict[str, Any]]:
    """One span per consecutive pair of Batch status events whose opening state is observable
    (queue wait / boot+fetch / run). A task's status events are already time-ordered by Batch;
    this still sorts defensively. A still-open final span (the task has not reached a terminal
    state yet) runs to ``now`` (default: the wall clock) when ``include_open`` -- a running
    task's queue wait or boot time so far is real information -- but ``now`` keeps moving between
    polls, so a *persisted* span (:func:`emit_task_timing`) must never be an open one: it calls
    this with ``include_open=False`` so the same status history always yields the same spans.
    """
    parsed: List[Tuple[str, float]] = []
    for event in status_events:
        state = event.get("taskState")
        when = event.get("eventTime")
        if not state or not when:
            continue
        try:
            parsed.append((state, _parse_rfc3339(when)))
        except ValueError:
            continue
    parsed.sort(key=lambda pair: pair[1])
    now = now if now is not None else time.time()
    spans = []
    for i, (state, start) in enumerate(parsed):
        stage = _STAGE_FOR_STATE.get(state)
        if stage is None:
            continue
        if i + 1 < len(parsed):
            end = parsed[i + 1][1]
            closed_by = parsed[i + 1][0]
        elif include_open:
            end = now
            closed_by = None  # still open: not closed by anything, so never terminal
        else:
            continue
        if end <= start:
            continue
        spans.append(
            dict(
                stage=stage,
                state=state,
                start=start,
                end=end,
                seconds=end - start,
                # Only a span closed by a genuinely terminal Batch state is the task actually
                # finishing; one closed by QUEUED/SCHEDULED/ASSIGNED/RUNNING again is a
                # preemption retry, still mid-flight (see _TERMINAL_TASK_STATES above).
                terminal=closed_by in _TERMINAL_TASK_STATES,
            )
        )
    return spans


def _write_event_atomic(events_dir: Path, event: Dict[str, Any]) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    dest = events_dir / (event["id"] + ".json")
    if dest.exists():  # idempotent: a re-poll of the same task's status must not duplicate spans
        return
    tmp = dest.with_suffix(".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(event, separators=(",", ":")))
    tmp.replace(dest)


def emit_task_timing(
    dest: Path,
    candidate: str,
    status_events: Sequence[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> int:
    """Write one ``task_timing`` event per observable span of this task's status history into
    ``dest/events`` (the same directory layout ``yapnr.exp.live.unpack_bundle`` writes into, and
    that ``yapnr.viewer.timing._observed_spans`` reads as real, non-estimated spans). Returns how
    many events were newly written (0 on a re-poll with nothing new: each span's id is derived
    from its own start/end/state, so writing it again is a no-op, not a duplicate).
    """
    written = 0
    for span in task_state_spans(status_events, now=now, include_open=False):
        event_id = (
            "task-timing-%s"
            % uuid.uuid5(
                uuid.NAMESPACE_URL,
                "%s|%s|%r|%r" % (candidate, span["state"], span["start"], span["end"]),
            ).hex
        )
        event = dict(
            schema=EVENT_SCHEMA,
            id=event_id,
            time=span["end"],
            kind="task_timing",
            candidate=candidate,
            iteration=None,
            data=dict(
                stage=span["stage"],
                label=span["state"],
                seconds=span["seconds"],
                terminal=span["terminal"],
            ),
        )
        before = (dest / "events" / (event_id + ".json")).exists()
        _write_event_atomic(dest / "events", event)
        if not before:
            written += 1
    return written


def mirror_task_timing(
    dest: Path,
    tasks: Sequence[Dict[str, Any]],
    *,
    candidate_for: Any = lambda task: task.get("name") or task.get("uid"),
    now: Optional[float] = None,
) -> int:
    """``emit_task_timing`` for every task in a ``batch tasks list``-shaped response (or any
    iterable of ``{"status": {"statusEvents": [...]}, ...}`` dicts -- a fake store in tests, or
    the real list the ``yapnr exp live`` mirror already fetches per poll). Never raises: a task
    whose shape doesn't match is skipped, so one odd record cannot stop the whole poll.
    """
    total = 0
    for task in tasks:
        try:
            candidate = candidate_for(task)
            status_events = (task.get("status") or {}).get("statusEvents") or []
        except (AttributeError, TypeError):
            continue
        if not candidate:
            continue
        total += emit_task_timing(dest, candidate, status_events, now=now)
    return total


def candidate_for_gcp_task(
    task: Dict[str, Any], indices: Sequence[str], task_ids: Sequence[str]
) -> Optional[str]:
    """The campaign's own task id for one GCP Batch task record (``batch tasks list``), via its
    task-group index.

    Unlike the Slurm backend (``yapnr.exp.backends.slurm``, which packs ``chunk`` consecutive
    tasks into one array element on purpose), GCP Batch runs exactly one of our tasks per Batch
    task -- Batch's own ``taskCount``/parallelism already does the fan-out -- so a Batch task's
    numeric index (the trailing integer in its ``name``,
    ``.../taskGroups/group0/tasks/<index>``) maps 1:1 through the submission's own ``.indices``
    file (line numbers into ``tasks.jsonl``, in submission order -- see ``yapnr.exp.task.Campaign``)
    to exactly one task id. Returns ``None`` for a malformed or out-of-range record rather than
    raising, the same tolerance :func:`mirror_task_timing` already gives a bad task record.
    """
    match = _TASK_INDEX_RE.search(task.get("name") or "")
    if not match:
        return None
    index = int(match.group(1))
    if not 0 <= index < len(indices):
        return None
    try:
        line_no = int(indices[index])
    except ValueError:
        return None
    if not 0 <= line_no < len(task_ids):
        return None
    return task_ids[line_no]


def mirror_gcp_batch_task_timing(
    dest: Path,
    tasks: Sequence[Dict[str, Any]],
    indices: Sequence[str],
    task_ids: Sequence[str],
    *,
    now: Optional[float] = None,
) -> int:
    """:func:`mirror_task_timing` keyed by the campaign's own task id (not Batch's task name),
    via :func:`candidate_for_gcp_task`. ``indices``/``task_ids``: one submission's
    ``submissions/<n>.indices`` content (already split into strings) and ``plan.tasks``' own
    ``id`` fields, in ``tasks.jsonl`` line order -- both already in hand at every call site
    (``yapnr exp live``, ``yapnr exp status``) that also has ``tasks`` itself."""
    return mirror_task_timing(
        dest,
        tasks,
        candidate_for=lambda task: candidate_for_gcp_task(task, indices, task_ids),
        now=now,
    )


# ------------------------------------------------------------------ CLI (`yapnr exp timing`)


def resolve_live_dir(target: str, cfg=None) -> Path:
    """``target`` as a live directory: used directly if it already looks like one (has an
    ``events`` subdirectory), else resolved as a campaign id through the owner config the way
    ``yapnr exp live`` resolves its own ``--out`` default."""
    path = Path(target).expanduser()
    if (path / "events").is_dir():
        return path
    from yapnr.exp import config as config_mod
    from yapnr.exp.cli import _find_plan

    cfg = cfg or config_mod.load(None)
    plan = _find_plan(target, cfg)
    return cfg.local.store_path(plan.private) / "live" / plan.id


def format_table(result: Dict[str, Any]) -> str:
    lines = [
        "mode: %s  (%d lane(s), %d still running)  wall: %.1fs  coverage: %.1f%%"
        % (
            result["mode"],
            result["lane_count"],
            result["running_count"],
            result.get("wall_seconds", 0.0),
            result.get("coverage", 1.0) * 100,
        ),
        "",
        "%-26s %6s %10s %9s %9s %9s %7s"
        % ("stage", "n", "total_s", "mean_s", "median_s", "p90_s", "share"),
    ]
    for stage in result["stage_order"]:
        s = result["stages"][stage]
        lines.append(
            "%-26s %6d %10.1f %9.2f %9.2f %9.2f %6.1f%%"
            % (stage, s["count"], s["total"], s["mean"], s["median"], s["p90"], s["share"] * 100)
        )
    unattributed = result.get("unattributed")
    if unattributed:
        # Real lane wall-clock time no stage span claims (instrumentation gaps, or a mirror's
        # detection lag past the lane's own last real event) -- see yapnr.viewer.timing's
        # docstring. Shown like a stage row, but its "share" is of lane-wall, not total_seconds
        # (it is not part of total_seconds at all).
        lines.append(
            "%-26s %6d %10.1f %9.2f %9.2f %9.2f %6.1f%%"
            % (
                "unattributed",
                unattributed["count"],
                unattributed["total"],
                unattributed["mean"],
                unattributed["median"],
                unattributed["p90"],
                unattributed.get("share", 0.0) * 100,
            )
        )
    lines.append("")
    lines.append("slowest lanes:")
    for lane in result["slowest"]:
        flag = "  (running)" if lane["running"] else ""
        lines.append("  %9.1fs  %s%s" % (lane["seconds"], lane["candidate"], flag))
    return "\n".join(lines)
