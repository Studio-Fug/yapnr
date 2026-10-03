"""What the three backends share: the stores, the submit sequence, status and the markers.

``submit`` is the same everywhere:

1. check the plan's integrity (task hashes, wrapper, bundles);
2. refuse when the store's ``control/frozen`` marker exists (the budget kill switch);
3. upload the campaign files (``campaign.json``, ``tasks.jsonl``, ``task.py``), refusing to
   overwrite different ones, and the bundles that are not in the inputs store yet;
4. list the pending tasks (no ``_DONE``), per resource class;
5. estimate them, apply the per-submit caps, and ask for confirmation above ``confirm_usd``;
6. per class: take the next submission number, write ``submissions/<n>.indices``, render the
   backend's artefacts, launch them and write ``submissions/<n>.json``.

A submission is one resource class on one backend, region and shape: one Batch job, one Slurm
array or one local pool entry.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from yapnr.exp import cost
from yapnr.exp import plan as planning
from yapnr.exp.config import Config
from yapnr.exp.store import FROZEN, Store

SUBMISSION_SCHEMA = "yapnr-submission-v1"
CAMPAIGN_FILES = ("campaign.json", "tasks.jsonl", "task.py")


class SubmitError(RuntimeError):
    pass


@dataclass
class Stores:
    runs: Store
    inputs: Store


@dataclass
class Submission:
    number: int
    cls: str
    indices: List[int]
    record: Dict[str, Any]


def campaign_prefix(cid: str) -> str:
    return "campaigns/%s" % cid


def task_key(task_id: str) -> str:
    return task_id.replace("/", "~")


def done_markers(runs: Store, cid: str) -> Dict[str, Dict[str, Any]]:
    """``_DONE`` markers of a campaign by task id (each names its attempt and verdict)."""
    out = {}
    for line in runs.read_lines("%s/tasks/*/_DONE" % campaign_prefix(cid)):
        line = line.strip()
        if not line:
            continue
        try:
            marker = json.loads(line)
        except ValueError:
            continue
        if isinstance(marker, dict) and marker.get("task"):
            out[marker["task"]] = marker
    return out


def submissions(runs: Store, cid: str) -> List[Dict[str, Any]]:
    records = []
    for rel in runs.list("%s/submissions/*.json" % campaign_prefix(cid)):
        if rel.endswith(".job.json"):
            continue
        try:
            records.append(json.loads(runs.read_text(rel)))
        except ValueError:
            continue
    return sorted(records, key=lambda r: r.get("submission", 0))


def local_records(plan: planning.Plan) -> List[Dict[str, Any]]:
    """The submission records the plan directory kept (``submissions/<n>/record.json``)."""
    records = []
    for path in sorted((plan.dir / "submissions").glob("*/record.json")):
        try:
            records.append(json.loads(path.read_text()))
        except ValueError:
            continue
    return sorted(records, key=lambda r: r.get("submission", 0))


def next_submission(runs: Store, cid: str) -> int:
    numbers = [r.get("submission", 0) for r in submissions(runs, cid)]
    taken = set()
    for rel in runs.list("%s/submissions/*.indices" % campaign_prefix(cid)):
        stem = Path(rel).name.split(".", 1)[0]
        if stem.isdigit():
            taken.add(int(stem))
    return max(numbers + list(taken) + [0]) + 1


def upload_campaign(plan: planning.Plan, stores: Stores) -> List[str]:
    """Copy the campaign files and missing bundles; returns what was uploaded."""
    uploaded = []
    prefix = campaign_prefix(plan.id)
    for name in CAMPAIGN_FILES:
        rel = "%s/%s" % (prefix, name)
        local = plan.dir / name
        if stores.runs.exists(rel):
            if stores.runs.read_bytes(rel) != local.read_bytes():
                raise SubmitError(
                    "%s in the store differs from the plan; the plan was changed after a submit"
                    % rel
                )
            continue
        stores.runs.upload(local, rel)
        uploaded.append(rel)
    for digest in plan.meta["bundles"]:
        rel = "bundles/%s.tar.gz" % digest
        if not stores.inputs.exists(rel):
            stores.inputs.upload(plan.bundle_path(digest), rel, no_clobber=True)
            uploaded.append(rel)
    return uploaded


def pending(
    plan: planning.Plan, done: Dict[str, Any], only: Optional[Sequence[str]] = None
) -> Dict[str, List[int]]:
    """Lines of tasks without ``_DONE``, per class (restricted to ``only`` task ids if given)."""
    wanted = set(only or [])
    unknown = wanted - {t["id"] for t in plan.tasks}
    if unknown:
        raise SubmitError("unknown task ids: %s" % ", ".join(sorted(unknown)))
    out: Dict[str, List[int]] = {}
    for cls in plan.classes:
        lines = [
            i
            for i in cls.lines
            if plan.tasks[i]["id"] not in done and (not wanted or plan.tasks[i]["id"] in wanted)
        ]
        if lines:
            out[cls.name] = lines
    return out


def confirm_tty(prompt: str) -> bool:
    import sys

    if not sys.stdin.isatty():
        return False
    return input(prompt).strip().lower() in ("y", "yes")


class Backend:
    """One backend; subclasses implement the hooks below."""

    name = ""

    def stores(self, plan: planning.Plan, config: Config, cloud=None) -> Stores:
        raise NotImplementedError

    def render(
        self,
        plan: planning.Plan,
        config: Config,
        cls: planning.ResourceClass,
        submission: int,
        indices: Sequence[int],
        deadline: int,
    ) -> Dict[str, str]:
        """The artefacts of one submission, by file name."""
        raise NotImplementedError

    def launch(
        self,
        plan: planning.Plan,
        config: Config,
        cls: planning.ResourceClass,
        submission: int,
        files: Dict[str, Path],
        stores: Stores,
        cloud=None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Start the rendered submission; returns what identifies it (job name, pid...)."""
        raise NotImplementedError

    def state(self, plan_meta: Dict[str, Any], record: Dict[str, Any], config: Config, cloud=None):
        """The backend's view of one submission: {'state': ..., 'counts': {...}} or None."""
        return None

    def cancel(self, record: Dict[str, Any], config: Config, cloud=None, dry_run=False) -> str:
        raise NotImplementedError

    def preview(self, plan: planning.Plan, config: Config) -> List[Path]:
        """Render submission 1 of every class into the plan directory, for review."""
        deadline = int(time.time() + config.limits.max_campaign_hours * 3600)
        written = []
        directory = plan.dir / "backend" / self.name
        for number, cls in enumerate(plan.classes, 1):
            for name, text in self.render(plan, config, cls, number, cls.lines, deadline).items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                if name.endswith(".sh"):
                    path.chmod(0o755)
                written.append(path)
        return written

    def submit(
        self,
        plan: planning.Plan,
        config: Config,
        *,
        yes: bool = False,
        max_usd: Optional[float] = None,
        dry_run: bool = False,
        only: Optional[Sequence[str]] = None,
        cloud=None,
        confirm: Callable[[str], bool] = confirm_tty,
        now: Callable[[], float] = time.time,
        say: Callable[[str], None] = print,
        price_table: Optional[cost.PriceTable] = None,
    ) -> List[Submission]:
        if plan.backend != self.name:
            raise SubmitError("the plan is for the %s backend" % plan.backend)
        errors = plan.check()
        if errors:
            raise SubmitError("the plan is damaged: " + "; ".join(errors))
        stores = self.stores(plan, config, cloud)
        if stores.runs.exists(FROZEN):
            raise SubmitError(
                "the store is frozen (%s exists: the budget kill switch fired); the owner clears "
                "it with `yapnr exp unfreeze`" % FROZEN
            )
        done = done_markers(stores.runs, plan.id)
        todo = pending(plan, done, only)
        if not todo:
            say("campaign %s: every task is done; nothing to submit" % plan.id)
            return []
        self.check_limits(
            plan,
            config,
            todo,
            yes=yes,
            max_usd=max_usd,
            confirm=confirm,
            say=say,
            price_table=price_table,
        )
        uploaded = upload_campaign(plan, stores)
        if uploaded:
            say("uploaded %d file(s) to the stores" % len(uploaded))
        deadline = int(now() + config.limits.max_campaign_hours * 3600)
        out = []
        prefix = campaign_prefix(plan.id)
        for cls in plan.classes:
            lines = todo.get(cls.name)
            if not lines:
                continue
            number = next_submission(stores.runs, plan.id)
            stores.runs.write_text(
                "%s/submissions/%d.indices" % (prefix, number),
                "".join("%d\n" % i for i in lines),
            )
            rendered = self.render(plan, config, cls, number, lines, deadline)
            directory = plan.dir / "submissions" / str(number)
            directory.mkdir(parents=True, exist_ok=True)
            files = {}
            for name, text in rendered.items():
                path = directory / name
                path.write_text(text)
                if name.endswith(".sh"):
                    path.chmod(0o755)
                files[name] = path
            job = self.launch(plan, config, cls, number, files, stores, cloud, dry_run)
            record = {
                "schema": SUBMISSION_SCHEMA,
                "campaign": plan.id,
                "submission": number,
                "backend": self.name,
                "class": cls.name,
                "tasks": len(lines),
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
                "deadline": deadline,
                "dry_run": dry_run,
                "job": job,
            }
            if plan.backend == "gcp-batch":
                placement = plan.placement(cls.name)
                record["placement"] = {
                    "region": placement.region,
                    "shape": placement.shape,
                    "model": placement.model,
                    "tasks_per_vm": placement.tasks_per_vm,
                }
            text = json.dumps(record, indent=2, sort_keys=True) + "\n"
            (directory / "record.json").write_text(text)  # the plan keeps a copy (dry runs too)
            stores.runs.write_text("%s/submissions/%d.json" % (prefix, number), text)
            say(
                "submission %d: class %s, %d task(s) -> %s"
                % (number, cls.name, len(lines), job.get("summary", job.get("name", "")))
            )
            out.append(Submission(number, cls.name, lines, record))
        return out

    def check_limits(self, plan, config, todo, *, yes, max_usd, confirm, say, price_table=None):
        """Per-submit caps and confirmation; only the money-spending backend has any."""
        count = sum(len(v) for v in todo.values())
        if count > config.limits.max_tasks:
            raise SubmitError(
                "%d tasks, above limits.max_tasks %d" % (count, config.limits.max_tasks)
            )


def status_rows(plan_meta: Dict[str, Any], tasks: List[Dict[str, Any]], done: Dict[str, Any]):
    """Counts of done tasks by verdict, and the pending ones."""
    verdicts: Dict[str, int] = {}
    for task in tasks:
        marker = done.get(task["id"])
        if marker:
            verdicts[marker.get("verdict", "done")] = (
                verdicts.get(marker.get("verdict", "done"), 0) + 1
            )
    return {
        "tasks": len(tasks),
        "done": sum(verdicts.values()),
        "verdicts": verdicts,
        "pending": len(tasks) - sum(verdicts.values()),
    }
