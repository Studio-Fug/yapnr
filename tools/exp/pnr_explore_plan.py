#!/usr/bin/env python3
"""Turn a list of placement seeds and/or candidate placements into an ``mc-eval`` campaign: one
PnR exploration task each, against any board example's ``integrate.py``-shaped driver (not
radar60-specific; the driver, its steps and a board's own ``work`` layout all come from the
jobs file).

    python3 tools/exp/pnr_explore_plan.py JOBS.toml --repo CHECKOUT [--commit REV] --out DIR

``JOBS.toml`` (or ``.json``)::

    schema = "yapnr-pnr-explore-v1"
    name = "radar60-explore"               # the campaign name
    driver = "examples/radar60/board/integrate.py"   # relative to the repo
    paths = ["hardware", "yapnr", "examples/radar60", "tools/exp"]  # the source bundle's paths
    image = "edge"                          # optional; visibility, determinism too

    [defaults]                              # every seed/candidate's defaults
    cpus = 1
    memory_gb = 4
    disk_gb = 6
    max_wall_s = 1800
    work_dir = "work"                       # the driver's --work
    seed_from = "prepared"                  # optional: a prepared snapshot (relative to the
                                             # jobs file) copied into work_dir before step 1 --
                                             # e.g. a "source"+"prepare" already run once, for a
                                             # driver whose first step needs network access or
                                             # an input a Batch VM cannot fetch
    common_args = ["--work", "work", "--out", "out/board", "--engine", "src",
                   "--python", "${PYTHON}", "--kicad-python", "${KICAD_PYTHON}",
                   "--kicad-cli", "${KICAD_CLI}"]
    post_steps = [["route"], ["check"]]     # steps after place/select/finish or finish
    collect = ["work/route/candidate.kicad_pcb", "out/board/measure.json"]
    embed = [["measure", "out/board/measure.json"]]   # record["data"]["measure"] = that JSON

    [[seeds]]                               # one task per seed: place --seed N, select, finish
    id = "s0"
    place_args = ["--seed", "0", "--n0", "1", "--procs", "1"]
    select_args = ["--top-n", "1"]

    [[candidates]]                          # one task per candidate: finish --placement PATH
    id = "cand-rank0"
    placement = "reva/candidates/rank0/placement.json"   # relative to the jobs file

    [live]                                  # optional: the live viewer mirror (yapnr.exp.spec
    enabled = true                          # live_errors), copied into campaign.toml verbatim
    interval_s = 45
    mode = "thin"

Every seed task runs ``pre_steps(if any) + [["place"] + place_args, ["select"] + select_args,
["finish"]] + post_steps``; every candidate task runs ``pre_steps + [["finish", "--placement",
"candidate.json"]] + post_steps`` (the candidate file is bundled as its own input). Each line's
command is ``${PYTHON} job/pnr_explore_job.py`` (tools/exp/pnr_explore_job.py, run from the job
bundle), which chains the driver's steps and writes ``out/<id>/result.json`` -- its ``data``
holds whatever ``embed`` names (a board's ``check`` step report, say), and ``out/<id>/files/``
holds whatever ``collect`` globbed (the routed board, the measure/check report file itself).
``yapnr exp fetch``'s ``mc-eval`` assembly then reads every task's ``result.json`` straight into
``assembled/dataset.jsonl`` (one line per task, with its labels and record) -- the generic part
of ranking; a board's own ranking table (DRC, opens by class, IR per rail...) reads that file and
knows its own record shape, e.g. examples/radar60/board/explore_rank.py.

Written as one source bundle of the committed revision (``git archive``, never the working
tree, like tools/exp/rf_stage_plan.py), so a branch another session is still editing plans as it
was last committed; the manifest's ``uncommitted_changes_left_out`` says whether ``paths`` had
any. ``seed_from`` is copied from disk as it stands (never through git), since it is usually a
local "prepare once" run's output, not checked in.

``yapnr exp plan``'s own campaign id is a hash of the campaign *file* (name, kind, image,
``config.stage_plan``'s path string) and the resolved commit, never of ``stage.jsonl``'s own
content -- so re-running this generator against the same jobs file and commit after a job-bundle
or ``pnr_explore_job.py`` fix reuses the earlier plan directory, whose tasks already carry
``_DONE`` markers (a fail is still a result) and so will not be resubmitted. Change ``name`` (or
pass a different commit) to force a fresh id when the fix needs the same tasks to actually run
again.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    import tomllib
except ImportError:  # Python < 3.11: JSON job files only
    tomllib = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
RUNNER = "pnr_explore_job.py"
SCHEMA = "yapnr-pnr-explore-v1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
GIT_TIMEOUT_S = 120
DEFAULT_RESOURCES = {"cpus": 1, "memory_gb": 4, "disk_gb": 6, "max_wall_s": 1800}
RESOURCE_KEYS = tuple(DEFAULT_RESOURCES)
TOP_KEYS = {
    "schema",
    "name",
    "driver",
    "paths",
    "image",
    "visibility",
    "determinism",
    "defaults",
    "seeds",
    "candidates",
    "live",
}
LIVE_KEYS = {"enabled", "interval_s", "mode"}
LIVE_MODES = {"thin", "full"}
DEFAULT_KEYS = {
    "work_dir",
    "seed_from",
    "common_args",
    "pre_steps",
    "post_steps",
    "collect",
    "embed",
} | set(RESOURCE_KEYS)
ENTRY_KEYS = {"id", "place_args", "select_args", "placement"} | set(RESOURCE_KEYS)


class JobError(ValueError):
    pass


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=GIT_TIMEOUT_S
    ).stdout


def load_jobs(path: Path) -> Dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    if tomllib is None:
        raise JobError("TOML job files need Python 3.11 or later")
    return tomllib.loads(path.read_text())


def source_bundle(repo: Path, commit: str, paths: List[str], dest: Path) -> Dict[str, Any]:
    """``git archive`` of ``commit``'s ``paths``, gzipped without a timestamp (reproducible)."""
    raw = git(repo, "archive", "--format=tar", commit, "--", *paths)
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(raw)
    data = out.getvalue()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": dest.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def check_jobs(doc: Mapping[str, Any]) -> List[str]:
    errors = ["unknown key %r" % k for k in sorted(set(doc) - TOP_KEYS)]
    if doc.get("schema") != SCHEMA:
        errors.append("schema must be %r" % SCHEMA)
    if not isinstance(doc.get("name"), str) or not NAME_RE.match(doc["name"]):
        errors.append("name is lower-case letters, digits and dashes, at most 41 characters")
    if not isinstance(doc.get("driver"), str):
        errors.append("driver names the step script (relative to the repo)")
    paths = doc.get("paths")
    if not (isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths)):
        errors.append("paths is a non-empty list of repo-relative paths to bundle")
    defaults = doc.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("defaults is a table")
    else:
        errors += ["defaults: unknown key %r" % k for k in sorted(set(defaults) - DEFAULT_KEYS)]
        for key in ("common_args", "pre_steps", "post_steps", "collect"):
            value = defaults.get(key, [])
            if not isinstance(value, list):
                errors.append("defaults.%s is a list" % key)
        for key in ("pre_steps", "post_steps"):
            for step in defaults.get(key, []):
                if not (isinstance(step, list) and step and all(isinstance(x, str) for x in step)):
                    errors.append("defaults.%s has a non-string step %r" % (key, step))
        for item in defaults.get("embed", []):
            if not (
                isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item)
            ):
                errors.append("defaults.embed entries are [key, path] pairs")
    seeds = doc.get("seeds", [])
    candidates = doc.get("candidates", [])
    if not seeds and not candidates:
        errors.append("seeds or candidates must name at least one task")
    seen = set()
    for where, entry in [("seeds[%d]" % i, e) for i, e in enumerate(seeds)] + [
        ("candidates[%d]" % i, e) for i, e in enumerate(candidates)
    ]:
        if not isinstance(entry, dict):
            errors.append("%s is a table" % where)
            continue
        errors += ["%s: unknown key %r" % (where, k) for k in sorted(set(entry) - ENTRY_KEYS)]
        if not isinstance(entry.get("id"), str) or not ID_RE.match(entry["id"]):
            errors.append("%s: id is [a-z0-9._-], at most 63 characters" % where)
        elif entry["id"] in seen:
            errors.append("%s: id %r is repeated" % (where, entry["id"]))
        else:
            seen.add(entry["id"])
    for i, entry in enumerate(candidates):
        if isinstance(entry, dict) and not isinstance(entry.get("placement"), str):
            errors.append("candidates[%d]: placement names a placement.json file" % i)
    if "live" in doc:
        live = doc["live"]
        if not isinstance(live, dict):
            errors.append("live is a table {enabled, interval_s, mode}")
        else:
            errors += ["live: unknown key %r" % k for k in sorted(set(live) - LIVE_KEYS)]
            if "enabled" in live and not isinstance(live["enabled"], bool):
                errors.append("live.enabled is a boolean")
            if "interval_s" in live:
                v = live["interval_s"]
                if isinstance(v, bool) or not isinstance(v, int) or v < 10:
                    errors.append("live.interval_s is a whole number of seconds, at least 10")
            if "mode" in live and live["mode"] not in LIVE_MODES:
                errors.append("live.mode must be one of %s" % ", ".join(sorted(LIVE_MODES)))
    return errors


def _json_list(*groups: List[Any]) -> str:
    merged: List[str] = []
    for group in groups:
        merged += group
    return json.dumps(merged)


def stage_line(
    entry: Mapping[str, Any],
    defaults: Mapping[str, Any],
    *,
    src: str,
    is_candidate: bool,
    job_dir_input: Mapping[str, str],
    candidate_input: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    jid = entry["id"]
    out = "out/" + jid
    work_dir = defaults.get("work_dir", "work")
    common = list(defaults.get("common_args", []))
    pre = [list(s) for s in defaults.get("pre_steps", [])]
    post = [list(s) for s in defaults.get("post_steps", [])]
    if is_candidate:
        steps = pre + [["finish", "--placement", "candidate/placement.json"]] + post
    else:
        steps = (
            pre
            + [
                ["place"] + list(entry.get("place_args", [])),
                ["select"] + list(entry.get("select_args", ["--top-n", "1"])),
                ["finish"],
            ]
            + post
        )
    inputs = [{"dest": "src", "path": src}, job_dir_input]
    argv = [
        "${PYTHON}",
        "job/" + RUNNER,
        "--driver",
        "src/" + defaults["driver"],
        "--python",
        "${PYTHON}",
        "--work-dir",
        work_dir,
        "--out",
        out,
        "--record",
        "result.json",
    ]
    if defaults.get("seed_from"):
        argv += ["--seed-from", "work0"]
        inputs.append({"dest": "work0", "path": defaults["seed_from_bundle"]})
    if is_candidate:
        inputs.append(dict(candidate_input, dest="candidate"))
    if common:
        argv += ["--common", _json_list(common)]
    for step in steps:
        argv += ["--step", _json_list(step)]
    for pattern in defaults.get("collect", []):
        argv += ["--collect", pattern]
    for key, path in defaults.get("embed", []):
        argv += ["--embed", "%s=%s" % (key, path)]
    resources = {k: entry.get(k, defaults.get(k, DEFAULT_RESOURCES[k])) for k in RESOURCE_KEYS}
    labels = {"task": jid}
    if is_candidate:
        labels["kind"] = "candidate"
    else:
        labels["kind"] = "seed"
    record = out + "/result.json"
    return {
        "id": jid,
        "command": argv,
        "inputs": inputs,
        "resources": resources,
        "record": record,
        "verdict": {"file": record, "json_path": "ok"},
        "labels": labels,
    }


def campaign_toml(doc: Mapping[str, Any]) -> str:
    lines = [
        "# Written by tools/exp/pnr_explore_plan.py; plan it with `yapnr exp plan`.",
        'schema = "yapnr-campaign-v1"',
        'kind = "mc-eval"',
        "name = %s" % json.dumps(doc["name"]),
        'source = "none"  # the driver and runner come from the stage plan\'s own bundle',
        "image = %s" % json.dumps(doc.get("image", "edge")),
        "visibility = %s" % json.dumps(doc.get("visibility", "public")),
        "determinism = %s" % json.dumps(doc.get("determinism", "seeded")),
        "",
        "[config]",
        'stage_plan = "stage.jsonl"',
    ]
    live = doc.get("live")
    if live:
        lines += ["", "[live]"]
        if "enabled" in live:
            lines.append("enabled = %s" % json.dumps(live["enabled"]))
        if "interval_s" in live:
            lines.append("interval_s = %d" % live["interval_s"])
        if "mode" in live:
            lines.append("mode = %s" % json.dumps(live["mode"]))
    return "\n".join(lines) + "\n"


def generate(jobs_path: Path, repo: Path, commit: str, out: Path) -> Dict[str, Any]:
    doc = load_jobs(jobs_path)
    errors = check_jobs(doc)
    if errors:
        raise JobError("; ".join(errors))
    full = git(repo, "rev-parse", "--verify", "%s^{commit}" % commit).decode().strip()
    out.mkdir(parents=True, exist_ok=True)
    bundle = source_bundle(
        repo, full, list(doc["paths"]), out / "bundles" / ("src-%s.tar.gz" % full[:12])
    )
    src = "bundles/" + bundle["path"]

    job_dir = out / "job"
    shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True)
    shutil.copyfile(HERE / RUNNER, job_dir / RUNNER)

    defaults = dict(doc.get("defaults", {}))
    defaults["driver"] = doc["driver"]
    seed_from_meta = None
    if defaults.get("seed_from"):
        snapshot = (jobs_path.parent / defaults["seed_from"]).resolve()
        if not snapshot.is_dir():
            raise JobError("seed_from %s is not a directory" % snapshot)
        snapshot_copy = out / "seed_from"
        shutil.rmtree(snapshot_copy, ignore_errors=True)
        shutil.copytree(snapshot, snapshot_copy)
        seed_from_meta = {"path": "seed_from", "source": str(snapshot)}
        defaults["seed_from_bundle"] = "seed_from"

    lines = []
    for entry in doc.get("seeds", []):
        lines.append(
            stage_line(
                entry,
                defaults,
                src=src,
                is_candidate=False,
                job_dir_input={"dest": "job", "path": "job"},
            )
        )
    for entry in doc.get("candidates", []):
        placement = (jobs_path.parent / entry["placement"]).resolve()
        if not placement.is_file():
            raise JobError("candidate %s: %s does not exist" % (entry["id"], placement))
        # bundle.data_bundle only takes a directory or a .tar.gz, never a bare file.
        cand_dir = out / "candidates" / entry["id"]
        shutil.rmtree(cand_dir, ignore_errors=True)
        cand_dir.mkdir(parents=True)
        shutil.copyfile(placement, cand_dir / "placement.json")
        lines.append(
            stage_line(
                entry,
                defaults,
                src=src,
                is_candidate=True,
                job_dir_input={"dest": "job", "path": "job"},
                candidate_input={"path": "candidates/" + entry["id"]},
            )
        )
    (out / "stage.jsonl").write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
    (out / "campaign.toml").write_text(campaign_toml(doc))
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no", "--", *doc["paths"]).decode()
    manifest = {
        "schema": "yapnr-pnr-explore-plan-v1",
        "commit": full,
        "paths": list(doc["paths"]),
        "source_bundle": bundle,
        "uncommitted_changes_left_out": bool(dirty.strip()),
        "seed_from": seed_from_meta,
        "tasks": len(lines),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("jobs", type=Path, help="the jobs file (TOML or JSON)")
    ap.add_argument("--repo", type=Path, default=Path("."), help="checkout holding the driver")
    ap.add_argument("--commit", default="HEAD", help="the committed revision to archive")
    ap.add_argument("--out", type=Path, required=True, help="the campaign directory to write")
    args = ap.parse_args(argv)
    try:
        manifest = generate(args.jobs, args.repo, args.commit, args.out)
    except (JobError, OSError, subprocess.SubprocessError) as err:
        print("pnr_explore_plan: %s" % err, file=sys.stderr)
        return 2
    print(
        "%d tasks from %s (%s) -> %s"
        % (
            manifest["tasks"],
            manifest["commit"][:12],
            "uncommitted changes left out" if manifest["uncommitted_changes_left_out"] else "clean",
            args.out / "campaign.toml",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
