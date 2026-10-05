#!/usr/bin/env python3
"""The in-task wrapper: runs one task of a campaign, the same way on every backend.

    python task.py --store RUNS --inputs BUNDLES --campaign CID --submission N
                   [--index I] [--chunk K] [--retry R] [--toolchain image|FILE]
                   [--work-root DIR] [--nice N]

Standard library only and Python 3.9 compatible: ``yapnr exp plan`` copies this file into the
campaign (``campaigns/<cid>/task.py``, its sha256 in ``campaign.json``) and every backend runs
that copy, so a campaign does not depend on the yapnr version inside its image.

``RUNS`` is the runs store as a directory (a Cloud Storage FUSE mount on Batch, the project file
system on Slurm, a local directory); ``BUNDLES`` holds the inputs store's ``<sha256>.tar.gz``.

1. Resolve the index (``--index``, else ``BATCH_TASK_INDEX``, else ``SLURM_ARRAY_TASK_ID``) and
   map it through ``submissions/<n>.indices`` to a line of ``tasks.jsonl``; check that line's
   spec hash against ``campaign.json``. With ``--chunk K`` the index names K consecutive lines.
2. Exit 3 if ``control/frozen`` exists; skip a task whose ``_DONE`` exists.
3. Make a private work directory; point ``HOME``, ``XDG_*`` and ``TMPDIR`` into it.
4. Stage the inputs (verify each bundle's sha256, extract); restore a checkpoint for
   ``restart: resume``.
5. Run the command under ``nice``, with the task's wall-time limit, in its own process group, with
   credentials and ambient ``PNR_*`` switches removed from the environment. On Batch it polls the
   metadata server's ``preempted`` flag; SIGTERM and SIGUSR1 stop it; for resumable tasks both
   flush the checkpoint first.
6. Evaluate ``done`` and ``verdict``, prune, and write ``result.tar.gz``, ``summary/``,
   ``record.json`` and ``log.tail`` under ``tasks/<task>/<attempt>/``; ``_DONE`` is written last.
7. Exit 0 whenever a result was recorded (pass, fail, timeout or an engine crash: retrying would
   repeat it); exit 75 for staging or upload failures, when stopped by a signal, and when the
   command itself exits 75 without a result (all retried, nothing recorded; a resumable task's
   checkpoint is synced first, so a command can exit 75 to continue in a new attempt); exit 2 for a
   malformed campaign.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

WRAPPER_VERSION = 1
TASK_SCHEMA = "yapnr-task-v1"
RECORD_SCHEMA = "yapnr-task-record-v1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FROZEN = 3
EXIT_TEMPFAIL = 75  # EX_TEMPFAIL: retried by the backend

KILL_GRACE_S = 30
LOG_TAIL_LINES = 200
# The metadata server by address: containers on Batch VMs may not resolve its host name.
METADATA = "http://169.254.169.254/computeMetadata/v1/"

# The image's toolchain (docs/containers.md). Local and Slurm-site profiles are JSON files.
TOOLCHAINS = {
    "image": {
        "PYTHON": "/opt/venv/bin/python",
        "KICAD_CLI": "/usr/bin/kicad-cli",
        "KICAD_PYTHON": "/usr/bin/python3",
        "FOOTPRINTS": "/usr/share/kicad/footprints",
        # The task's own entrypoint (the image's yapnr-kicad-env) wraps each command, so KiCad
        # finds its library tables in the private HOME the wrapper sets up.
        "use_task_entrypoint": True,
        "launcher": [],
        "isolate_home": True,
    }
}
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_]+)\}")
SCRUB_RE = re.compile(
    r"(^PNR_|TOKEN|SECRET|PASSWORD|CREDENTIAL|^GOOGLE_|^CLOUDSDK_|^AWS_|^GITHUB_|^PYTHONPATH$)"
)


class TaskFailure(Exception):
    """A failure that ends the wrapper with ``code`` and records nothing."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def task_key(task_id):
    """The store directory of a task: its id with '/' replaced by '~' (ids never contain '~')."""
    return task_id.replace("/", "~")


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status(task_id, event, **fields):
    """One JSON status line on stdout (Cloud Logging keeps these; no inputs, no paths)."""
    line = dict(yapnr_task=task_id, event=event, time=utc_now())
    line.update(fields)
    print(json.dumps(line, sort_keys=True), flush=True)


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def copy_atomic(src, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(".%s.tmp-%d" % (dest.name, os.getpid()))
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def metadata(path, timeout=2.0):
    """A value from the Compute Engine metadata server, or None off Google Cloud."""
    request = urllib.request.Request(METADATA + path, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode().strip()
    except Exception:
        return None


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _command_output(argv):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def backend_name():
    if os.environ.get("BATCH_TASK_INDEX") is not None or os.environ.get("BATCH_JOB_UID"):
        return "gcp-batch"
    if os.environ.get("YAPNR_BACKEND"):
        return os.environ["YAPNR_BACKEND"]
    if os.environ.get("SLURM_JOB_ID"):
        return "slurm"
    return "local"


def machine_info(on_gcp):
    """Machine type, CPU model, vCPUs, threads per core, platform and kernel."""
    info = dict(
        platform="%s-%s" % (sys.platform, platform.machine().lower()),
        kernel=platform.release(),
        vcpus=os.cpu_count(),
        hostname_hash=hashlib.sha256(platform.node().encode()).hexdigest()[:12],
    )
    if hasattr(os, "sched_getaffinity"):
        info["vcpus_available"] = len(os.sched_getaffinity(0))
    if sys.platform == "darwin":
        info["cpu_model"] = _command_output(["sysctl", "-n", "machdep.cpu.brand_string"])
        logical = _command_output(["sysctl", "-n", "hw.logicalcpu"])
        physical = _command_output(["sysctl", "-n", "hw.physicalcpu"])
        if logical and physical and physical.isdigit() and int(physical):
            info["threads_per_core"] = int(logical) // int(physical)
    else:
        model = None
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        if model is None:
            lscpu = _command_output(["lscpu"])
            for line in (lscpu or "").splitlines():
                if line.startswith("Model name:"):
                    model = line.split(":", 1)[1].strip()
                    break
        info["cpu_model"] = model
        siblings = _read("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list")
        if siblings:
            count = 0
            for part in siblings.split(","):
                low, _, high = part.partition("-")
                count += int(high) - int(low) + 1 if high else 1
            info["threads_per_core"] = count
    if on_gcp:
        machine_type = metadata("instance/machine-type")
        zone = metadata("instance/zone")
        info.update(
            machine_type=machine_type.rsplit("/", 1)[-1] if machine_type else None,
            zone=zone.rsplit("/", 1)[-1] if zone else None,
            provisioning_model=metadata("instance/scheduling/provisioning-model")
            or ("SPOT" if metadata("instance/scheduling/preemptible") == "TRUE" else None),
        )
        if info.get("zone"):
            info["region"] = info["zone"].rsplit("-", 1)[0]
    return info


class Campaign:
    """The campaign files of one submission in the runs store."""

    def __init__(self, store, campaign_id, submission):
        self.store = Path(store)
        self.id = campaign_id
        self.submission = int(submission)
        self.dir = self.store / "campaigns" / campaign_id
        try:
            self.meta = json.loads((self.dir / "campaign.json").read_text())
            self.lines = (self.dir / "tasks.jsonl").read_text().splitlines()
            indices = (self.dir / "submissions" / ("%d.indices" % self.submission)).read_text()
        except (OSError, ValueError) as err:
            raise TaskFailure(EXIT_TEMPFAIL, "cannot read the campaign: %s" % err) from err
        if self.meta.get("id") != campaign_id:
            raise TaskFailure(EXIT_USAGE, "campaign.json names another campaign")
        self.indices = [int(x) for x in indices.split()]

    def positions(self, index, chunk):
        start = index * chunk
        if start >= len(self.indices) or index < 0:
            raise TaskFailure(EXIT_USAGE, "index %d is outside the submission" % index)
        return list(range(start, min(start + chunk, len(self.indices))))

    def task(self, position):
        line_no = self.indices[position]
        try:
            task = json.loads(self.lines[line_no])
        except (IndexError, ValueError) as err:
            raise TaskFailure(EXIT_USAGE, "tasks.jsonl line %d: %s" % (line_no, err)) from err
        if task.get("schema") != TASK_SCHEMA:
            raise TaskFailure(EXIT_USAGE, "unknown task schema %r" % task.get("schema"))
        if task.get("campaign") != self.id:
            raise TaskFailure(EXIT_USAGE, "task %s belongs to another campaign" % task.get("id"))
        digest = hashlib.sha256(canonical_json(task)).hexdigest()
        if self.meta.get("task_hashes", {}).get(task["id"]) != digest:
            raise TaskFailure(EXIT_USAGE, "task %s does not match its spec hash" % task["id"])
        return task, digest

    def frozen(self):
        return (self.store / "control" / "frozen").exists()

    def task_dir(self, task_id):
        return self.dir / "tasks" / task_key(task_id)

    def checkpoint_dir(self, task_id):
        return self.store / "checkpoints" / self.id / task_key(task_id)


class Stop:
    """Signals that ask the wrapper to stop (SIGTERM: Spot preemption or cancel; SIGUSR1: Slurm)."""

    def __init__(self):
        self.signal = None
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                signal.signal(sig, self._handle)

    def _handle(self, signum, frame):
        self.signal = signum


def load_toolchain(name):
    if name in TOOLCHAINS:
        return dict(TOOLCHAINS[name])
    try:
        profile = json.loads(Path(name).read_text())
    except (OSError, ValueError) as err:
        raise TaskFailure(EXIT_USAGE, "toolchain %s: %s" % (name, err)) from err
    profile.setdefault("launcher", [])
    profile.setdefault("isolate_home", True)
    return profile


def substitute(argv, toolchain):
    out = []
    for arg in argv:

        def value(match):
            name = match.group(1)
            if not toolchain.get(name):
                raise TaskFailure(EXIT_USAGE, "the toolchain has no %s" % name)
            return toolchain[name]

        out.append(PLACEHOLDER_RE.sub(value, arg))
    return out


def task_environment(task, toolchain, work, campaign_meta, attempt):
    env = {k: v for k, v in os.environ.items() if not SCRUB_RE.search(k)}
    if toolchain.get("isolate_home", True):
        # The XDG directories are the defaults below HOME, where yapnr-kicad-env seeds KiCad's
        # library tables ($HOME/.config/kicad/<series>).
        home = work / ".yapnr" / "home"
        dirs = dict(
            XDG_CONFIG_HOME=home / ".config",
            XDG_CACHE_HOME=home / ".cache",
            XDG_DATA_HOME=home / ".local" / "share",
        )
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env.update({name: str(path) for name, path in dirs.items()})
    tmp = work / ".yapnr" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    for name, keys in (
        ("KICAD_CLI", ("PNR_KICAD_CLI", "YAPNR_KICAD_CLI")),
        ("KICAD_PYTHON", ("PNR_KICAD_PYTHON", "YAPNR_KICAD_PYTHON")),
        ("FOOTPRINTS", ("PNR_KICAD_FOOTPRINTS",)),
    ):
        if toolchain.get(name):
            for key in keys:
                env[key] = toolchain[name]
    live_cfg = campaign_meta.get("live")
    if live_cfg and live_cfg.get("enabled"):
        # The live viewer (docs/viewer.md): the engine's telemetry writer (pnr.live) picks this
        # up on its own; LiveUploader below packs what it writes into bundles for the mirror.
        env["PNR_LIVE_DIR"] = str(work / ".yapnr" / "live")
        env["PNR_LIVE_CANDIDATE"] = task["id"]
    source = campaign_meta.get("source") or {}
    if source.get("commit"):
        env["YAPNR_ENGINE_REVISION"] = source["commit"]
        env["YAPNR_ENGINE_DIRTY"] = "1" if source.get("dirty") else "0"
    env.update(
        YAPNR_TASK_ID=task["id"],
        YAPNR_CAMPAIGN=task["campaign"],
        YAPNR_ATTEMPT=attempt,
        PYTHONUNBUFFERED="1",
    )
    env.update(task["env"])
    return env


def with_launcher(argv, task, toolchain):
    """Prefix the launcher: the task's entrypoint in the image, the profile's launcher locally."""
    launcher = [x for x in toolchain.get("launcher", []) if x]
    if toolchain.get("use_task_entrypoint") and task.get("entrypoint"):
        launcher = [task["entrypoint"]]
    if launcher and Path(launcher[0]).exists():
        return launcher + argv
    return argv


def stage_inputs(task, inputs_dir, work):
    for item in task["inputs"]:
        bundle = Path(inputs_dir) / ("%s.tar.gz" % item["bundle"])
        try:
            data = bundle.read_bytes()
        except OSError as err:
            raise TaskFailure(EXIT_TEMPFAIL, "input %s: %s" % (item["dest"], err)) from err
        if hashlib.sha256(data).hexdigest() != item["bundle"]:
            raise TaskFailure(EXIT_TEMPFAIL, "input %s: sha256 mismatch" % item["dest"])
        dest = work / item["dest"]
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                name, link = member.name, member.linkname
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or member.isdev()
                    or member.isfifo()
                    or (
                        (member.issym() or member.islnk())
                        and (link.startswith("/") or ".." in Path(link).parts)
                    )
                ):
                    raise TaskFailure(EXIT_USAGE, "input %s: unsafe member" % item["dest"])
            if hasattr(tarfile, "data_filter"):  # checked above; silences the 3.12+ warning
                tar.extractall(dest, members=members, filter="fully_trusted")
            else:
                tar.extractall(dest, members=members)


class Checkpoints:
    """Copies a resumable task's checkpoint to the store and restores it on a retry.

    ``checkpoint.path`` names a file, or a directory whose top-level files are the checkpoint
    (subdirectories, such as caches, are not kept). A checkpoint of several files is consistent
    only as a set, and a sync can be cut short (a Spot VM gets about 30 s, the container less),
    so each sync writes its changed files into a new generation directory ``g<n>/`` and then
    ``_checkpoint.json``, which maps every file to the copy it belongs with. A restore reads only
    what the manifest names: a cut-short sync leaves the previous generation whole. Generations
    no manifest names any more are removed after the manifest is written.
    """

    MANIFEST = "_checkpoint.json"
    STORED_RE = re.compile(r"^g[0-9]+/[^/]+$")

    def __init__(self, task, work, store_dir):
        self.spec = task["checkpoint"] if task["restart"] == "resume" else None
        self.local = work / self.spec["path"] if self.spec else None
        self.work = work
        self.remote = store_dir
        self.last_sync = time.monotonic()
        self.stamps = {}  # file name -> (mtime_ns, size) of the copy in the store
        self.stored = {}  # file name -> its path in the store (the manifest's "files")
        self.generation = 0

    def _files(self):
        """The checkpoint's files; hidden ones (an application's temporary files) are not kept."""
        if self.local.is_dir():
            return [
                p
                for p in sorted(self.local.iterdir())
                if p.is_file() and not p.name.startswith(".")
            ]
        return [self.local] if self.local.is_file() else []

    @staticmethod
    def _stamp(path):
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _manifest(self):
        try:
            manifest = json.loads((self.remote / self.MANIFEST).read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            return None
        files = manifest.get("files") or {}
        if isinstance(files, list):  # the first layout: the files flat beside the manifest
            files = {name: name for name in files}
        safe = {
            name: stored
            for name, stored in files.items()
            if "/" not in name
            and not name.startswith(".")
            and (self.STORED_RE.match(stored) or stored == name)
        }
        return dict(manifest, files=safe)

    def restore(self):
        """Restore the last whole checkpoint; False if there is none (start from scratch).

        The files are copied into a staging directory first, so a store error never leaves a
        half-restored mix in the work directory. Raises TaskFailure(EXIT_TEMPFAIL) when the store
        cannot be read (a retry may succeed).
        """
        if not self.spec:
            return False
        try:
            manifest = self._manifest()
            if not manifest or not manifest["files"]:
                return False
            staging = self.work / ".yapnr" / "restore"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            for name, stored in manifest["files"].items():
                shutil.copyfile(self.remote / stored, staging / name)
        except FileNotFoundError:
            # A file the manifest names is gone: nothing consistent to resume from.
            shutil.rmtree(self.work / ".yapnr" / "restore", ignore_errors=True)
            return False
        except OSError as err:
            raise TaskFailure(EXIT_TEMPFAIL, "checkpoint restore: %s" % err) from err
        target = self.local if manifest.get("kind") == "dir" else self.local.parent
        target.mkdir(parents=True, exist_ok=True)
        for name in manifest["files"]:
            os.replace(staging / name, target / name)
            self.stamps[name] = self._stamp(target / name)
        shutil.rmtree(staging, ignore_errors=True)
        self.stored = dict(manifest["files"])
        self.generation = int(manifest.get("generation") or 0)
        return True

    def due(self):
        return bool(self.spec) and time.monotonic() - self.last_sync >= self.spec["sync_every_s"]

    def sync(self):
        if not self.spec:
            return False
        self.last_sync = time.monotonic()
        files = self._files()
        generation = self.generation + 1
        stored, stamps, changed = {}, {}, False
        for path in files:
            stamp = self._stamp(path)
            if self.stamps.get(path.name) == stamp and path.name in self.stored:
                stored[path.name] = self.stored[path.name]
            else:
                stored[path.name] = "g%d/%s" % (generation, path.name)
                copy_atomic(path, self.remote / stored[path.name])
                changed = True
            stamps[path.name] = stamp
        if not changed and set(stored) == set(self.stored):
            return False
        manifest = dict(
            kind="dir" if self.local.is_dir() else "file",
            files=stored,
            generation=generation,
            time=utc_now(),
        )
        write_atomic(self.remote / self.MANIFEST, json.dumps(manifest).encode())
        self.stored, self.stamps, self.generation = stored, stamps, generation
        self._prune()
        return True

    def _prune(self):
        """Remove the generations (and first-layout files) the manifest no longer names."""
        keep = set(self.stored.values())
        keep_dirs = {stored.split("/", 1)[0] for stored in keep if "/" in stored}
        try:
            entries = list(self.remote.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name == self.MANIFEST or entry.name.startswith("."):
                continue
            try:
                if entry.is_dir() and re.match(r"^g[0-9]+$", entry.name):
                    if entry.name not in keep_dirs:
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        for child in entry.iterdir():
                            if "%s/%s" % (entry.name, child.name) not in keep:
                                child.unlink()
                elif entry.is_file() and entry.name not in keep:
                    entry.unlink()
            except OSError:
                pass  # the next sync tries again


LIVE_THIN_DROP_PREFIX = "signal_net_"  # per-net maze/grid events (pnr/route/detail/maze.py)


class LiveUploader:
    """Packs new live-viewer telemetry (hardware/pnr/pnr/live.py) into bundles for the store.

    ``live_spec`` is the campaign's ``[live]`` config (``{"enabled", "interval_s", "mode"}``) or
    None when the live viewer is off; every method is then a no-op, so a task's behaviour and
    outputs are byte-identical to before this feature existed. A bundle holds the events (and the
    boards they reference) written since the last one, as a ``tar.gz`` at
    ``<campaign>/live/<task key>/<attempt>/<seq>.tar.gz`` (docs/cloud-experiments.md, "Live viewer
    mirror"); ``yapnr exp live`` unpacks these into a local mirror the viewer reads. In ``"thin"``
    mode the per-net maze events are dropped (``LIVE_THIN_DROP_PREFIX``); checkpoints, round
    summaries and placement costs are kept either way. Namespacing bundles under the attempt
    means a retried or preempted task's lane continues (new events keep landing after the ones
    its last attempt sent) without seq numbers from different attempts colliding.
    """

    def __init__(self, live_spec, work, remote):
        self.spec = live_spec
        self.live_dir = work / ".yapnr" / "live"
        self.remote = Path(remote)
        self.last_sync = time.monotonic()
        self.seq = 0
        self.sent_events = set()
        self.sent_boards = set()

    def due(self):
        return bool(self.spec) and time.monotonic() - self.last_sync >= self.spec["interval_s"]

    def _pending(self):
        events_dir = self.live_dir / "events"
        if not events_dir.is_dir():
            return []
        return sorted(p for p in events_dir.glob("*.json") if p.name not in self.sent_events)

    def sync(self):
        """Upload one bundle of everything new since the last call; True when one was written."""
        if not self.spec:
            return False
        self.last_sync = time.monotonic()
        kept = []
        for path in self._pending():
            self.sent_events.add(path.name)
            try:
                event = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if self.spec["mode"] == "thin" and str(event.get("kind", "")).startswith(
                LIVE_THIN_DROP_PREFIX
            ):
                continue
            kept.append((path, event))
        boards = []
        for path, event in kept:
            sha = event.get("board_sha256")
            if sha and sha not in self.sent_boards:
                board_path = self.live_dir / "boards" / (sha + ".kicad_pcb")
                if board_path.is_file():
                    boards.append((sha, board_path))
                    self.sent_boards.add(sha)
        if not kept and not boards:
            return False
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, _event in kept:
                tar.add(str(path), arcname="events/" + path.name)
            for sha, board_path in boards:
                tar.add(str(board_path), arcname="boards/" + sha + ".kicad_pcb")
        self.seq += 1
        write_atomic(self.remote / ("%06d.tar.gz" % self.seq), buf.getvalue())
        return True


def _done(task, work):
    path = work / task["done"]["file"]
    if not path.is_file():
        return False
    want = task["done"]["json"]
    if not want:
        return True
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and all(data.get(k) == v for k, v in want.items())


def _verdict(task, work):
    spec = task["verdict"]
    if spec is None:
        return "done"
    try:
        value = json.loads((work / spec["file"]).read_text())
        for part in spec["json_path"].split("."):
            value = value[part]
    except (OSError, ValueError, KeyError, TypeError):
        return "done"
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return str(value)[:40]


def prune(root, globs):
    removed = 0
    for pattern in globs:
        for name in glob.glob(str(root / pattern), recursive=True):
            path = Path(name)
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
            elif path.is_dir():
                shutil.rmtree(path)
                removed += 1
    return removed


def tail(path, lines):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read().decode(errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def _kill_group(proc_pid, grace):
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc_pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                pid, _, _ = os.wait4(proc_pid, os.WNOHANG)
            except ChildProcessError:
                return
            if pid:
                return
            time.sleep(0.2)


def run_command(argv, cwd, env, wall_s, niceness, stop, checkpoints, live, logs, on_gcp):
    """Run ``argv``; returns (exit_code, timed_out, stopped_by_signal, rusage)."""
    logs.mkdir(parents=True, exist_ok=True)

    def lower_priority():
        if niceness:
            os.nice(niceness)

    with open(logs / "stdout.log", "wb") as out, open(logs / "stderr.log", "wb") as err:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=lower_priority,
            )
        except OSError as error:
            err.write(("cannot start the command: %s\n" % error).encode())
            return 127, False, None, None
        deadline = time.monotonic() + wall_s
        next_poll = time.monotonic()
        while True:
            pid, wait_status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                proc.returncode = os.waitstatus_to_exitcode(wait_status)
                return proc.returncode, False, None, usage
            if stop.signal is not None:
                if checkpoints.spec and checkpoints.spec["on_signal"]:
                    try:
                        checkpoints.sync()
                    except OSError:
                        pass  # the last whole generation in the store stays the checkpoint
                if live.spec:
                    try:
                        live.sync()  # the last events before this attempt's lane goes quiet
                    except OSError:
                        pass  # the next attempt's bundles pick up where this one left off
                _kill_group(proc.pid, KILL_GRACE_S)
                proc.returncode = -1
                return None, False, stop.signal, None
            if on_gcp and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + 5
                if metadata("instance/preempted", timeout=1) == "TRUE":
                    stop.signal = signal.SIGTERM
                    continue
            if checkpoints.due():
                try:
                    checkpoints.sync()
                except OSError:
                    pass  # the next sync tries again
            if live.due():
                try:
                    live.sync()
                except OSError:
                    pass  # the next sync tries again
            if time.monotonic() >= deadline:
                _kill_group(proc.pid, KILL_GRACE_S)
                proc.returncode = -1
                return None, True, None, None
            time.sleep(0.5)


def collect(task, work, logs, staging):
    """Prune, then build result.tar.gz, summary/ and log.tail in ``staging``."""
    root = work / task["outputs"]["root"]
    pruned = prune(root, task["outputs"]["prune"]) if root.is_dir() else 0
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(staging / "result.tar.gz", "w:gz") as tar:
        if root.is_dir():
            tar.add(str(root), arcname=task["outputs"]["root"])
        for name in ("stdout.log", "stderr.log"):
            if (logs / name).is_file():
                tar.add(str(logs / name), arcname="_logs/" + name)
    summary_files = []
    for pattern in task["outputs"]["summary"]:
        for name in sorted(glob.glob(str(root / pattern), recursive=True)):
            path = Path(name)
            if path.is_file():
                rel = path.relative_to(root)
                dest = staging / "summary" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                summary_files.append(str(rel))
    lines = ["== stderr (last lines)"] + tail(logs / "stderr.log", LOG_TAIL_LINES // 2)
    lines += ["== stdout (last lines)"] + tail(logs / "stdout.log", LOG_TAIL_LINES // 2)
    (staging / "log.tail").write_text("\n".join(lines) + "\n")
    return pruned, sorted(set(summary_files))


def upload(staging, attempt_dir):
    """Copy the staged attempt into the store; files first, the directory listing never."""
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            copy_atomic(path, attempt_dir / path.relative_to(staging))


def pick_attempt(task_dir, submission, retry):
    base = "s%dr%d" % (submission, retry)
    for n in range(1, 50):
        name = base if n == 1 else "%s-%d" % (base, n)
        if not (task_dir / name).exists():
            return name
    raise TaskFailure(EXIT_TEMPFAIL, "no free attempt directory")


def run_task(args, campaign, position, toolchain, stop):
    task, digest = campaign.task(position)
    task_id = task["id"]
    if campaign.frozen():
        status(task_id, "frozen")
        return EXIT_FROZEN
    task_dir = campaign.task_dir(task_id)
    if (task_dir / "_DONE").exists():
        status(task_id, "skip", reason="done")
        return EXIT_OK
    retry = args.retry
    if retry is None:
        retry = int(
            os.environ.get("BATCH_TASK_RETRY_ATTEMPT") or os.environ.get("SLURM_RESTART_COUNT") or 0
        )
    attempt = pick_attempt(task_dir, campaign.submission, retry)
    on_gcp = backend_name() == "gcp-batch"
    work_root = Path(args.work_root) if args.work_root else None
    if work_root:
        work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="yapnr-task-", dir=str(work_root) if work_root else None))
    logs = work / ".yapnr" / "logs"
    try:
        status(task_id, "start", attempt=attempt)
        load_start = os.getloadavg() if hasattr(os, "getloadavg") else None
        stage_inputs(task, args.inputs, work)
        checkpoints = Checkpoints(task, work, campaign.checkpoint_dir(task_id))
        restored = checkpoints.restore()
        status(task_id, "staged", restored_checkpoint=restored)
        live_cfg = campaign.meta.get("live")
        live = LiveUploader(
            live_cfg if live_cfg and live_cfg.get("enabled") else None,
            work,
            campaign.dir / "live" / task_key(task_id) / attempt,
        )
        env = task_environment(task, toolchain, work, campaign.meta, attempt)
        argv = substitute(task["command"], toolchain)
        argv = with_launcher(argv, task, toolchain)
        started, start_clock = utc_now(), time.monotonic()
        code, timed_out, stopped, usage = run_command(
            argv,
            work,
            env,
            task["resources"]["max_wall_s"],
            args.nice,
            stop,
            checkpoints,
            live,
            logs,
            on_gcp,
        )
        wall = time.monotonic() - start_clock
        if stopped is not None:
            status(task_id, "stopped", signal=int(stopped))
            return EXIT_TEMPFAIL
        finished = _done(task, work)
        if code == EXIT_TEMPFAIL and not finished and not timed_out:
            # The command's own EX_TEMPFAIL without a result: transient, retried, not recorded.
            # A resumable command also exits 75 to go on in a fresh attempt (its time is nearly
            # up), so its last checkpoint goes to the store first.
            if checkpoints.spec:
                try:
                    checkpoints.sync()
                except OSError:
                    pass  # the last whole generation in the store stays the checkpoint
            if live.spec:
                try:
                    live.sync()
                except OSError:
                    pass  # the next attempt's bundles pick up where this one left off
            status(task_id, "tempfail", exit_code=code)
            return EXIT_TEMPFAIL
        if timed_out:
            verdict = "timeout"
        elif finished:
            verdict = _verdict(task, work)
        else:
            verdict = "error"
        if checkpoints.spec:
            try:
                checkpoints.sync()
            except OSError:
                pass  # the result below is what counts; a checkpoint only serves a resume
        if live.spec:
            try:
                live.sync()  # the last events of this attempt, win or lose
            except OSError:
                pass  # best-effort: a missed bundle only shortens the live replay
        staging = work / ".yapnr" / "attempt"
        pruned, summary_files = collect(task, work, logs, staging)
        rss = usage.ru_maxrss if usage else None
        if rss is not None:
            rss = rss / (1024.0 * 1024.0) if sys.platform == "darwin" else rss / 1024.0
        record = dict(
            schema=RECORD_SCHEMA,
            wrapper_version=WRAPPER_VERSION,
            campaign=campaign.id,
            campaign_spec_sha256=campaign.meta.get("spec_sha256"),
            task=task_id,
            task_spec_sha256=digest,
            kind=task["kind"],
            labels=task["labels"],
            submission=campaign.submission,
            attempt=attempt,
            retry=retry,
            source=campaign.meta.get("source"),
            # The image only when the task ran in it; a host toolchain records its profile name.
            image=dict(
                ref=task["image"] if args.toolchain == "image" else None,
                toolchain="image" if args.toolchain == "image" else "host",
                source_revision=os.environ.get("YAPNR_SOURCE_REVISION"),
                sif_sha256=os.environ.get("YAPNR_SIF_SHA256") or None,
            ),
            backend=backend_name(),
            machine=machine_info(on_gcp),
            python=platform.python_version(),
            started=started,
            finished=utc_now(),
            wall_s=round(wall, 3),
            cpu_s=round(usage.ru_utime + usage.ru_stime, 3) if usage else None,
            peak_rss_mb=round(rss, 1) if rss is not None else None,
            load_avg_start=list(load_start) if load_start else None,
            load_avg_end=list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            exit_code=code,
            timed_out=timed_out,
            verdict=verdict,
            pruned=pruned,
            summary_files=summary_files,
            resources=task["resources"],
        )
        (staging / "record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        try:
            upload(staging, task_dir / attempt)
            write_atomic(
                task_dir / "_DONE",
                (
                    json.dumps(
                        dict(
                            task=task_id,
                            attempt=attempt,
                            verdict=verdict,
                            spec_sha256=digest,
                            time=utc_now(),
                        ),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )
        except OSError as err:
            raise TaskFailure(EXIT_TEMPFAIL, "upload failed: %s" % err) from err
        status(task_id, "done", verdict=verdict, wall_s=round(wall, 1), exit_code=code)
        return EXIT_OK
    except TaskFailure as failure:
        status(task_id, "failure", code=failure.code, reason=str(failure)[:300])
        return failure.code
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--store", required=True, help="the runs store, as a directory")
    parser.add_argument("--inputs", required=True, help="the directory of input bundles")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--submission", type=int, required=True)
    parser.add_argument("--index", type=int, help="else BATCH_TASK_INDEX or SLURM_ARRAY_TASK_ID")
    parser.add_argument("--chunk", type=int, default=1, help="tasks per index (Slurm arrays)")
    parser.add_argument("--retry", type=int, help="the retry attempt (else from the backend)")
    parser.add_argument("--toolchain", default="image", help="'image' or a JSON profile")
    parser.add_argument("--work-root", help="where work directories go (default: TMPDIR)")
    parser.add_argument("--nice", type=int, default=0)
    parser.add_argument("--keep-work", action="store_true", help="keep the work directory")
    return parser.parse_args(argv)


def resolve_index(explicit):
    if explicit is not None:
        return explicit
    for name in ("BATCH_TASK_INDEX", "SLURM_ARRAY_TASK_ID"):
        if os.environ.get(name, "").isdigit():
            return int(os.environ[name])
    raise TaskFailure(EXIT_USAGE, "no task index (--index, BATCH_TASK_INDEX, SLURM_ARRAY_TASK_ID)")


def main(argv=None):
    args = parse_args(argv)
    stop = Stop()
    try:
        if args.chunk < 1:
            raise TaskFailure(EXIT_USAGE, "--chunk is at least 1")
        campaign = Campaign(args.store, args.campaign, args.submission)
        toolchain = load_toolchain(args.toolchain)
        if args.toolchain == "image":
            # Another image than yapnr's names its interpreter in the campaign's runtime.
            runtime = (campaign.meta.get("image") or {}).get("runtime") or {}
            if runtime.get("python"):
                toolchain["PYTHON"] = runtime["python"]
        positions = campaign.positions(resolve_index(args.index), args.chunk)
    except TaskFailure as failure:
        print(
            json.dumps(dict(yapnr_task=None, event="failure", reason=str(failure)[:300])),
            flush=True,
        )
        return failure.code
    worst = EXIT_OK
    for position in positions:
        if stop.signal is not None:
            return EXIT_TEMPFAIL
        try:
            code = run_task(args, campaign, position, toolchain, stop)
        except TaskFailure as failure:  # a malformed line or a hash mismatch
            print(
                json.dumps(dict(yapnr_task=None, event="failure", reason=str(failure)[:300])),
                flush=True,
            )
            code = failure.code
        if code == EXIT_FROZEN:
            return EXIT_FROZEN
        if code == EXIT_USAGE:
            return EXIT_USAGE
        if code != EXIT_OK:
            worst = code
        if stop.signal is not None:
            return EXIT_TEMPFAIL
    return worst


if __name__ == "__main__":
    sys.exit(main())
