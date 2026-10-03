"""The owner configuration of the experiment layer: ``~/.config/yapnr/cloud.toml``.

Owner-specific values (project, buckets, regions, toolchain paths, Slurm sites) live only here,
never in the repository. ``YAPNR_CLOUD_CONFIG`` names another file. The annotated example is
docs/examples/cloud.toml.example; every key and default is documented there.

A missing file is not an error: the local backend works with the defaults. The ``gcp`` and
``slurm`` sections are checked when a command needs them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ENV_CONFIG = "YAPNR_CLOUD_CONFIG"
DEFAULT_PATH = "~/.config/yapnr/cloud.toml"

PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

# A retried task repeats its work; more than three retries means something other than Spot is
# wrong (docs/design/cloud-experiments.md, section 10).
MAX_RETRIES_LIMIT = 3


class ConfigError(ValueError):
    def __init__(self, path: str, errors: List[str]):
        self.errors = list(errors)
        super().__init__("%s: %s" % (path, "; ".join(self.errors)))


@dataclass
class Limits:
    max_tasks: int = 2000
    max_parallel_vcpus: int = 64
    max_task_wall_s: int = 14400
    max_campaign_hours: float = 12
    max_retries: int = 3
    confirm_usd: float = 5
    refuse_usd: float = 25
    hard_refuse_usd: float = 100
    allow_on_demand: bool = False


@dataclass
class Gcp:
    project: str
    home_region: str
    regions: List[str]
    inputs_bucket: str
    runs_bucket: str
    submit_service_account: str = "yapnr-submit"
    runner_service_account: str = "yapnr-runner"
    registry: str = "{region}-docker.pkg.dev/{project}/ghcr"
    subnetwork: str = "projects/{project}/regions/{region}/subnetworks/yapnr-{region}"
    template: str = "yapnr-{shape}-{model}-{region}"
    gcloud: str = "gcloud"
    gcloud_configuration: Optional[str] = None
    price_api_key_command: List[str] = field(default_factory=list)
    ranking: List[Tuple[str, str]] = field(default_factory=list)
    queue_timeout_s: int = 1800
    container_user: Optional[str] = "0:0"
    gcsfuse_options: List[str] = field(default_factory=lambda: ["--implicit-dirs"])
    boot_disk_gb: int = 30
    template_families: List[str] = field(default_factory=lambda: ["c4d", "c4", "c4a", "n4"])

    def account(self, account_id: str) -> str:
        return "%s@%s.iam.gserviceaccount.com" % (account_id, self.project)

    @property
    def submit_email(self) -> str:
        return self.account(self.submit_service_account)

    @property
    def runner_email(self) -> str:
        return self.account(self.runner_service_account)


@dataclass
class Local:
    store: str = "~/yapnr-runs"
    private_results_root: str = "~/yapnr-private-runs"
    workers: int = 4
    nice: int = 10
    python: Optional[str] = None
    kicad_cli: Optional[str] = None
    kicad_python: Optional[str] = None
    footprints: Optional[str] = None
    isolate_home: bool = True
    public_remotes: List[str] = field(default_factory=lambda: [r"github\.com[:/]studio-fug/"])

    def store_path(self, private: bool = False) -> Path:
        root = self.private_results_root if private else self.store
        return Path(os.path.expandvars(os.path.expanduser(root)))


@dataclass
class SlurmSite:
    name: str
    store: str
    sif: str
    account: Optional[str] = None
    partition: Optional[str] = None
    qos: Optional[str] = None
    max_array: int = 1000
    max_concurrent: int = 64
    chunk: int = 8
    max_time_h: float = 48
    module: Optional[str] = "apptainer"
    apptainer: str = "apptainer"
    private_ok: bool = False
    extra_sbatch: List[str] = field(default_factory=list)
    # Tasks run side by side in one array element. 1 suits sites that share nodes and charge per
    # core; on sites that allocate (and charge) whole nodes, set it to the tasks a node holds and
    # add "--exclusive" to extra_sbatch, or every one-core task is billed a whole node.
    slots: int = 1
    # The site's core speed relative to the reference core (the development Mac's M4 performance
    # core) for the core-hour estimate; typical HPC server cores are about half of it (PassMark
    # single-thread), until a calibration run on the site measures it.
    speed: float = 0.5


@dataclass
class Config:
    path: Optional[str] = None
    gcp: Optional[Gcp] = None
    limits: Limits = field(default_factory=Limits)
    local: Local = field(default_factory=Local)
    slurm: Dict[str, SlurmSite] = field(default_factory=dict)
    price_table: Optional[str] = None
    calibration: Optional[str] = None

    def require_gcp(self) -> Gcp:
        if self.gcp is None:
            raise ConfigError(self.path or DEFAULT_PATH, ["no [gcp] section (see the example)"])
        return self.gcp

    def require_site(self, name: Optional[str]) -> SlurmSite:
        if not self.slurm:
            raise ConfigError(self.path or DEFAULT_PATH, ["no [slurm.<site>] section"])
        if name is None:
            if len(self.slurm) != 1:
                raise ConfigError(self.path or DEFAULT_PATH, ["name the Slurm site (--site)"])
            return next(iter(self.slurm.values()))
        if name not in self.slurm:
            raise ConfigError(self.path or DEFAULT_PATH, ["no Slurm site %r" % name])
        return self.slurm[name]


def _take(table: Mapping[str, Any], cls, where: str, errors: List[str], **extra):
    """Build the dataclass ``cls`` from ``table``, recording unknown keys and wrong types."""
    names = {f.name: f for f in cls.__dataclass_fields__.values()}
    values = dict(extra)
    for key, value in table.items():
        if key not in names or key in extra:
            errors.append("unknown key %s.%s" % (where, key))
            continue
        values[key] = value
    try:
        return cls(**values)
    except TypeError as err:
        errors.append("%s: %s" % (where, err))
        return None


def _check_types(obj: Any, where: str, errors: List[str]) -> None:
    hints = {
        int: (int,),
        float: (int, float),
        bool: (bool,),
        str: (str,),
    }
    for name, f in obj.__dataclass_fields__.items():
        value = getattr(obj, name)
        kind = f.type if isinstance(f.type, type) else None
        text = str(f.type)
        if value is None:
            continue
        if text in ("int", "float", "bool", "str"):
            kind = {"int": int, "float": float, "bool": bool, "str": str}[text]
        if kind in hints:
            ok = isinstance(value, hints[kind]) and not (
                kind is not bool and isinstance(value, bool)
            )
            if not ok:
                errors.append("%s.%s must be %s" % (where, name, kind.__name__))
        elif text.startswith(("List[str]", "Optional[str]")):
            if text.startswith("List") and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                errors.append("%s.%s must be a list of strings" % (where, name))
            if text.startswith("Optional[str]") and not isinstance(value, str):
                errors.append("%s.%s must be a string" % (where, name))


def parse(data: Mapping[str, Any], path: str = "<config>") -> Config:
    """Build and check a Config from a parsed TOML document."""
    errors: List[str] = []
    known = {"gcp", "limits", "local", "slurm", "prices"}
    errors += ["unknown section [%s]" % k for k in sorted(set(data) - known)]
    config = Config(path=path)
    limits = _take(data.get("limits", {}), Limits, "limits", errors)
    if limits:
        _check_types(limits, "limits", errors)
        if isinstance(limits.max_retries, int) and not 0 <= limits.max_retries <= MAX_RETRIES_LIMIT:
            errors.append("limits.max_retries is 0 to %d" % MAX_RETRIES_LIMIT)
        if all(
            isinstance(v, (int, float))
            for v in (limits.confirm_usd, limits.refuse_usd, limits.hard_refuse_usd)
        ) and not (0 <= limits.confirm_usd <= limits.refuse_usd <= limits.hard_refuse_usd):
            errors.append("limits: confirm_usd <= refuse_usd <= hard_refuse_usd")
        for name in ("max_tasks", "max_parallel_vcpus", "max_task_wall_s"):
            value = getattr(limits, name)
            if isinstance(value, int) and value < 1:
                errors.append("limits.%s is at least 1" % name)
        config.limits = limits
    local = _take(data.get("local", {}), Local, "local", errors)
    if local:
        _check_types(local, "local", errors)
        if isinstance(local.workers, int) and not 1 <= local.workers <= 64:
            errors.append("local.workers is 1 to 64")
        if isinstance(local.nice, int) and not 0 <= local.nice <= 19:
            errors.append("local.nice is 0 to 19")
        config.local = local
    if "gcp" in data:
        table = dict(data["gcp"])
        ranking = table.pop("ranking", [])
        gcp = _take(table, Gcp, "gcp", errors)
        if gcp:
            _check_types(gcp, "gcp", errors)
            gcp.ranking = []
            for pair in ranking if isinstance(ranking, list) else [None]:
                if not (
                    isinstance(pair, (list, tuple))
                    and len(pair) == 2
                    and all(isinstance(x, str) for x in pair)
                ):
                    errors.append("gcp.ranking is a list of [family, region] pairs")
                    break
                gcp.ranking.append((pair[0], pair[1]))
            if isinstance(gcp.project, str) and not PROJECT_RE.match(gcp.project):
                errors.append("gcp.project %r is not a project id" % gcp.project)
            regions = gcp.regions if isinstance(gcp.regions, list) else []
            if not regions or not all(isinstance(r, str) and REGION_RE.match(r) for r in regions):
                errors.append("gcp.regions is a non-empty list of regions")
            if gcp.home_region not in regions:
                errors.append("gcp.home_region is one of gcp.regions")
            for name in ("inputs_bucket", "runs_bucket"):
                value = getattr(gcp, name)
                if isinstance(value, str) and not BUCKET_RE.match(value):
                    errors.append("gcp.%s %r is not a bucket name" % (name, value))
            if gcp.inputs_bucket == gcp.runs_bucket:
                errors.append("gcp.inputs_bucket and gcp.runs_bucket are two buckets")
            for name in ("submit_service_account", "runner_service_account"):
                value = getattr(gcp, name)
                if isinstance(value, str) and not ACCOUNT_ID_RE.match(value):
                    errors.append("gcp.%s is an account id (the part before the @)" % name)
            for _, region in gcp.ranking:
                if region not in regions:
                    errors.append("gcp.ranking names %r, which is not in gcp.regions" % region)
            config.gcp = gcp
    slurm = data.get("slurm", {})
    if not isinstance(slurm, dict):
        errors.append("[slurm.<site>] tables")
    else:
        for name, table in slurm.items():
            if not SITE_RE.match(name) or not isinstance(table, dict):
                errors.append("slurm site %r: a lower-case name and a table" % name)
                continue
            site = _take(table, SlurmSite, "slurm.%s" % name, errors, name=name)
            if site:
                _check_types(site, "slurm.%s" % name, errors)
                if isinstance(site.chunk, int) and site.chunk < 1:
                    errors.append("slurm.%s.chunk is at least 1" % name)
                if isinstance(site.max_array, int) and site.max_array < 1:
                    errors.append("slurm.%s.max_array is at least 1" % name)
                if isinstance(site.slots, int) and site.slots < 1:
                    errors.append("slurm.%s.slots is at least 1" % name)
                if isinstance(site.speed, (int, float)) and not 0 < site.speed <= 4:
                    errors.append("slurm.%s.speed is above 0 (1.0: the reference core)" % name)
                config.slurm[name] = site
    prices = data.get("prices", {})
    if isinstance(prices, dict):
        for key in sorted(set(prices) - {"table", "calibration"}):
            errors.append("unknown key prices.%s" % key)
        config.price_table = prices.get("table")
        config.calibration = prices.get("calibration")
    if errors:
        raise ConfigError(path, errors)
    return config


def config_path(explicit: Optional[str] = None) -> Path:
    raw = explicit or os.environ.get(ENV_CONFIG) or DEFAULT_PATH
    return Path(os.path.expanduser(raw))


def load(explicit: Optional[str] = None) -> Config:
    """Load the owner configuration; a missing default file gives the defaults."""
    path = config_path(explicit)
    if not path.is_file():
        if explicit or os.environ.get(ENV_CONFIG):
            raise ConfigError(str(path), ["no such file"])
        return Config(path=None)
    try:
        import tomllib
    except ImportError as err:  # Python < 3.11
        raise ConfigError(str(path), ["reading TOML needs Python 3.11 or later"]) from err
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as err:
            raise ConfigError(str(path), [str(err)]) from err
    return parse(data, str(path))
