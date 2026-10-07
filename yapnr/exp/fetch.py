"""Fetch a campaign's results from its store and rebuild the layout the local tools read.

1. Mirror ``campaigns/<cid>/tasks/`` from the store into ``<dest>/<cid>/raw/`` (summaries, records
   and log tails; ``--full`` adds every ``result.tar.gz``). Egress costs money on Google Cloud, so
   full results are opt-in.
2. For each task with a ``_DONE`` marker, take the attempt it names into
   ``<dest>/<cid>/tasks/<task>/`` (``_DONE``, ``record.json``, ``log.tail``, ``summary/`` and,
   with ``--full``, ``result/``).
3. Call the kind's ``assemble`` into ``<dest>/<cid>/assembled/``: for the ladder, the run directory
   one ``run.py --out`` of all cells would have written, which ``tools/ci/ladder_summary.py``
   reads unchanged.

Private campaigns are fetched only below ``[local] private_results_root``, and never into a git
checkout whose remote is the public repository.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from yapnr.exp import bundle, kinds
from yapnr.exp.backends.base import campaign_prefix, done_markers, task_key
from yapnr.exp.config import Config
from yapnr.exp.store import Store

# `gcloud storage rsync --exclude` matches from the start of the relative path (re.match), so
# the pattern spans the directories before the name; LocalStore's re.search agrees.
NOT_FULL = r"(.*/)?result\.tar\.gz$"


class FetchError(RuntimeError):
    pass


def inside_public_checkout(path: Path, patterns: Sequence[str]) -> Optional[str]:
    """The public remote of the git checkout that contains ``path``, if any."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(probe), "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        for pattern in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                return line.split()[1] if len(line.split()) > 1 else line
    return None


def check_destination(meta: Dict[str, Any], dest: Path, config: Config) -> None:
    if meta.get("visibility") != "private":
        return
    root = config.local.store_path(private=True).resolve()
    target = Path(dest).resolve()
    if target != root and root not in target.parents:
        raise FetchError(
            "campaign %s is private: fetch it below [local] private_results_root (%s)"
            % (meta["id"], root)
        )
    remote = inside_public_checkout(target, config.local.public_remotes)
    if remote:
        raise FetchError("refusing to fetch a private campaign into a public checkout")


def _extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            bundle.unsafe_member(member)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="fully_trusted")
        else:
            tar.extractall(dest, members=members)


def load_campaign(runs: Store, cid: str):
    prefix = campaign_prefix(cid)
    try:
        meta = json.loads(runs.read_text("%s/campaign.json" % prefix))
        lines = runs.read_text("%s/tasks.jsonl" % prefix).splitlines()
    except Exception as err:
        raise FetchError("campaign %s is not in the store: %s" % (cid, err)) from err
    return meta, [json.loads(line) for line in lines if line.strip()]


def fetch(
    runs: Store,
    cid: str,
    dest: Path,
    config: Config,
    *,
    full: bool = False,
    allow_mixed: bool = False,
    assemble: bool = True,
) -> Dict[str, Any]:
    meta, tasks = load_campaign(runs, cid)
    check_destination(meta, dest, config)
    base = Path(dest) / cid
    raw = base / "raw"
    runs.sync_down("%s/tasks" % campaign_prefix(cid), raw, exclude=None if full else NOT_FULL)
    markers = done_markers(runs, cid)
    fetched = base / "tasks"
    report: Dict[str, Any] = {"campaign": cid, "tasks": len(tasks), "done": 0, "missing": []}
    for task in tasks:
        marker = markers.get(task["id"])
        if not marker:
            report["missing"].append(task["id"])
            continue
        key = task_key(task["id"])
        source = raw / key / marker["attempt"]
        target = fetched / key
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in ("record.json", "log.tail"):
            if (source / name).is_file():
                shutil.copyfile(source / name, target / name)
        for name in ("summary", "profiles"):
            if (source / name).is_dir():
                shutil.copytree(source / name, target / name)
        if full and (source / "result.tar.gz").is_file():
            _extract((source / "result.tar.gz").read_bytes(), target / "result")
        (target / "_DONE").write_text(json.dumps(marker, sort_keys=True) + "\n")
        report["done"] += 1
    if assemble and report["done"]:
        kind = kinds.get(meta["kind"])
        assembled = kind.assemble(meta, tasks, fetched, base / "assembled", allow_mixed)
        report["assembled"] = assembled
    base.mkdir(parents=True, exist_ok=True)
    (base / "fetch.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def records(fetched_campaign: Path) -> List[Dict[str, Any]]:
    """Every task record of a fetched campaign (``<dest>/<cid>``)."""
    out = []
    for path in sorted((Path(fetched_campaign) / "tasks").glob("*/record.json")):
        try:
            out.append(json.loads(path.read_text()))
        except ValueError:
            continue
    return out
