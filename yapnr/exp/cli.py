"""``yapnr exp``: plan, submit, watch, fetch and cancel experiment campaigns.

    yapnr exp plan CAMPAIGN.toml --backend local|gcp-batch|slurm [--site S] [--offline ...]
    yapnr exp submit PLAN [--yes] [--max-usd X] [--dry-run] [--only TASK ...] [--wait]
    yapnr exp status PLAN [--json]
    yapnr exp logs PLAN [TASK] [--limit N]
    yapnr exp fetch PLAN [--full] [--into DIR] [--from DIR] [--allow-mixed]
    yapnr exp cancel PLAN [--submission N] [--dry-run]
    yapnr exp doctor --backend B [--image-digest D]
    yapnr exp prices [--refresh] [--rerank] [--family F ...]
    yapnr exp unfreeze --backend gcp-batch|local
    yapnr exp calibration ingest --reference DIR --cloud DIR ... --out FILE

``PLAN`` is a plan directory (``plan`` prints it) or a campaign id planned on this machine. Only
``submit`` (without ``--dry-run``), ``cancel`` and ``unfreeze`` change anything in a cloud; the
guide is docs/cloud-experiments.md and the design docs/design/cloud-experiments.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

PLAN_HELP = "a plan directory, or a campaign id planned on this machine"


def _config(args):
    from yapnr.exp import config

    return config.load(args.config)


def _find_plan(text: str, cfg):
    from yapnr.exp.plan import Plan, PlanError

    path = Path(text).expanduser()
    if path.is_dir():
        return Plan(path)
    for private in (False, True):
        candidate = cfg.local.store_path(private) / "plans" / text
        if candidate.is_dir():
            return Plan(candidate)
    raise PlanError("no plan directory or planned campaign %r" % text)


def _cloud(cfg, plan, dry_run=False):
    if plan.backend != "gcp-batch":
        return None
    from yapnr.exp.backends.gcp_batch import make_cloud

    return make_cloud(cfg, dry_run)


def _money(value: float) -> str:
    return "$%.2f" % value


def _cmd_plan(args) -> int:
    from yapnr.exp import plan as planning

    cfg = _config(args)
    result = planning.make_plan(
        Path(args.campaign),
        args.backend,
        cfg,
        out=Path(args.out) if args.out else None,
        repo=Path(args.repo) if args.repo else None,
        site=args.site,
        offline=args.offline,
        image_digest=args.image_digest,
        allow_dirty=args.allow_dirty,
        region=args.region,
        shape=args.shape,
        families=args.family,
        template=False if args.no_template else None,
        replace=args.replace,
    )
    meta = result.meta
    est = meta["estimate"]
    print("campaign  %s (%s, %s)" % (meta["id"], meta["kind"], meta["visibility"]))
    print("plan      %s" % result.dir)
    print("image     %s" % meta["image"]["ref"])
    if meta["source"]:
        src = meta["source"]
        print("source    %s%s" % (src["commit"][:12], " (dirty)" if src["dirty"] else ""))
    print("tasks     %d in %d class(es)" % (meta["task_count"], len(meta["classes"])))
    for cls in meta["classes"]:
        line = "  %-12s %4d tasks  %d core(s), %g GB, max %d s" % (
            cls["name"],
            len(cls["lines"]),
            cls["cpus"],
            cls["memory_gb"],
            cls["max_wall_s"],
        )
        placement = meta["placements"].get(cls["name"])
        if placement:
            line += "  -> %s %s in %s, %d per VM%s" % (
                placement["shape"],
                placement["model"],
                placement["region"],
                placement["tasks_per_vm"],
                " (template)" if placement["template"] else "",
            )
        print(line)
    if args.backend == "gcp-batch":
        print(
            "estimate  expected %s, ceiling %s, about %.1f h"
            % (_money(est["expected_usd"]), _money(est["ceiling_usd"]), est["makespan_h"])
        )
        caps = meta["caps"] or {}
        for refusal in caps.get("refusals", []):
            print("REFUSED   %s" % refusal)
        if caps.get("confirm"):
            print("confirm   submit will ask for confirmation (above limits.confirm_usd)")
    else:
        print("estimate  %.1f core-hours, about %.1f h" % (est["core_hours"], est["makespan_h"]))
    for note in est.get("assumptions", []):
        print("  - %s" % note)
    print("review    %s" % (result.dir / "backend" / args.backend))
    print(
        "next      yapnr exp submit %s%s"
        % (result.dir, " --dry-run" if args.backend == "gcp-batch" else "")
    )
    return 0


def _cmd_submit(args) -> int:
    from yapnr.exp import backends

    cfg = _config(args)
    plan = _find_plan(args.plan, cfg)
    backend = backends.get(plan.backend)
    if plan.backend == "local":
        backend.wait = args.wait
    cloud = _cloud(cfg, plan, args.dry_run)
    done = backend.submit(
        plan,
        cfg,
        yes=args.yes,
        max_usd=args.max_usd,
        dry_run=args.dry_run,
        only=args.only,
        cloud=cloud,
    )
    if args.dry_run and cloud is not None:
        print("\n# the gcloud calls a submit would make:")
        print(cloud.transcript())
    return 0 if done is not None else 1


def _cmd_status(args) -> int:
    from yapnr.exp import backends
    from yapnr.exp.backends.base import status_rows

    cfg = _config(args)
    plan = _find_plan(args.plan, cfg)
    backend = backends.get(plan.backend)
    cloud = _cloud(cfg, plan)
    stores = backend.stores(plan, cfg, cloud)
    done = backends.done_markers(stores.runs, plan.id)
    rows = status_rows(plan.meta, plan.tasks, done)
    subs = backends.submissions(stores.runs, plan.id)
    views = []
    for record in subs:
        view = backend.state(plan.meta, record, cfg, cloud)
        views.append(
            {"submission": record.get("submission"), "class": record.get("class"), "view": view}
        )
    frozen = stores.runs.exists("control/frozen")
    report = dict(rows, campaign=plan.id, backend=plan.backend, frozen=frozen, submissions=views)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    verdicts = ", ".join("%s %d" % kv for kv in sorted(rows["verdicts"].items())) or "none"
    print("campaign %s  %s  frozen %s" % (plan.id, plan.backend, "YES" if frozen else "no"))
    print(
        "tasks %d   done %d (%s)   pending %d"
        % (rows["tasks"], rows["done"], verdicts, rows["pending"])
    )
    for item in views:
        view = item["view"] or {}
        counts = ", ".join("%s %s" % kv for kv in sorted((view.get("counts") or {}).items()))
        print(
            "  submission %s  %-10s %s %s%s"
            % (
                item["submission"],
                item["class"],
                view.get("state", "-"),
                counts,
                "  preemptions %d" % view["preemptions"] if view.get("preemptions") else "",
            )
        )
    est = plan.meta["estimate"]
    if plan.backend == "gcp-batch":
        print(
            "estimate %s (ceiling %s)" % (_money(est["expected_usd"]), _money(est["ceiling_usd"]))
        )
    return 0


def _cmd_logs(args) -> int:
    from yapnr.exp import backends
    from yapnr.exp.backends.base import task_key

    cfg = _config(args)
    plan = _find_plan(args.plan, cfg)
    backend = backends.get(plan.backend)
    cloud = _cloud(cfg, plan)
    stores = backend.stores(plan, cfg, cloud)
    done = backends.done_markers(stores.runs, plan.id)
    tasks = [t for t in plan.tasks if not args.task or t["id"] == args.task]
    for task in tasks:
        marker = done.get(task["id"])
        if marker:
            rel = "campaigns/%s/tasks/%s/%s/log.tail" % (
                plan.id,
                task_key(task["id"]),
                marker["attempt"],
            )
            print("== %s (%s)" % (task["id"], marker.get("verdict")))
            print(stores.runs.read_text(rel) if stores.runs.exists(rel) else "(no log tail)")
        elif plan.backend == "gcp-batch" and args.task:
            for record in backends.submissions(stores.runs, plan.id):
                indices = stores.runs.read_text(
                    "campaigns/%s/submissions/%d.indices" % (plan.id, record["submission"])
                ).split()
                line = plan.tasks.index(task)
                if str(line) in indices:
                    print(
                        "== %s (Cloud Logging, submission %d)" % (task["id"], record["submission"])
                    )
                    for text in backend.logs(
                        record, cfg, cloud, indices.index(str(line)), args.limit
                    ):
                        print(text)
    return 0


def _cmd_fetch(args) -> int:
    from yapnr.exp import backends, fetch
    from yapnr.exp.store import LocalStore

    cfg = _config(args)
    plan = _find_plan(args.plan, cfg)
    if args.source:
        runs = LocalStore(Path(args.source).expanduser())
    else:
        runs = backends.get(plan.backend).stores(plan, cfg, _cloud(cfg, plan)).runs
    dest = (
        Path(args.into).expanduser()
        if args.into
        else cfg.local.store_path(plan.private) / "fetched"
    )
    report = fetch.fetch(runs, plan.id, dest, cfg, full=args.full, allow_mixed=args.allow_mixed)
    print("fetched %d of %d task(s) into %s" % (report["done"], report["tasks"], dest / plan.id))
    assembled = report.get("assembled") or {}
    for path in assembled.get("paths", []):
        print("assembled %s" % path)
    for warning in assembled.get("warnings", []):
        print("warning   %s" % warning)
    if report["missing"]:
        print("pending   %d task(s) without a result" % len(report["missing"]))
    return 0


def _cmd_cancel(args) -> int:
    from yapnr.exp import backends
    from yapnr.exp.backends.base import local_records

    cfg = _config(args)
    plan = _find_plan(args.plan, cfg)
    backend = backends.get(plan.backend)
    cloud = _cloud(cfg, plan, args.dry_run)
    if args.dry_run:
        records = local_records(plan)  # a dry run reads nothing remote
    else:
        records = backends.submissions(backend.stores(plan, cfg, cloud).runs, plan.id)
    for record in records:
        if args.submission and record.get("submission") != args.submission:
            continue
        print(backend.cancel(record, cfg, cloud, args.dry_run))
    if args.dry_run and cloud is not None:
        print(cloud.transcript())
    return 0


def _cmd_doctor(args) -> int:
    cfg = _config(args)
    if args.backend == "gcp-batch":
        from yapnr.exp.backends.gcp_batch import doctor, make_cloud

        checks = doctor(cfg, make_cloud(cfg), args.image_digest)
    elif args.backend == "local":
        from yapnr.exp.backends.local import toolchain_profile

        profile = toolchain_profile(cfg)
        checks = [
            {
                "check": "toolchain %s" % k,
                "ok": bool(profile[k] and Path(profile[k]).exists()),
                "detail": profile[k] or "unset",
            }
            for k in ("PYTHON", "KICAD_CLI", "KICAD_PYTHON", "FOOTPRINTS")
        ]
        store = cfg.local.store_path()
        checks.append({"check": "store", "ok": True, "detail": str(store)})
    else:
        site = cfg.require_site(args.site)
        checks = [
            {
                "check": "site %s" % site.name,
                "ok": True,
                "detail": "store %s, sif %s" % (site.store, site.sif),
            }
        ]
    failed = 0
    for check in checks:
        failed += not check["ok"]
        print(
            "%-4s %s%s"
            % (
                "ok" if check["ok"] else "FAIL",
                check["check"],
                ("  " + check["detail"]) if check["detail"] else "",
            )
        )
    return 1 if failed else 0


def _cmd_prices(args) -> int:
    from yapnr.exp import cost, prices

    cfg = _config(args)
    table = cost.PriceTable.load(cfg.price_table)
    calibration = cost.Calibration.load(cfg.calibration)
    if args.refresh:
        gcp = cfg.require_gcp()
        skus = prices.fetch_skus(prices.api_key(gcp.price_api_key_command))
        data, changes = prices.refresh(table, skus)
        for family, region, old, new in changes[:40]:
            print("%-5s %-26s %s -> %s" % (family, region, old, new))
        if len(changes) > 40:
            print("... %d more changes" % (len(changes) - 40))
        target = prices.write(data, Path(args.out or cfg.price_table or prices.DEFAULT_CACHE))
        print("wrote %s (set [prices] table to use it)" % target)
        table = cost.PriceTable(data, str(target))
    families = args.family or ["c4d", "c4", "c3d", "t2d", "c4a", "n2d", "n4", "e2"]
    regions = (
        cfg.gcp.regions
        if cfg.gcp
        else sorted({r for f in families for r in table.family(f).get("spot", {})})
    )
    print(
        "prices of %s (%s); speed: %s"
        % (table.accessed, table.path, calibration.path or "PassMark estimates")
    )
    for value, family, region, shape in prices.rank(table, calibration, families, regions)[
        : args.top
    ]:
        print("  $%.5f per reference-core-hour  %-5s %-26s %s" % (value, family, region, shape))
    if args.rerank:
        print("record the top pairs as [gcp] ranking in the owner config")
    return 0


def _cmd_unfreeze(args) -> int:
    from yapnr.exp.store import FROZEN, GcsStore, LocalStore

    cfg = _config(args)
    if args.backend == "gcp-batch":
        from yapnr.exp.backends.gcp_batch import make_cloud

        store = GcsStore(cfg.require_gcp().runs_bucket, make_cloud(cfg))
    else:
        store = LocalStore(cfg.local.store_path())
    if not store.exists(FROZEN):
        print("not frozen")
        return 0
    # On gcp-batch the guard also disabled yapnr-submit; this command runs as that account, so it
    # gets here only after the owner re-enabled it (runbook, "Kill-switch drill").
    print(store.read_text(FROZEN).strip())
    if not args.yes:
        if not sys.stdin.isatty() or input("clear the freeze? [y/N] ").strip().lower() not in (
            "y",
            "yes",
        ):
            print("left frozen")
            return 1
    store.delete(FROZEN)
    print("unfrozen; a cut quota is restored with the runbook's quota command")
    return 0


def _cmd_calibration(args) -> int:
    from yapnr.exp import calibration, fetch

    reference: List[Any] = []
    for path in args.reference:
        reference += fetch.records(Path(path))
    cloud: List[Any] = []
    for path in args.cloud:
        cloud += fetch.records(Path(path))
    data = calibration.ingest(reference, cloud)
    calibration.write(data, Path(args.out))
    for key, value in sorted(data["speed"].items()):
        print("%-20s speed %.3f (%d pairs)" % (key, value, data["samples"][key]))
    load = data.get("reference_load")
    if load:
        print(
            "reference load %.2f (median 1-minute load at task start) on %s vCPUs"
            % (load["median_load_1m"], load["vcpus"] or "?")
        )
        if load["busy"]:
            print(
                "warning   the reference ran on a busy machine, so the speed factors overstate the "
                "cloud shapes; run the reference again on an idle Mac (docs/cloud-experiments.md, "
                "Calibration)"
            )
    print("wrote %s (set [prices] calibration to use it)" % args.out)
    return 0


def _run(func):
    def run(args) -> int:
        from yapnr.exp import errors

        try:
            return func(args)
        except errors.ERRORS as err:
            print("yapnr exp: %s" % err, file=sys.stderr)
            return 2

    return run


def register(commands: argparse._SubParsersAction) -> None:
    exp = commands.add_parser(
        "exp",
        help="experiment campaigns on a local pool, Google Cloud Batch or Slurm",
        description=__doc__.split("\n\n")[0],
    )
    exp.add_argument("--config", help="the owner config (default ~/.config/yapnr/cloud.toml)")
    sub = exp.add_subparsers(dest="exp_command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("plan", help="expand a campaign file into a plan directory")
    p.add_argument("campaign")
    p.add_argument("--backend", required=True, choices=["local", "gcp-batch", "slurm"])
    p.add_argument("--site", help="the Slurm site ([slurm.<site>] in the owner config)")
    p.add_argument("--out", help="the plan directory (default: <store>/plans/<campaign id>)")
    p.add_argument("--repo", help="the git checkout the source bundle comes from")
    p.add_argument("--offline", action="store_true", help="no registry lookup")
    p.add_argument("--image-digest", help="pin the image to this sha256 digest")
    p.add_argument("--allow-dirty", action="store_true", help="bundle uncommitted changes")
    p.add_argument("--region", help="pin the region (calibration)")
    p.add_argument("--shape", help="pin the machine type, e.g. c4d-highcpu-8 (calibration)")
    p.add_argument("--family", action="append", help="allowed machine families (repeatable)")
    p.add_argument(
        "--no-template",
        action="store_true",
        help="gcp-batch: an instance policy even for Hyperdisk families (the smoke job's check)",
    )
    p.add_argument("--replace", action="store_true", help="replace an existing plan directory")
    p.set_defaults(func=_run(_cmd_plan))

    p = sub.add_parser("submit", help="submit a plan's pending tasks")
    p.add_argument("plan", help=PLAN_HELP)
    p.add_argument("--yes", action="store_true", help="confirm an estimate above confirm_usd")
    p.add_argument(
        "--max-usd", type=float, help="raise refuse_usd for this submit (<= hard_refuse_usd)"
    )
    p.add_argument("--dry-run", action="store_true", help="record the calls without making them")
    p.add_argument("--only", nargs="+", metavar="TASK", help="submit only these task ids")
    p.add_argument("--wait", action="store_true", help="local: run the pool in the foreground")
    p.set_defaults(func=_run(_cmd_submit))

    p = sub.add_parser("status", help="tasks done, verdicts and the backend's view")
    p.add_argument("plan", help=PLAN_HELP)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_run(_cmd_status))

    p = sub.add_parser("logs", help="log tails of finished tasks, Cloud Logging for running ones")
    p.add_argument("plan", help=PLAN_HELP)
    p.add_argument("task", nargs="?")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=_run(_cmd_logs))

    p = sub.add_parser("fetch", help="download results and assemble the local layout")
    p.add_argument("plan", help=PLAN_HELP)
    p.add_argument("--full", action="store_true", help="also every result.tar.gz (egress costs)")
    p.add_argument("--into", help="destination (default: <store>/fetched)")
    p.add_argument("--from", dest="source", help="a local copy of the store (Slurm sites)")
    p.add_argument("--allow-mixed", action="store_true", help="assemble mixed platforms")
    p.set_defaults(func=_run(_cmd_fetch))

    p = sub.add_parser("cancel", help="cancel the campaign's jobs")
    p.add_argument("plan", help=PLAN_HELP)
    p.add_argument("--submission", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_run(_cmd_cancel))

    p = sub.add_parser("doctor", help="read-only checks of a backend's setup")
    p.add_argument("--backend", required=True, choices=["local", "gcp-batch", "slurm"])
    p.add_argument("--site")
    p.add_argument("--image-digest", help="also check the registry cache serves this digest")
    p.set_defaults(func=_run(_cmd_doctor))

    p = sub.add_parser("prices", help="rank (family, region) by price and measured speed")
    p.add_argument(
        "--refresh", action="store_true", help="read today's prices (Billing Catalog API)"
    )
    p.add_argument("--rerank", action="store_true")
    p.add_argument("--family", action="append")
    p.add_argument("--out", help="where --refresh writes the table")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=_run(_cmd_prices))

    p = sub.add_parser("unfreeze", help="clear the budget kill switch's freeze marker")
    p.add_argument("--backend", required=True, choices=["local", "gcp-batch"])
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_run(_cmd_unfreeze))

    p = sub.add_parser("calibration", help="speed factors from calibration runs")
    calib = p.add_subparsers(dest="calibration_command", metavar="<command>")
    calib.required = True
    q = calib.add_parser("ingest", help="pair reference and cloud records into a calibration file")
    q.add_argument("--reference", nargs="+", required=True, help="fetched campaign dirs (the Mac)")
    q.add_argument("--cloud", nargs="+", required=True, help="fetched campaign dirs (cloud shapes)")
    q.add_argument("--out", required=True)
    q.set_defaults(func=_run(_cmd_calibration))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="yapnr")
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["exp"] + list(argv if argv is not None else sys.argv[1:]))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
