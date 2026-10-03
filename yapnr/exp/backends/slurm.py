"""Slurm with Apptainer: one job array per submission, for academic HPC allocations.

The plan renders, per resource class, an ``sbatch`` script and, for the whole campaign, a
``submit.sh`` and a ``status.sh`` that need only bash and the Slurm commands. The owner copies the
plan directory to the site's login node and runs ``submit.sh`` there (never an automated SSH
session); ``yapnr exp submit`` does the same where yapnr is installed on the login node.

- The image is a SIF built once on the login node from the campaign's pinned digest:
  ``apptainer pull <sif> docker://ghcr.io/studio-fug/yapnr@sha256:...``.
- Each array element runs ``chunk`` consecutive tasks of the submission in sequence, because
  ``MaxArraySize`` (1001 by default) and QOS limits cap array sizes.
- ``--requeue --signal=B:USR1@300``: the batch shell forwards USR1 to the container, waits, and
  requeues the element; finished tasks are skipped through their ``_DONE`` markers. An element
  whose wrapper exits 75 (a transient failure) is requeued too. Both requeues stop after
  ``limits.max_retries`` (``SLURM_RESTART_COUNT``), so an element that cannot finish within its
  time limit fails instead of burning the allocation in a loop.
- The store is a directory on the site's project or scratch file system with the bucket layout:
  ``bundles/``, ``campaigns/<cid>/...``, ``control/frozen``.

``#SBATCH`` lines are static; everything that depends on the site's paths (the store, the SIF,
``--output``) goes on the ``sbatch`` command line that ``submit.sh`` builds, where the shell
expands ``$SCRATCH`` and friends.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from yapnr.exp import image
from yapnr.exp import plan as planning
from yapnr.exp.backends.base import Backend, Stores, SubmitError
from yapnr.exp.config import Config, SlurmSite
from yapnr.exp.store import LocalStore

IMAGE_PYTHON = "/opt/venv/bin/python"
IMAGE_ENTRYPOINT = "/usr/local/bin/yapnr-kicad-env"
# Time for staging and upload per task, and for the element's start.
TASK_GRACE_S = 300
ELEMENT_GRACE_S = 600


def _hms(seconds: float) -> str:
    seconds = int(math.ceil(seconds))
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def element_seconds(cls: planning.ResourceClass, chunk: int) -> int:
    return chunk * (int(cls.max_wall_s) + TASK_GRACE_S) + ELEMENT_GRACE_S


def render_sbatch(
    plan_meta: Dict[str, Any],
    site: SlurmSite,
    cls: planning.ResourceClass,
    chunk: int,
    max_retries: int,
) -> str:
    """The batch script of one resource class (static: no site paths, no submission number)."""
    cid = plan_meta["id"]
    seconds = element_seconds(cls, chunk)
    if seconds > site.max_time_h * 3600:
        raise SubmitError(
            "class %s needs %s per array element (chunk %d), above the site's max_time_h %g"
            % (cls.name, _hms(seconds), chunk, site.max_time_h)
        )
    directives = ["--job-name=yapnr-%s-%s" % (cid, cls.name)]
    for key, value in (("account", site.account), ("partition", site.partition), ("qos", site.qos)):
        if value:
            directives.append("--%s=%s" % (key, value))
    directives += [
        "--cpus-per-task=%d" % cls.cpus,
        "--mem=%dM" % int(math.ceil(cls.memory_gb * 1024)),
        "--time=%s" % _hms(seconds),
        "--requeue",
        "--signal=B:USR1@300",
    ]
    directives += list(site.extra_sbatch)
    module = "module load %s 2>/dev/null || true" % shlex.quote(site.module) if site.module else ":"
    lines = ["#!/bin/bash", "# yapnr exp: campaign %s, class %s." % (cid, cls.name)]
    lines += ["#SBATCH %s" % d for d in directives]
    lines += [
        "#",
        "# submit.sh passes --array, --output and --export (YAPNR_STORE, YAPNR_SIF,",
        "# YAPNR_SIF_SHA256, YAPNR_SUBMISSION) on the sbatch command line.",
        "set -euo pipefail",
        module,
        ': "${YAPNR_STORE:?}" "${YAPNR_SIF:?}" "${YAPNR_SUBMISSION:?}"',
        'scratch="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/yapnr-${SLURM_JOB_ID}"',
        'mkdir -p "${scratch}"',
        'restarts="${SLURM_RESTART_COUNT:-0}"',
        "cmd=(%s exec --cleanenv --containall" % shlex.quote(site.apptainer),
        '  --bind "${YAPNR_STORE}:/store" --bind "${scratch}:/scratch"',
        '  --env "YAPNR_BACKEND=slurm,YAPNR_SIF_SHA256=${YAPNR_SIF_SHA256:-}"',
        '  "${YAPNR_SIF}" %s %s' % (IMAGE_ENTRYPOINT, IMAGE_PYTHON),
        "  /store/campaigns/%s/task.py --store /store --inputs /store/bundles" % cid,
        '  --campaign %s --submission "${YAPNR_SUBMISSION}"' % cid,
        '  --index "${SLURM_ARRAY_TASK_ID}" --chunk %d --retry "${restarts}"' % chunk,
        "  --toolchain image --work-root /scratch)",
        "# The time limit is near: stop the wrapper (it flushes checkpoints and exits 75), then",
        "# requeue a bounded number of times; finished tasks are skipped on the next run.",
        "requeue() {",
        '  kill -USR1 "${child}" 2>/dev/null || true',
        '  wait "${child}" || true',
        '  rm -rf "${scratch}"',
        '  if [ "${restarts}" -lt %d ]; then' % max_retries,
        '    scontrol requeue "${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"',
        "    exit 0",
        "  fi",
        '  echo "time limit reached after ${restarts} requeue(s); not requeued again" >&2',
        "  exit 75",
        "}",
        "trap requeue USR1",
        '"${cmd[@]}" &',
        "child=$!",
        "status=0",
        'wait "${child}" || status=$?',
        'rm -rf "${scratch}"',
        "# 75: a transient failure (staging, upload); requeue a bounded number of times.",
        'if [ "${status}" -eq 75 ] && [ "${restarts}" -lt %d ]; then' % max_retries,
        '  scontrol requeue "${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"',
        "  exit 0",
        "fi",
        'exit "${status}"',
    ]
    return "\n".join(lines) + "\n"


def render_submit(plan_meta: Dict[str, Any], site: SlurmSite, chunk: int) -> str:
    """``submit.sh``: stage the store, then one array per resource class (bash and Slurm only)."""
    cid = plan_meta["id"]
    ref = plan_meta["image"]["ref"]
    digest = (plan_meta["image"].get("digest") or "").replace("sha256:", "")
    sif = site.sif.replace("{digest12}", digest[:12])
    classes = [c["name"] for c in plan_meta["classes"]]
    lines = [
        "#!/bin/bash",
        "# yapnr exp: submit campaign %s on Slurm site %s." % (cid, site.name),
        "# Run on the login node from the plan directory copied there:",
        "#   bash backend/slurm/submit.sh",
        "# Needs bash, coreutils, Slurm (sbatch) and Apptainer. Running it again submits every",
        "# task of each class again; array elements skip finished tasks (their _DONE markers) and",
        "# exit in seconds. Run it again only after the earlier arrays ended (status.sh), or a",
        "# task still running there runs twice.",
        "set -euo pipefail",
        'plan="$(cd "$(dirname "$0")/../.." && pwd)"',
        'store="%s"' % site.store,
        'sif="%s"' % sif,
        "campaign=%s" % cid,
        'dir="${store}/campaigns/${campaign}"',
        'if [ -e "${store}/control/frozen" ]; then',
        '  echo "the store is frozen (control/frozen); not submitting" >&2',
        "  exit 3",
        "fi",
        'mkdir -p "${store}/bundles" "${dir}/submissions" "${dir}/slurm"',
        'for bundle in "${plan}"/bundles/*.tar.gz; do',
        '  [ -e "${bundle}" ] || continue',
        '  target="${store}/bundles/$(basename "${bundle}")"',
        '  [ -e "${target}" ] || cp "${bundle}" "${target}"',
        "done",
        "for name in campaign.json tasks.jsonl task.py; do",
        '  if [ -e "${dir}/${name}" ]; then',
        '    cmp -s "${plan}/${name}" "${dir}/${name}" || {',
        '      echo "${dir}/${name} differs from the plan" >&2',
        "      exit 2",
        "    }",
        "  else",
        '    cp "${plan}/${name}" "${dir}/${name}"',
        "  fi",
        "done",
        'if [ ! -e "${sif}" ]; then',
        '  echo "building ${sif} from %s" >&2' % ref,
        '  mkdir -p "$(dirname "${sif}")"',
        '  %s pull "${sif}" "docker://%s"' % (shlex.quote(site.apptainer), ref),
        "fi",
        'sif_sha256="$(sha256sum "${sif}" | cut -d" " -f1)"',
        "next=1",
        'for record in "${dir}"/submissions/*.indices; do',
        '  [ -e "${record}" ] || continue',
        '  n="$(basename "${record}" .indices)"',
        '  if [ "${n}" -ge "${next}" ]; then next=$((n + 1)); fi',
        "done",
        "classes=(%s)" % " ".join(classes),
        'for class in "${classes[@]}"; do',
        '  n="${next}"',
        "  next=$((next + 1))",
        '  cp "${plan}/backend/slurm/${class}.indices" "${dir}/submissions/${n}.indices"',
        '  count="$(wc -l < "${dir}/submissions/${n}.indices" | tr -d " ")"',
        "  elements=$(((count + %d - 1) / %d))" % (chunk, chunk),
        "  job=$(sbatch --parsable --array=0-$((elements - 1))%%%d \\" % site.max_concurrent,
        '    --output="${dir}/slurm/%A_%a.out" \\',
        '    --export="ALL,YAPNR_STORE=${store},YAPNR_SIF=${sif},'
        'YAPNR_SIF_SHA256=${sif_sha256},YAPNR_SUBMISSION=${n}" \\',
        '    "${plan}/backend/slurm/${class}.sbatch")',
        '  job="${job%%;*}"  # --parsable prints <job id>[;<cluster>]',
        '  cat > "${dir}/submissions/${n}.json" <<EOF',
        '{"schema": "yapnr-submission-v1", "campaign": "${campaign}", "submission": ${n},'
        ' "backend": "slurm", "class": "${class}", "tasks": ${count},'
        ' "job": {"id": "${job}", "site": "%s"}}' % site.name,
        "EOF",
        '  echo "submission ${n}: class ${class}, ${count} task(s), job ${job}"',
        "done",
    ]
    return "\n".join(lines) + "\n"


def render_status(plan_meta: Dict[str, Any], site: SlurmSite) -> str:
    cid = plan_meta["id"]
    return (
        "\n".join(
            [
                "#!/bin/bash",
                "# yapnr exp: status of campaign %s on Slurm site %s (bash and Slurm only)."
                % (
                    cid,
                    site.name,
                ),
                "set -euo pipefail",
                'store="%s"' % site.store,
                'dir="${store}/campaigns/%s"' % cid,
                'total="$(wc -l < "${dir}/tasks.jsonl" | tr -d " ")"',
                'done_count="$(find "${dir}/tasks" -name _DONE 2>/dev/null | wc -l | tr -d " ")"',
                'echo "campaign %s: ${done_count} of ${total} tasks done"' % cid,
                'for record in "${dir}"/submissions/*.json; do',
                '  [ -e "${record}" ] || continue',
                '  job="$(sed -n \'s/.*"id": "\\([0-9_]*\\)".*/\\1/p\' "${record}")"',
                '  [ -n "${job}" ] && sacct -X -n -j "${job}" --format=JobID,State,Elapsed || true',
                "done",
            ]
        )
        + "\n"
    )


class Slurm(Backend):
    name = "slurm"

    def site(self, plan, config: Config) -> SlurmSite:
        return config.require_site(plan.meta["backend"].get("site"))

    def stores(self, plan, config: Config, cloud=None) -> Stores:
        root = Path(os.path.expandvars(os.path.expanduser(self.site(plan, config).store)))
        store = LocalStore(root)
        return Stores(store, store)

    def render(self, plan, config, cls, submission, indices, deadline) -> Dict[str, str]:
        site = self.site(plan, config)
        chunk = int(plan.meta["backend"]["chunk"])
        files = {
            "%s.sbatch"
            % cls.name: render_sbatch(plan.meta, site, cls, chunk, config.limits.max_retries),
            "%s.indices" % cls.name: "".join("%d\n" % i for i in indices),
        }
        return files

    def preview(self, plan, config) -> List[Path]:
        written = super().preview(plan, config)
        site = self.site(plan, config)
        chunk = int(plan.meta["backend"]["chunk"])
        directory = plan.dir / "backend" / self.name
        for name, text in (
            ("submit.sh", render_submit(plan.meta, site, chunk)),
            ("status.sh", render_status(plan.meta, site)),
        ):
            path = directory / name
            path.write_text(text)
            path.chmod(0o755)
            written.append(path)
        return written

    def launch(self, plan, config, cls, submission, files, stores, cloud=None, dry_run=False):
        site = self.site(plan, config)
        chunk = int(plan.meta["backend"]["chunk"])
        indices = files["%s.indices" % cls.name].read_text().split()
        elements = max(1, math.ceil(len(indices) / chunk))
        store = str(stores.runs.root)
        ref = image.parse(plan.meta["image"]["ref"])
        sif = os.path.expandvars(
            os.path.expanduser(site.sif.replace("{digest12}", (ref.digest or "")[7:19]))
        )
        argv = [
            "sbatch",
            "--parsable",
            "--array=0-%d%%%d" % (elements - 1, site.max_concurrent),
            "--output=%s/campaigns/%s/slurm/%%A_%%a.out" % (store, plan.id),
            "--export=ALL,YAPNR_STORE=%s,YAPNR_SIF=%s,YAPNR_SUBMISSION=%d"
            % (store, sif, submission),
            str(files["%s.sbatch" % cls.name]),
        ]
        if dry_run:
            return {"id": None, "summary": "dry run: " + shlex.join(argv), "argv": argv}
        if shutil.which("sbatch") is None:
            raise SubmitError("sbatch is not on PATH: run this on the site's login node")
        Path(store, "campaigns", plan.id, "slurm").mkdir(parents=True, exist_ok=True)
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise SubmitError("sbatch failed: %s" % done.stderr.strip()[:300])
        job = done.stdout.strip().split(";")[0]
        return {"id": job, "site": site.name, "summary": "Slurm job %s on %s" % (job, site.name)}

    def cancel(self, record, config, cloud=None, dry_run=False) -> str:
        job = (record.get("job") or {}).get("id")
        if not job:
            return "submission %s has no job" % record.get("submission")
        if dry_run or shutil.which("scancel") is None:
            return "run on the login node: scancel %s" % job
        subprocess.run(["scancel", str(job)], check=True, timeout=60)
        return "cancelled Slurm job %s" % job

    def state(self, plan_meta, record, config, cloud=None):
        job = (record.get("job") or {}).get("id")
        if not job or shutil.which("sacct") is None:
            return None
        done = subprocess.run(
            ["sacct", "-X", "-n", "-P", "-j", str(job), "--format=JobID,State"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return array_view(done.stdout if done.returncode == 0 else None)


# sacct states of an array element that may still run (sacct(1), "JOB STATE CODES").
LIVE_STATES = frozenset(
    (
        "PENDING",
        "CONFIGURING",
        "RUNNING",
        "COMPLETING",
        "REQUEUED",
        "REQUEUE_FED",
        "REQUEUE_HOLD",
        "RESIZING",
        "SIGNALING",
        "STAGE_OUT",
        "STOPPED",
        "SUSPENDED",
    )
)


def array_view(sacct_output: Any) -> Dict[str, Any]:
    """The view of one array from ``sacct -X -n -P --format=JobID,State``: RUNNING while any
    element may still run, FINISHED when none can, UNKNOWN when sacct gave nothing."""
    counts: Dict[str, int] = {}
    for line in (sacct_output or "").splitlines():
        parts = line.split("|")
        if len(parts) == 2:
            state = parts[1].split()[0] if parts[1].strip() else "UNKNOWN"
            counts[state] = counts.get(state, 0) + 1
    if not counts:
        state = "UNKNOWN"
    elif LIVE_STATES & set(counts):
        state = "RUNNING"
    else:
        state = "FINISHED"
    return {"state": state, "counts": counts, "preemptions": counts.get("PREEMPTED", 0)}
