"""Test helpers for ``yapnr.exp``: configs, fixture repositories, hand-made campaigns, goldens.

Everything here is offline and free: configs use documentation values (``example-project``),
fixture repositories are made with fixed dates so their commits hash the same everywhere, and
golden comparisons normalize what differs between machines (temporary directories) and what the
privacy scan must never see in a committed file (service-account addresses).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from yapnr.exp import config as config_mod
from yapnr.exp import spec
from yapnr.exp.cloud import NO_CLOUD_ENV

# No test may ever reach a real cloud: real gcloud calls fail while this is set, and test
# configs name a gcloud that does not exist.
os.environ[NO_CLOUD_ENV] = "1"

DIGEST = "sha256:" + "ab" * 32
TODAY = _dt.date(2026, 10, 2)
DEADLINE = 1790000000
UPDATE_ENV = "YAPNR_UPDATE_GOLDEN"
CID = "20261002-smoke-000001"

# The two-region setup: C4D in us-west4, C4 in northamerica-northeast1 (which has no C4D), and
# the instance templates infra/gcp makes for it (region_template_shapes).
RANKING = [("c4d", "us-west4"), ("c4", "northamerica-northeast1"), ("c4", "us-west4")]
RANKED_FAMILIES = '[placement]\nfamilies = ["c4d", "c4"]\n'
TEMPLATES = {
    "yapnr-c4d-highcpu-16-spot-us-west4": "c4d-highcpu-16",
    "yapnr-c4d-standard-16-spot-us-west4": "c4d-standard-16",
    "yapnr-c4d-highcpu-8-spot-us-west4": "c4d-highcpu-8",
    "yapnr-c4-highcpu-16-spot-northamerica-northeast1": "c4-highcpu-16",
    "yapnr-c4-highcpu-8-spot-northamerica-northeast1": "c4-highcpu-8",
}

GCP_TOML = """
[gcp]
project = "example-project"
home_region = "us-west4"
regions = ["us-west4", "northamerica-northeast1"]
inputs_bucket = "example-yapnr-inputs"
runs_bucket = "example-yapnr-runs"
gcloud = "/nonexistent/gcloud-for-tests"
"""


def write_config(tmp: Path, extra: str = "", gcp: bool = True, slurm: bool = False) -> Path:
    """An owner config below ``tmp`` (stores in ``tmp``); returns its path."""
    text = GCP_TOML if gcp else ""
    text += """
[local]
store = "%s"
private_results_root = "%s"
workers = 2
nice = 0
python = "%s"
""" % (
        tmp / "store",
        tmp / "private",
        sys.executable,
    )
    if slurm:
        text += """
[slurm.example-site]
account = "example-account"
partition = "cpu"
store = "$SCRATCH/yapnr-store"
sif = "$HOME/yapnr/yapnr-{digest12}.sif"
max_array = 4
max_concurrent = 16
chunk = 2
"""
    text += extra
    path = Path(tmp) / "cloud.toml"
    path.write_text(text)
    return path


def load_config(tmp: Path, extra: str = "", gcp: bool = True, slurm: bool = False):
    return config_mod.load(str(write_config(tmp, extra, gcp, slurm)))


def git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.com",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.com",
        GIT_AUTHOR_DATE="2026-10-02T00:00:00Z",
        GIT_COMMITTER_DATE="2026-10-02T00:00:00Z",
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(repo),
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def fixture_repo(root: Path) -> Path:
    """A git repository with the paths the ladder kind bundles; the same commit every time."""
    repo = Path(root) / "repo"
    (repo / "hardware" / "pnr" / "regression").mkdir(parents=True)
    (repo / "hardware" / "tools").mkdir(parents=True)
    (repo / "hardware" / "pnr" / "regression" / "run.py").write_text("print('fixture')\n")
    (repo / "hardware" / "tools" / "tool.py").write_text("# fixture\n")
    (repo / "README.md").write_text("fixture\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def write_campaign(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


SMOKE_CAMPAIGN = """
schema = "yapnr-campaign-v1"
kind = "smoke"
name = "smoke"
source = "none"
image = "edge"

[config]
count = 2
sleep_s = 0
"""

LADDER_CAMPAIGN = """
schema = "yapnr-campaign-v1"
kind = "ladder-cell"
name = "ladder-small"
source = "HEAD"
image = "edge"

[matrix]
case = ["01-connector-led-2", "02-resistor-led-3"]
seed = [0, 1]

[config]
timeout = 600

[resources]
cpus = 1
memory_gb = 3
max_wall_s = 1800
"""


def python_task(
    task_id: str,
    script: str,
    *,
    campaign: str = CID,
    done_json: Optional[Mapping[str, Any]] = None,
    verdict: Optional[Mapping[str, Any]] = None,
    restart: str = "scratch",
    checkpoint: Optional[Mapping[str, Any]] = None,
    max_wall_s: int = 600,
    summary: Sequence[str] = ("*.json",),
    prune: Sequence[str] = (),
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """A task that runs ``script`` with ``${PYTHON}`` in its work directory; outputs in out/."""
    task = {
        "schema": spec.TASK_SCHEMA,
        "id": task_id,
        "campaign": campaign,
        "kind": "smoke",
        "visibility": "public",
        "image": "ghcr.io/studio-fug/yapnr@" + DIGEST,
        "entrypoint": None,
        "command": ["${PYTHON}", "-c", script],
        "env": dict(env or {}),
        "inputs": [],
        "outputs": {"root": "out", "summary": list(summary), "prune": list(prune)},
        "done": {"file": "out/done.json", "json": dict(done_json or {})},
        "verdict": dict(verdict) if verdict else None,
        "resources": {"cpus": 1, "memory_gb": 1, "disk_gb": 1, "max_wall_s": max_wall_s},
        "restart": restart,
        "checkpoint": dict(checkpoint) if checkpoint else None,
        "determinism": "seeded",
        "labels": {"n": task_id.rsplit("/", 1)[-1]},
    }
    return spec.check_task(task)


def write_store_campaign(
    store: Path, tasks: List[Dict[str, Any]], submission: int = 1, cid: str = CID
) -> Path:
    """The files of one campaign and one submission (all tasks) in a store directory."""
    from yapnr.exp import plan as planning

    base = Path(store) / "campaigns" / cid
    (base / "submissions").mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": planning.PLAN_SCHEMA,
        "id": cid,
        "task_hashes": {t["id"]: spec.spec_hash(t) for t in tasks},
        "source": {"commit": "f" * 40, "dirty": False},
    }
    (base / "campaign.json").write_text(json.dumps(meta))
    (base / "tasks.jsonl").write_text(
        "".join(spec.canonical_json(t).decode() + "\n" for t in tasks)
    )
    (base / "task.py").write_bytes(planning.WRAPPER.read_bytes())
    (base / "submissions" / ("%d.indices" % submission)).write_text(
        "".join("%d\n" % i for i in range(len(tasks)))
    )
    (Path(store) / "bundles").mkdir(exist_ok=True)
    return base


def host_toolchain(path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps({"PYTHON": sys.executable, "launcher": [], "isolate_home": True}))
    return path


def run_wrapper(
    store: Path,
    index: int,
    toolchain: Path,
    *,
    submission: int = 1,
    cid: str = CID,
    extra: Sequence[str] = (),
    env: Optional[Mapping[str, str]] = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess:
    wrapper = Path(store) / "campaigns" / cid / "task.py"
    argv = [
        sys.executable,
        str(wrapper),
        "--store",
        str(store),
        "--inputs",
        str(Path(store) / "bundles"),
        "--campaign",
        cid,
        "--submission",
        str(submission),
        "--index",
        str(index),
        "--toolchain",
        str(toolchain),
        *extra,
    ]
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("BATCH_", "SLURM_"))}
    run_env.update(env or {})
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=run_env)


SERVICE_ACCOUNT_RE = re.compile(r"@([a-z][a-z0-9-]*[a-z0-9])\.iam\.gserviceaccount\.com")


def normalize(text: str, tmp: Optional[Path] = None) -> str:
    """Machine-independent text for a golden file: no temporary paths, no account addresses."""
    text = SERVICE_ACCOUNT_RE.sub("@{project}.iam.gserviceaccount.com", text)
    if tmp is not None:
        text = text.replace(str(Path(tmp).resolve()), "{tmp}").replace(str(tmp), "{tmp}")
    text = re.sub(r"/[^ '\"]*/yapnr-store-[A-Za-z0-9_]+/", "{tmpfile}/", text)
    return text


def golden_dir() -> Path:
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        return Path(workspace) / "tests" / "unit" / "exp" / "golden"
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tests" / "unit" / "exp").is_dir():
            return parent / "tests" / "unit" / "exp" / "golden"
    raise FileNotFoundError("tests/unit/exp/golden")


def find_golden(name: str) -> Path:
    """The golden file in the runfiles (tests) or the source tree."""
    for base in (Path.cwd() / "tests" / "unit" / "exp" / "golden", golden_dir()):
        if (base / name).is_file():
            return base / name
    return golden_dir() / name


def check_golden(case, name: str, text: str) -> None:
    """Compare with ``tests/unit/exp/golden/<name>``; ``YAPNR_UPDATE_GOLDEN=1`` rewrites it."""
    if os.environ.get(UPDATE_ENV):
        path = golden_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    path = find_golden(name)
    case.assertTrue(path.is_file(), "missing golden %s (run with %s=1)" % (name, UPDATE_ENV))
    case.assertEqual(
        text, path.read_text(), "golden %s differs (%s=1 rewrites it)" % (name, UPDATE_ENV)
    )
