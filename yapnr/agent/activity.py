"""Project presence and timing derived from recorded lifecycle/tool evidence."""

import datetime as dt
import json
import re
from pathlib import Path

from yapnr.agent import workspace

ID = re.compile(r"[a-f0-9-]{36}\Z")


def timestamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


def presence(project, data, now=None):
    root = Path(project).resolve()
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    if data.get("action") not in ("open", "heartbeat", "close"):
        raise ValueError("Invalid project lifecycle action")
    if not all(ID.fullmatch(data.get(k, "")) for k in ("session", "client")):
        raise ValueError("Invalid project/client session ID")
    events = []
    with workspace.lock(root) as folder:
        path = folder / "presence.json"
        state = workspace.read_json(path, {"schema": "yapnr-project-presence-v1", "sessions": {}})
        entry = state["sessions"].get(data["session"])
        if entry is None:
            if data["action"] != "open":
                raise ValueError("Open the project session first")
            entry = {
                "id": data["session"],
                "client": data["client"],
                "opened": now,
                "last_seen": now,
            }
            state["sessions"][data["session"]] = entry
            events.append(
                {
                    "source": "project-opened",
                    "event_id": entry["id"] + ":open",
                    "session": dict(entry),
                }
            )
        if entry["client"] != data["client"]:
            raise ValueError("Session belongs to another client")
        if not entry.get("closed"):
            entry["last_seen"] = max(entry["last_seen"], now, key=timestamp)
            if data["action"] == "close":
                entry["closed"] = max(entry["last_seen"], now, key=timestamp)
                events.append(
                    {
                        "source": "project-closed",
                        "event_id": entry["id"] + ":close",
                        "session": dict(entry),
                    }
                )
        workspace.atomic(path, workspace.encoded(state))
    for event in events:
        workspace.record(root, event)
    return entry


def union(intervals):
    total, end = 0.0, None
    for a, b in sorted(intervals):
        if b < a:
            continue
        total += max(0, b - max(a, end if end is not None else a))
        end = max(b, end if end is not None else b)
    return total


def timeline(project, now=None):
    root = Path(project).resolve()
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    state = workspace.read_json(root / ".yapnr/workflow/state.json", {})
    history = state.get("history", [])
    stages = []
    for i, event in enumerate(history):
        start = timestamp(event["time"])
        end = timestamp(history[i + 1]["time"]) if i + 1 < len(history) else None
        stages.append(
            {
                "id": workspace.sha(workspace.encoded(event)),
                "label": event["to"],
                "start": start,
                "end": end,
                "revision": event["revision"],
                "receipt": event["receipt_sha256"],
                "source": "workflow history",
                "measure": "state occupancy; not CPU time",
            }
        )
    sessions = []
    for session in workspace.read_json(root / ".yapnr/workspace/presence.json", {"sessions": {}})[
        "sessions"
    ].values():
        start, last = timestamp(session["opened"]), timestamp(session["last_seen"])
        end = timestamp(session["closed"]) if session.get("closed") else None
        active = end is None and now - last < 60
        sessions.append(
            {
                **session,
                "start": start,
                "end": end,
                "last": last,
                "status": (
                    "closed" if end else "open" if active else "end unknown / connection lost"
                ),
            }
        )
    tasks, warnings = [], []
    seen = set()
    for path in sorted((root / ".yapnr/workspace/opencode").glob("*.json")):
        if path.stat().st_size > 16 * 1024 * 1024:
            warnings.append(
                "A large session snapshot was not aggregated; full evidence remains in the workspace."
            )
            continue
        try:
            snapshot = json.loads(path.read_bytes())
            for message in snapshot.get("messages", []):
                for part in message.get("parts", []):
                    if part.get("type") != "tool" or part["id"] in seen:
                        continue
                    seen.add(part["id"])
                    tool = part.get("state", {})
                    times = tool.get("time", {})
                    if not times.get("start"):
                        continue
                    tasks.append(
                        {
                            "id": part["id"],
                            "label": part.get("tool", "tool"),
                            "start": times["start"] / 1000,
                            "end": times.get("end", 0) / 1000 or None,
                            "status": tool.get("status", "unknown"),
                            "session": snapshot.get("info", {}).get("id"),
                            "source": "native runtime tool timestamps",
                            "count": 1,
                            "counter_scope": "whole tool occurrence",
                            "unit": "tool call",
                        }
                    )
        except (OSError, ValueError, TypeError, KeyError):
            warnings.append("An incomplete session snapshot remains unavailable for aggregation.")
    known = [
        (
            s["start"],
            s["end"] if s["end"] is not None else now if s["status"] == "open" else s["last"],
        )
        for s in sessions
    ]
    bounds = [
        n
        for row in stages + sessions + tasks
        for n in [row["start"], row.get("end")]
        if n is not None
    ]
    return {
        "schema": "yapnr-project-timing-v1",
        "now": now,
        "timezone": "UTC",
        "stages": stages,
        "sessions": sessions,
        "tasks": tasks,
        "warnings": sorted(set(warnings)),
        "range": [min(bounds, default=now), now],
        "project_open_seconds": union(known),
        "open_measure": "union of recorded open intervals; unknown ends counted only through last seen",
        "summed_task_seconds": sum(
            max(0, t["end"] - t["start"]) for t in tasks if t["end"] is not None
        ),
        "task_measure": "summed captured tool wall time; CPU time unavailable",
        "clock": "Presence/workflow: server UTC; native tool clock alignment not independently verified",
    }
