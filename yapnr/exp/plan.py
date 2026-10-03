"""Planning: a campaign file becomes a plan directory (tasks, bundles, placements, estimate).

``make_plan`` reads a ``yapnr-campaign-v1`` file, pins the image to a digest, archives the source
commit (and any data inputs) into content-addressed bundles, expands the matrix into
``yapnr-task-v1`` records through the campaign's kind, groups them into resource classes (one
Batch job or Slurm array each), places every class (family, region and shape on Google Cloud),
prices it, and writes::

    <plan>/campaign.json     the plan (uploaded to the runs store; no readable spec if private)
    <plan>/tasks.jsonl       one canonical task per line
    <plan>/task.py           the in-task wrapper every backend runs (its sha256 is in campaign.json)
    <plan>/bundles/          <sha256>.tar.gz inputs
    <plan>/campaign.toml     the campaign file as given (local only)
    <plan>/private-ids.json  opaque task id -> its labels (private campaigns; local only)
    <plan>/backend/<name>/   the rendered artefacts for a first submission, for review

Nothing here talks to a cloud: the only network access is the registry lookup of an image tag,
which ``--offline`` (with ``--image-digest``) avoids.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from yapnr.exp import bundle, cost, image, kinds, spec
from yapnr.exp.config import Config

PLAN_SCHEMA = "yapnr-campaign-plan-v1"
BACKENDS = ("local", "gcp-batch", "slurm")
WRAPPER = Path(__file__).resolve().parent / "task.py"
DEFAULT_FAMILIES = ("c4d", "c3d", "t2d")


class PlanError(ValueError):
    pass


def load_campaign(path: Path) -> Dict[str, Any]:
    try:
        import tomllib
    except ImportError as err:  # Python < 3.11
        raise PlanError("reading campaign files needs Python 3.11 or later") from err
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise PlanError("%s: %s" % (path, err)) from err
    errors = spec.campaign_errors(data)
    if not errors:
        try:
            errors = kinds.get(data["kind"]).check(data)
        except ValueError as err:
            errors = [str(err)]
    if errors:
        raise spec.SpecError(str(path), errors)
    return data


@dataclass
class ResourceClass:
    name: str
    image: str
    cpus: int
    memory_gb: float
    disk_gb: float
    max_wall_s: int
    lines: List[int] = field(default_factory=list)
    reference_s: List[float] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "image": self.image,
            "cpus": self.cpus,
            "memory_gb": self.memory_gb,
            "disk_gb": self.disk_gb,
            "max_wall_s": self.max_wall_s,
            "lines": self.lines,
            "reference_s": [round(s, 1) for s in self.reference_s],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ResourceClass":
        return cls(**dict(data))


def _class_name(cpus: int, memory_gb: float, index: int, count: int) -> str:
    name = "c%dm%s" % (cpus, ("%g" % memory_gb).replace(".", "p"))
    return name if count == 1 else "%s-%d" % (name, index)


def resource_classes(tasks: Sequence[Mapping[str, Any]], reference: Sequence[float]):
    """Tasks grouped by (image, cores, memory, disk), in order of first appearance."""
    groups: Dict[Tuple[Any, ...], List[int]] = {}
    for line, task in enumerate(tasks):
        r = task["resources"]
        key = (task["image"], r["cpus"], r["memory_gb"], r["disk_gb"])
        groups.setdefault(key, []).append(line)
    names: Dict[Tuple[int, float], int] = {}
    for key in groups:
        names[(key[1], key[2])] = names.get((key[1], key[2]), 0) + 1
    seen: Dict[Tuple[int, float], int] = {}
    classes = []
    for key, lines in groups.items():
        short = (key[1], key[2])
        seen[short] = seen.get(short, 0) + 1
        classes.append(
            ResourceClass(
                name=_class_name(key[1], key[2], seen[short], names[short]),
                image=key[0],
                cpus=int(key[1]),
                memory_gb=float(key[2]),
                disk_gb=float(key[3]),
                max_wall_s=max(int(tasks[i]["resources"]["max_wall_s"]) for i in lines),
                lines=lines,
                reference_s=[reference[i] for i in lines],
            )
        )
    return classes


def campaign_id(kind_short: str, private: bool, digest_input: Any, today: _dt.date) -> str:
    digest = spec.sha256_hex(spec.canonical_json(digest_input))[:6]
    return "%s-%s-%s" % (today.strftime("%Y%m%d"), "p" if private else kind_short, digest)


def _repo_root(explicit: Optional[Path]) -> Optional[Path]:
    for candidate in (explicit, os.environ.get("BUILD_WORKSPACE_DIRECTORY"), Path.cwd()):
        if not candidate:
            continue
        try:
            return bundle.repo_root(Path(candidate))
        except bundle.BundleError:
            continue
    return None


def gcp_placements(
    classes: Sequence[ResourceClass],
    campaign: Mapping[str, Any],
    config: Config,
    table: cost.PriceTable,
    calibration: cost.Calibration,
    *,
    determinism: str = "seeded",
    region: Optional[str] = None,
    shape: Optional[str] = None,
    families: Optional[Sequence[str]] = None,
    template: Optional[bool] = None,
) -> Dict[str, cost.Placement]:
    """A (family, region, shape) per class; one machine type for wall-clock-budgeted campaigns."""
    gcp = config.require_gcp()
    placement = dict(campaign.get("placement", {}))
    if template is not None:
        placement["template"] = template
    if shape:
        placement["shape"] = shape
    if region:
        placement["region"] = region
    regions = [placement["region"]] if placement.get("region") else list(gcp.regions)
    for name in regions:
        if name not in gcp.regions:
            raise PlanError("region %s is not one of gcp.regions in the owner config" % name)
    chosen = list(families or placement.get("families") or DEFAULT_FAMILIES)
    vm_vcpus = int(placement.get("vm_vcpus", 16))
    if placement.get("shape"):
        parts = placement["shape"].split("-")
        if len(parts) != 3 or not parts[2].isdigit():
            raise PlanError(
                "placement.shape %r is not <family>-<type>-<vcpus>" % placement["shape"]
            )
        chosen, vm_vcpus = [parts[0]], int(parts[2])
    model = "spot" if placement.get("spot", True) else "standard"
    packing = placement.get("packing", "core")
    one_type = determinism == "wall_clock_budgeted"
    # Wall-clock-budgeted search does the most work per budget on the fastest core, so those
    # campaigns take the first listed family that fits; the others the cheapest per result.
    prefer = placement.get("prefer") or ("first-family" if one_type else "cost")
    out: Dict[str, cost.Placement] = {}
    first: Optional[cost.Placement] = None
    order = sorted(classes, key=lambda c: -len(c.lines))
    for cls in order:
        pairs = gcp.ranking
        allowed_families, allowed_regions = chosen, regions
        if one_type and first is not None:
            allowed_families, allowed_regions, pairs = [first.family], [first.region], []
        try:
            p = cost.place(
                table,
                calibration,
                cpus=cls.cpus,
                memory_gb=cls.memory_gb,
                families=allowed_families,
                regions=allowed_regions,
                ranking=pairs,
                vm_vcpus=vm_vcpus,
                packing=packing,
                model=model,
                template_families=gcp.template_families,
                prefer=prefer,
            )
        except cost.CostError as err:
            raise PlanError("class %s: %s" % (cls.name, err)) from err
        if placement.get("shape") and p.shape != placement["shape"]:
            raise PlanError(
                "class %s needs %s, not the requested %s" % (cls.name, p.shape, placement["shape"])
            )
        if one_type and first is not None and p.shape != first.shape:
            raise PlanError(
                "a wall-clock-budgeted campaign runs on one machine type, but class %s needs %s "
                "and class %s %s; give the tasks the same resources"
                % (
                    cls.name,
                    p.shape,
                    order[0].name,
                    first.shape,
                )
            )
        if placement.get("template") is False:
            p.template = False
        first = first or p
        out[cls.name] = p
    return out


def estimate(
    backend: str,
    classes: Sequence[ResourceClass],
    placements: Mapping[str, cost.Placement],
    config: Config,
    table: Optional[cost.PriceTable],
    *,
    site=None,
    subset: Optional[Mapping[str, Sequence[int]]] = None,
) -> cost.Estimate:
    """The estimate of ``classes`` (or of the lines in ``subset`` per class)."""
    picked = []
    for cls in classes:
        lines = list(subset.get(cls.name, [])) if subset is not None else cls.lines
        if not lines:
            continue
        reference = [cls.reference_s[cls.lines.index(i)] for i in lines]
        picked.append((cls, lines, reference))
    if backend == "gcp-batch":
        limits = config.limits
        rows = []
        for cls, lines, reference in picked:
            p = placements[cls.name]
            parallel = cost.parallel_tasks(len(lines), p, limits.max_parallel_vcpus)
            # The longest one Batch attempt may run is maxRunDuration, not the task's own limit.
            attempt_s = float(cls.max_wall_s + cost.BATCH_ATTEMPT_GRACE_S)
            rows.append((cls.name, p, reference, [attempt_s] * len(lines), parallel))
        return cost.estimate_gcp(
            table,
            rows,
            max_retries=limits.max_retries,
            max_parallel_vcpus=limits.max_parallel_vcpus,
            max_campaign_hours=limits.max_campaign_hours,
        )
    reference = [s for _, _, ref in picked for s in ref]
    if backend == "local":
        return cost.estimate_local(reference, config.local.workers)
    cpus = [cls.cpus for cls, lines, _ in picked for _ in lines]
    return cost.estimate_slurm(reference, cpus, site.max_concurrent if site else 1)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def make_plan(
    campaign_path: Path,
    backend: str,
    config: Config,
    *,
    out: Optional[Path] = None,
    repo: Optional[Path] = None,
    site: Optional[str] = None,
    offline: bool = False,
    image_digest: Optional[str] = None,
    allow_dirty: bool = False,
    region: Optional[str] = None,
    shape: Optional[str] = None,
    families: Optional[Sequence[str]] = None,
    template: Optional[bool] = None,
    replace: bool = False,
    today: Optional[_dt.date] = None,
    opener: Optional[Callable] = None,
    price_table: Optional[cost.PriceTable] = None,
    calibration: Optional[cost.Calibration] = None,
) -> "Plan":
    if backend not in BACKENDS:
        raise PlanError("backend is one of %s" % ", ".join(BACKENDS))
    campaign_path = Path(campaign_path).resolve()
    campaign = load_campaign(campaign_path)
    kind = kinds.get(campaign["kind"])
    private = campaign.get("visibility", "public") == "private"
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    table = price_table or cost.PriceTable.load(config.price_table)
    calibration = calibration or cost.Calibration.load(config.calibration)
    site_config = config.require_site(site) if backend == "slurm" else None
    if site_config is not None and private and not site_config.private_ok:
        raise PlanError(
            "Slurm site %s is not marked private_ok; private campaigns stay off it"
            % site_config.name
        )

    # The image, pinned by digest (the local backend runs the host toolchain, but records it).
    image_text = campaign.get("image", "edge")
    try:
        pinned = image.pin(
            image_text,
            image_digest,
            offline=offline or backend == "local" and not image_digest,
            **({"opener": opener} if opener else {}),
        )
        image_ref = pinned.pinned
    except image.ImageError as err:
        if backend != "local":
            raise PlanError(str(err)) from err
        image_ref = str(image.parse(image_text))
        pinned = None

    # The source bundle: one commit (or the working tree with --allow-dirty).
    source = campaign.get("source", "HEAD")
    source_meta: Optional[Dict[str, Any]] = None
    source_input = None
    root = None
    if kind.source_paths != () and source != "none":
        root = _repo_root(repo)
        if root is None:
            raise PlanError("the source bundle needs a git checkout (--repo)")
        paths = list(kind.source_paths) if kind.source_paths else None
        try:
            commit, dirty = bundle.resolve_source(root, source, paths)
        except bundle.BundleError as err:
            raise PlanError(str(err)) from err
        if dirty and not allow_dirty:
            raise PlanError(
                "the checkout has uncommitted changes under %s; commit them or pass --allow-dirty"
                % ", ".join(paths or ["."])
            )
        source_meta = {"commit": commit, "dirty": bool(dirty), "paths": paths}
    digest_input = {
        "campaign": campaign,
        "backend": backend,
        "site": site_config.name if site_config else None,
        "source": source_meta,
        "image": image_ref,
        "overrides": {
            "region": region,
            "shape": shape,
            "families": list(families or []),
            "template": template,
        },
    }
    cid = campaign_id(kind.short, private, digest_input, today)
    if out is None:
        out = config.local.store_path(private) / "plans" / cid
    out = Path(out)
    if out.exists():
        if not replace:
            raise PlanError("%s exists (pass --replace to plan again)" % out)
        shutil.rmtree(out)
    bundle_dir = out / "bundles"
    try:
        if source_meta is not None:
            digest, _ = bundle.source_bundle(
                root, source_meta["commit"], source_meta["paths"], bundle_dir, bool(dirty)
            )
            source_meta["bundle"] = digest
            source_input = {"dest": "src", "bundle": digest, "kind": "source"}
        data_inputs = []
        for item in campaign.get("inputs", []):
            input_kind = "private" if private else item.get("kind", "data")
            path = (campaign_path.parent / item["path"]).resolve()
            digest, _ = bundle.data_bundle(path, bundle_dir)
            data_inputs.append({"dest": item["dest"], "bundle": digest, "kind": input_kind})
        ctx = kinds.Context(
            campaign_id=cid,
            image=image_ref,
            visibility="private" if private else "public",
            determinism=campaign.get("determinism", kind.default_determinism),
            resources=dict(kind.default_resources, **campaign.get("resources", {})),
            source_input=source_input,
            data_inputs=data_inputs,
            base_dir=campaign_path.parent,
            bundle_dir=bundle_dir,
            repo=root,
        )
        tasks = kind.expand(campaign, ctx)
    except (bundle.BundleError, ValueError, OSError) as err:
        shutil.rmtree(out, ignore_errors=True)
        raise PlanError(str(err)) from err
    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        shutil.rmtree(out, ignore_errors=True)
        raise PlanError("the campaign expands to duplicate task ids")
    if not tasks:
        shutil.rmtree(out, ignore_errors=True)
        raise PlanError("the campaign expands to no tasks")

    reference = []
    for task in tasks:
        key = task["labels"].get("case") or task["labels"].get("rung") or ""
        measured = calibration.reference_seconds(task["kind"], key)
        reference.append(measured if measured is not None else kind.reference_seconds(task))
    classes = resource_classes(tasks, reference)

    placements: Dict[str, cost.Placement] = {}
    if backend == "gcp-batch":
        placements = gcp_placements(
            classes,
            campaign,
            config,
            table,
            calibration,
            determinism=ctx.determinism,
            region=region,
            shape=shape,
            families=families,
            template=template,
        )
    est = estimate(backend, classes, placements, config, table, site=site_config)
    caps = None
    if backend == "gcp-batch":
        check = cost.check_caps(
            est,
            config.limits,
            tasks=len(tasks),
            max_wall_s=max(c.max_wall_s for c in classes),
        )
        caps = {"refusals": check.refusals, "confirm": check.confirm, "limit_usd": check.limit_usd}

    wrapper = WRAPPER.read_bytes()
    task_lines = [spec.canonical_json(t).decode() for t in tasks]
    # Private campaigns: the opaque task ids and what they stand for stay in the local plan.
    readable = {t["id"]: t["labels"] for t in tasks} if private else {}
    meta: Dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "id": cid,
        "kind": campaign["kind"],
        "name": campaign.get("name"),
        "visibility": "private" if private else "public",
        "determinism": ctx.determinism,
        "created": today.isoformat(),
        "spec": None if private else campaign,
        "spec_sha256": spec.sha256_hex(spec.canonical_json(campaign)),
        "source": source_meta,
        "image": {
            "ref": image_ref,
            "requested": image_text,
            "digest": pinned.digest if pinned else None,
            "repository": pinned.name if pinned else None,
        },
        "bundles": sorted({i["bundle"] for t in tasks for i in t["inputs"]}),
        "task_count": len(tasks),
        "task_hashes": {t["id"]: spec.spec_hash(t) for t in tasks},
        "wrapper": {"sha256": spec.sha256_hex(wrapper)},
        "classes": [c.to_json() for c in classes],
        "backend": {"name": backend},
        "placements": {k: v.to_json() for k, v in placements.items()},
        "estimate": est.to_json(),
        "caps": caps,
        "prices": (
            {"table": table.path, "accessed": table.accessed} if backend == "gcp-batch" else None
        ),
        "calibration": calibration.path,
    }
    if backend == "local":
        meta["backend"].update(
            workers=config.local.workers,
            nice=config.local.nice,
            store=str(config.local.store_path(private)),
        )
    if site_config is not None:
        chunk = max(site_config.chunk, 1)
        for cls in classes:
            chunk = max(chunk, math.ceil(len(cls.lines) / site_config.max_array))
        meta["backend"].update(site=site_config.name, chunk=chunk)
    if backend == "gcp-batch":
        meta["backend"].update(home_region=config.require_gcp().home_region)

    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "campaign.json", meta)
    (out / "tasks.jsonl").write_text("".join(line + "\n" for line in task_lines))
    (out / "task.py").write_bytes(wrapper)
    shutil.copyfile(campaign_path, out / "campaign.toml")
    if private:
        _write_json(out / "private-ids.json", readable)
    plan = Plan(out)
    from yapnr.exp import backends

    backends.get(backend).preview(plan, config)
    return plan


class Plan:
    """A plan directory, as ``make_plan`` wrote it."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        try:
            self.meta = json.loads((self.dir / "campaign.json").read_text())
            self.lines = (self.dir / "tasks.jsonl").read_text().splitlines()
        except (OSError, ValueError) as err:
            raise PlanError("%s is not a plan directory: %s" % (directory, err)) from err
        if self.meta.get("schema") != PLAN_SCHEMA:
            raise PlanError("%s: unknown plan schema %r" % (directory, self.meta.get("schema")))
        self.tasks = [json.loads(line) for line in self.lines]
        self.classes = [ResourceClass.from_json(c) for c in self.meta["classes"]]

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def backend(self) -> str:
        return self.meta["backend"]["name"]

    @property
    def private(self) -> bool:
        return self.meta["visibility"] == "private"

    def placement(self, name: str) -> cost.Placement:
        data = dict(self.meta["placements"][name])
        data.pop("vm_hour_usd", None)
        return cost.Placement(**data)

    def placements(self) -> Dict[str, cost.Placement]:
        return {name: self.placement(name) for name in self.meta.get("placements", {})}

    def check(self) -> List[str]:
        """Integrity findings: every task matches its hash, the wrapper its sha256, bundles exist."""
        errors = []
        hashes = self.meta["task_hashes"]
        for task in self.tasks:
            if spec.task_errors(task):
                errors.append("task %s is not a valid task" % task.get("id"))
            elif hashes.get(task["id"]) != spec.spec_hash(task):
                errors.append("task %s does not match its hash" % task["id"])
        if len(self.tasks) != self.meta["task_count"]:
            errors.append(
                "tasks.jsonl has %d tasks, campaign.json %d"
                % (len(self.tasks), self.meta["task_count"])
            )
        wrapper = self.dir / "task.py"
        if (
            not wrapper.is_file()
            or spec.sha256_hex(wrapper.read_bytes()) != self.meta["wrapper"]["sha256"]
        ):
            errors.append("task.py does not match campaign.json")
        for digest in self.meta["bundles"]:
            path = self.dir / "bundles" / ("%s.tar.gz" % digest)
            if not path.is_file():
                errors.append("bundle %s is missing" % digest[:12])
        return errors

    def bundle_path(self, digest: str) -> Path:
        return self.dir / "bundles" / ("%s.tar.gz" % digest)

    def class_of(self, line: int) -> ResourceClass:
        for cls in self.classes:
            if line in cls.lines:
                return cls
        raise KeyError(line)
