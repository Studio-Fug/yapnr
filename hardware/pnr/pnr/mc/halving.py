"""Successive halving over complete placements.

Every stage ranks the survivors of the previous stage by that stage's own
measured objective and promotes a fixed fraction to the next, more expensive
stage. No candidate is chosen by hand; the only inputs are the stage budgets.

Stages
  0 place   N independent global starts -> legalize -> cheap/capacity proxy
  1 screen  K1 best proxies + a seeded uniform control sample of the other legal
            placements (``control=True``) -> Python detail route (signal screening)
  2 native  K2 best screens (controls compete on their screen result only), or
            with ``--promote-from place`` a seeded uniform sample of K2 legal
            placements (``sampled=True``, no screen) -> native pipeline, short budget
  3 deep    K3 best natives -> full native electrical pipeline, long budget

Each stage appends one JSON record per candidate to ``dataset.jsonl`` (pose,
stage metrics, timings) so later stages' outcomes can be regressed on earlier
features. Rank agreement between consecutive stages is reported (Spearman, only
from SPEARMAN_MIN_N pairs up; the pair count is always recorded) so the value of
each cheap stage as a predictor is measured rather than assumed.

With ``--library`` the block library is frozen once into
``out/library.snapshot.json``; every worker (and any resume of the same --out)
reads that snapshot, never the live library directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
PNR_ROOT = HERE.parents[2]  # .../hardware/pnr
SRC_ROOT = HERE.parents[4]  # repo-shaped source root (contains hardware/)
KI = "/Applications/KiCad/KiCad.app/Contents"
SPEARMAN_MIN_N = 8  # below this a rank correlation is noise: record null + n
LIBRARY_SNAPSHOT = "library.snapshot.json"


# ---------------------------------------------------------------- utilities


def _append(path: Path, record: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _process_pool(n):
    """Stage 0/1 executor (a seam so tests can run the driver in-process)."""
    return cf.ProcessPoolExecutor(n)


def _loadavg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _jsonable(value):
    """Plain JSON types only: tuples/sets -> lists, paths -> str, numpy -> python."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "tolist"):  # numpy scalar / array
        return _jsonable(value.tolist())
    raise TypeError(f"library value of type {type(value).__name__} is not JSON-serializable")


def _snapshot_library(root: Path, path: Path) -> dict:
    """Freeze ``load_library(root)`` into ``path`` once; a resume reuses the frozen copy."""
    reused = path.exists()
    if not reused:
        from pnr.hier.top import load_library

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_jsonable(load_library(Path(root))), sort_keys=True, indent=1))
        tmp.replace(path)
    data = path.read_bytes()
    return dict(
        path=str(path),
        source=str(root),
        reused=reused,
        blocks=len(json.loads(data)),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _read_library(library):
    """(library, sha256 or None) from a snapshot file, or from a library root (legacy callers)."""
    path = Path(library)
    if path.is_dir():
        from pnr.hier.top import load_library

        return load_library(path), None
    data = path.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def _spearman(a, b):
    """Rank correlation of two equal-length sequences (ties averaged)."""
    n = len(a)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else None


def _agreement(pairs):
    """(rho, n) over (x, y) pairs; rho is None below SPEARMAN_MIN_N pairs."""
    pairs = list(pairs)
    n = len(pairs)
    rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs]) if n >= SPEARMAN_MIN_N else None
    return rho, n


def _load(inputs: Path, constraints_path: Path):
    import yaml

    from pnr.constraints import compile_constraints
    from pnr.graph import BoardGraph

    graph = BoardGraph.from_json((inputs / "graph.json").read_text())
    constraints = compile_constraints(
        yaml.safe_load(constraints_path.read_text()),
        graph.refs,
        {c.address: c.ref for c in graph.components if c.address},
        {
            f"{c.address}:{p.name}": p.net
            for c in graph.components
            if c.address
            for p in c.pads
            if p.name
        },
    )
    from pnr.fab_profile import apply_rules

    rules = apply_rules(json.loads((inputs / "rules.json").read_text()))
    return graph, constraints, rules


# ---------------------------------------------------------------- stage 0

PLACE_JOB = (
    "inputs",
    "constraints_path",
    "start",
    "cand_dir",
    "iters",
    "library",
    "assemble_blocks",
)


def _place_one(args):
    """Worker: one global start -> legal placement + proxies. Never raises.

    ``args`` follows PLACE_JOB; ``library`` is the run's frozen snapshot file (a
    library root directory is still accepted). A hierarchical start only uses the
    start's seed, so it is recorded as kind='hier-seed' with the pool's start_kind.
    """
    return _place_impl(*args)


def _gen_prior_one(args):
    """Feedback 'prior' worker: PLACE_JOB fields + pair weights [[ref_a, pad_a, ref_b, pad_b, w]]."""
    *job, pairs = args
    weights = {(ra, pa, rb, pb): float(w) for ra, pa, rb, pb, w in pairs or []}
    return _place_impl(*job, pair_weights=weights or None)


def _place_impl(
    inputs, constraints_path, start, cand_dir, iters, library, assemble_blocks, pair_weights=None
):
    t = time.monotonic()
    record = dict(
        id=start["id"],
        kind="hier-seed" if library else start["kind"],
        start_kind=start["kind"],
        seed=start["seed"],
        stage="place",
    )
    try:
        if library:
            lib, digest = _read_library(library)
            if digest:
                record["library_sha256"] = digest
        from pnr.place.capacity_proxy import cheap_score, score
        from pnr.place.initial_pool import (
            _hard_and_source_errors,
            _prepared_source,
            preserve_source_locks,
        )
        from pnr.place.metrics import hpwl
        from pnr.place.placer import place

        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        constraints = preserve_source_locks(graph, constraints)
        source = _prepared_source(graph, constraints, rules)
        if library:
            from pnr.hier.top import hierarchical_place

            placed, report, choice = hierarchical_place(
                graph, constraints, rules, lib, start["seed"], iters, pair_weights=pair_weights
            )
            record["hier"] = choice
            if assemble_blocks:
                boards = [
                    v["native_dir"] + "/electrical/board.kicad_pcb"
                    for v in choice["blocks"].values()
                    if v.get("native_dir")
                ]
                Path(cand_dir).mkdir(parents=True, exist_ok=True)
                (Path(cand_dir) / "blocks.json").write_text(json.dumps(boards, indent=1))
        else:
            placed, report = place(
                source,
                constraints,
                seed=start["seed"],
                iters=iters,
                orient=True,
                spread=1.0,
                channel_rules=rules,
                initial_positions=start.get("positions"),
                initial_rotations=start.get("rotations"),
                pair_weights=pair_weights,
                **({"initial_sides": start["sides"]} if start.get("sides") else {}),
            )
        errors = _hard_and_source_errors(placed, source, constraints, rules)
        from pnr.place.metrics import hard_violations

        flat_violations = {k: v for k, v in hard_violations(placed, constraints).items() if v}
        if errors or not report.legal or flat_violations:
            record["flat_violations"] = str(flat_violations)[:500]
            record.update(status="illegal", errors=str(errors)[:500])
        else:
            cand = Path(cand_dir)
            cand.mkdir(parents=True, exist_ok=True)
            (cand / "placed.json").write_text(placed.to_json())
            record.update(
                status="legal", hpwl_mm=hpwl(placed), cheap_score=cheap_score(placed, rules)
            )
            try:
                proxy = score(placed, rules, pitch=2.0, passes=2)
                record["proxy_score"] = proxy["score"]
                record["proxy"] = {k: v for k, v in proxy.items() if k not in ("heatmap", "rounds")}
            except ValueError as error:
                record.update(proxy_score=float("inf"), proxy_error=str(error))
            record["poses"] = {
                c.ref: [c.pos[0], c.pos[1], c.rot, c.side] for c in placed.components
            }
    except Exception as error:  # a failed start is data, not a crash
        record.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2000:])
    record["seconds"] = time.monotonic() - t
    return record


def _starts(inputs, constraints_path, n, seed):
    from pnr.place.initial_pool import (
        InitialPoolConfig,
        _prepared_source,
        initial_starts,
        preserve_source_locks,
    )

    graph, constraints, rules = _load(inputs, constraints_path)
    constraints = preserve_source_locks(graph, constraints)
    source = _prepared_source(graph, constraints, rules)
    cfg = InitialPoolConfig(starts=n, route_finalists=1, proxy_budget=n)
    starts = initial_starts(source, constraints, cfg, seed=seed, orient=True, rules=rules)
    for s in starts:
        s["id"] = "p%03d" % int(s["id"].split("-")[1])
    return starts


# ---------------------------------------------------------------- stage 1


def _screen_one(args):
    inputs, constraints_path, cand_dir, route_iters, live = args
    t = time.monotonic()
    cand = Path(cand_dir)
    record = dict(id=cand.name, stage="screen")
    try:
        if live:
            os.environ["PNR_LIVE_CANDIDATE"] = live
        from pnr.graph import BoardGraph
        from pnr.place.initial_pool import _route_metrics
        from pnr.route.detail.router import route_board

        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        placed = BoardGraph.from_json((cand / "placed.json").read_text())
        route = route_board(placed, constraints, rules, pitch=None, max_iters=route_iters)
        m = _route_metrics(route)
        record.update(
            status="ok",
            **{k: v for k, v in m.items() if k != "unresolved_nets"},
            unresolved=len(m["unresolved_nets"]),
        )
        (cand / "screen.json").write_text(json.dumps(m, indent=2))
    except Exception as error:
        record.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2000:])
    record["seconds"] = time.monotonic() - t
    return record


# ---------------------------------------------------------------- stage 2/3


def _assemble_boards(blocks: Path, repo: Path):
    """(boards, None) for cand/blocks.json, or (None, why it cannot be assembled).
    Relative board paths resolve against ``repo`` (the native subprocess cwd)."""
    if not blocks.exists():
        return (
            None,
            f"--assemble set but {blocks} is missing (candidate not placed with --library --assemble)",
        )
    try:
        boards = json.loads(blocks.read_text())
    except ValueError as error:
        return None, f"--assemble set but {blocks} is unreadable: {error}"
    if not boards:
        return (
            None,
            f"--assemble set but {blocks} lists no routed block boards (library has no native_dir)",
        )
    missing = [b for b in boards if not (Path(repo) / b).exists()]
    if missing:
        return None, f"--assemble set but {len(missing)} block board(s) are missing: {missing[:4]}"
    return boards, None


def _native_one(
    inputs: Path,
    constraints_path: Path,
    cand: Path,
    stage: str,
    seconds: int,
    workers: int,
    env: dict,
    repo: Path,
    assemble: bool = False,
) -> dict:
    """Full native evaluation of one candidate. With ``assemble`` the routed block
    copper in cand/blocks.json is copied in; if that is impossible the record is
    status='failed' with the reason and nothing is run (never a silent flat run)."""
    t = time.monotonic()
    round_dir = cand / stage
    blocks = cand / "blocks.json"
    record = dict(
        id=cand.name,
        stage=stage,
        budget_seconds=seconds,
        workers=workers,
        assembled=False,
        loadavg_start=_loadavg(),
    )
    if env.get("PNR_FEEDBACK") == "1":
        # the evaluation code at launch: an import of this record needs no tree lookup
        from pnr.feedback.signals import code_stamp

        record.update(code_stamp(router="shove" if env.get("PNR_SHOVE") == "1" else "plain"))
    if assemble:
        boards, error = _assemble_boards(blocks, repo)
        if error:
            record.update(
                status="failed", error=error, loadavg_end=_loadavg(), seconds=time.monotonic() - t
            )
            return record
        record.update(assembled=True, assembled_blocks=len(boards))
    if round_dir.exists():
        shutil.rmtree(round_dir)
    round_dir.mkdir(parents=True)
    for name in ("source.kicad_pcb", "source.kicad_pro", "rules.json", "fp-lib-table"):
        shutil.copy2(inputs / name, round_dir / name)
    shutil.copy2(cand / "placed.json", round_dir / "placed.json")
    dev = SRC_ROOT / "hardware/splanc_dev"
    cmd = [
        sys.executable,
        "-m",
        "pnr.full_iteration",
        str(round_dir),
        "--constraints",
        str(constraints_path),
        "--electrical-fab",
        str(dev / "mini-routing-electrical-fab.json"),
        "--plane-fab",
        str(dev / "mini-plane-access-fab.json"),
        "--annotation-source",
        str(dev / "elec/src/splanc_mini.ato"),
        "--seconds",
        str(seconds),
        *(["--assemble", str(blocks)] if assemble else []),
    ]
    run_env = dict(
        env,
        PNR_SINGLE_TRACK_WORKERS=str(workers),
        PNR_LIVE_CANDIDATE=f"{env.get('PNR_LIVE_CANDIDATE', 'hier')}/{cand.name}/{stage}",
    )
    # A whole evaluation: PNR_EVALUATION_TIMEOUT; the child stays in this process group.
    from pnr import proc

    with (round_dir / "run.log").open("w") as log:
        code, timed_out = proc.run_status(
            cmd,
            timeout=proc.evaluation_timeout(seconds),
            session=False,
            cwd=repo,
            env=run_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    record.update(exit_code=code, loadavg_end=_loadavg())
    if timed_out:
        record["timed_out"] = True
    evaluation = round_dir / "evaluation.json"
    if evaluation.exists():
        e = json.loads(evaluation.read_text())
        obj = e[
            "objective"
        ]  # [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
        record.update(
            status="ok",
            objective=obj,
            opens=obj[5],
            violations=obj[0],
            subwidth=obj[3],
            blocked_entries=obj[1],
            reference_failures=obj[2],
            unqualified_pairs=obj[4],
            qualified=e["qualified"],
        )
        # PNR_SI=1 side fields (present only when the flag produced them); si_layout_failures
        # is a rank key with PNR_SI=1 (_rank_key), the others are recorded only.
        record.update(
            {k: e[k] for k in ("si_layout_failures", "si_design_failures", "si_errors") if k in e}
        )
        prog = round_dir / "electrical/native-loop/progress.json"
        if prog.exists():
            p = json.loads(prog.read_text())
            record["trajectory"] = [r.get("after") for r in p.get("rounds", [])]
            record["initial_native_opens"] = p.get("initial_opens")
            record["native_termination"] = p.get("termination")
    else:
        record["status"] = "failed"
    record["seconds"] = time.monotonic() - t
    return record


# ---------------------------------------------------------------- driver


def _rank_key(stage):
    if stage == "place":
        return lambda r: (r.get("proxy_score", math.inf), r.get("cheap_score", math.inf))
    if stage == "screen":
        from pnr.place.initial_pool import route_rank

        return route_rank
    # native/deep. objective = [violations, blocked, reference, subwidth, unqualified_pairs, unconnected]
    # key: violations, unconnected, unqualified pairs, reference failures, blocked entries, width debt, id
    # PNR_SI=1: routed SI layout failures (pnr.si side field `si_layout_failures`) right after
    # violations and unconnected; a record without an SI result (None: no report or the
    # analysis crashed) ranks as the worst, like hot_loops_open in synth_native. SI design
    # failures and errors never change the order. Flag off: the key is unchanged.
    if os.environ.get("PNR_SI") == "1":

        def si_key(r):
            o = r.get("objective")
            si = r.get("si_layout_failures")
            si = math.inf if si is None else si
            return (
                (o[0], o[5], si, o[4], o[2], o[1], o[3], r["id"])
                if o
                else (math.inf,) * 7 + (r["id"],)
            )

        return si_key

    def key(r):
        o = r.get("objective")
        return (o[0], o[5], o[4], o[2], o[1], o[3], r["id"]) if o else (math.inf,) * 6 + (r["id"],)

    return key


def _sample(ids, k, seed, purpose):
    """Seeded uniform sample of ``k`` ids, independent of record order and of other draws."""
    ids = sorted(ids)
    return random.Random(f"halving:{seed}:{purpose}").sample(ids, min(max(k, 0), len(ids)))


# ---------------------------------------------------------------- feedback generations


def _read_records(path):
    """(records, bad line count) of a dataset.jsonl that another process may still be appending to."""
    recs, bad = [], 0
    for line in Path(path).read_text().splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except ValueError:
                bad += 1  # a half-written last line of a running run
    return recs, bad


def _seed_ready(run):
    """None when ``run`` has finished its native stage (status.json stages.native, or finished), else why not.

    A seed run is read once, at this run's first start: importing it earlier
    would freeze a partial pool (and put its remaining native rung and ours on
    the machine at once)."""
    try:
        status = json.loads((Path(run) / "status.json").read_text())
    except (OSError, ValueError):
        return "no readable status.json"
    stages = status.get("stages") or {}
    if status.get("finished") or "native" in stages or "deep" in stages:
        return None
    return "native stage not finished (stages: %s)" % ", ".join(sorted(stages))


def _import_seed_runs(
    runs,
    out,
    current,
    policy,
    done_native,
    dataset,
    code_policy="error",
    frozen=None,
    allow_running=False,
):
    """(records, errors, warnings): the native ok records of earlier runs as generation-0 parents.

    Ids become '<run dir name>-<id>'. The candidate's placed.json (and
    blocks.json) is copied into this run's cand dir so a later deep evaluation can
    use it; feedback stays in the source run (read-only, ``native_dir``). Each
    import is appended to this run's dataset once (stage 'native',
    ``imported_from``). Router, fab profile and (unless 'warn') budget must match;
    so must the evaluation code unless ``code_policy`` is 'warn', or 'rebase'
    (the record is marked ``stale_code``: re-evaluated under this code before
    generation 1, never ranked itself).

    The import set is frozen at the first start (a 'seed-from' dataset record):
    a resume uses exactly those records, whatever the seed runs did since, so the
    generation plan and its positional child ids cannot shift."""
    from pnr.feedback.signals import (
        PNR_ROOT,
        TreeCode,
        check_code,
        check_import,
        observed_code,
        observed_key,
        read_round,
    )

    resolved = sorted(str(Path(r).resolve()) for r in runs)
    if frozen is not None:
        errors = []
        if frozen.get("runs") != resolved:
            errors.append(
                "--seed-from %s differs from the import set frozen at the first start (%s)"
                % (resolved, frozen.get("runs"))
            )
        if frozen.get("code_policy", "error") != code_policy:
            errors.append(
                "--import-code-mismatch %s differs from the first start (%s)"
                % (code_policy, frozen.get("code_policy"))
            )
        missing = [i for i in frozen.get("native") or [] if i not in done_native]
        if missing:
            errors.append("frozen imports missing from dataset.jsonl: %s" % missing[:5])
        return [done_native[i] for i in frozen.get("native") or [] if i in done_native], errors, []
    recs, errors, warnings = [], [], []
    current_code = TreeCode(PNR_ROOT, current["router"])
    cache = {}
    for run in runs:
        run = Path(run).resolve()
        ds = run / "dataset.jsonl"
        if not ds.exists():
            errors.append(f"{run}: no dataset.jsonl")
            continue
        why = _seed_ready(run)
        if why:
            (warnings if allow_running else errors).append(f"{run}: {why}")
        native = {}
        lines, bad = _read_records(ds)
        if bad:
            warnings.append(f"{run}: {bad} unreadable dataset line(s) skipped")
        for r in lines:
            if r.get("stage") == "native" and not r.get("imported_from") and "id" in r:
                native[r["id"]] = r
        for rid, r in sorted(native.items()):
            if r.get("status") != "ok" or not r.get("objective") or r.get("stale_code"):
                continue
            new_id = f"{run.name}-{rid}"
            if new_id in done_native:
                recs.append(done_native[new_id])
                continue
            src = run / "cand" / rid
            fb = read_round(src / "native")
            if fb.get("missing"):
                warnings.append(f"{new_id}: feedback missing")
            else:
                errs, warns = check_import(observed_key(fb, stage="native"), current, policy)
                errors += [f"{new_id}: {e}" for e in errs]
                warnings += [f"{new_id}: {w}" for w in warns]
            obs = observed_code(
                src / "native",
                current["router"],
                stamped=r.get("code"),
                cache=cache,
                scheme=r.get("code_key_scheme"),
            )
            errs, warns, stale = check_code(obs, current_code, policy=code_policy)
            errors += [f"{new_id}: {e}" for e in errs]
            warnings += [f"{new_id}: {w}" for w in warns]
            if not (src / "placed.json").exists():
                warnings.append(f"{new_id}: no placed.json, skipped")
                continue
            rec = dict(
                r,
                id=new_id,
                source_id=rid,
                imported_from=str(run),
                native_dir=str(src / "native"),
                gen=0,
                fb=fb,
                import_code=dict(
                    code=obs.get("code"),
                    code_key_scheme=obs.get("code_key_scheme"),
                    tree=obs.get("tree"),
                    reason=obs.get("reason"),
                ),
            )
            for k in ("parent", "root", "arm", "k", "matched", "lineage_depth", "promoted_from"):
                rec.pop(k, None)  # the source run's lineage is not this run's
            if stale:
                rec["stale_code"] = True
            recs.append(rec)
    if not errors:
        for rec in recs:
            if rec["id"] in done_native:
                continue
            src, dst = (
                Path(rec["imported_from"]) / "cand" / rec["source_id"],
                Path(out) / "cand" / rec["id"],
            )
            dst.mkdir(parents=True, exist_ok=True)
            for name in ("placed.json", "blocks.json"):
                if (src / name).exists() and not (dst / name).exists():
                    shutil.copy2(src / name, dst / name)
            _append(dataset, rec)
        _append(
            dataset,
            dict(
                id="seed-from",
                stage="seed-from",
                runs=resolved,
                native=[r["id"] for r in recs],
                code_policy=code_policy,
                router_key=current,
                frozen_at=time.time(),
            ),
        )
    return recs, errors, warnings


def _same_code(records):
    """Records evaluated by this run's code (stale-code seed imports feed only the rebase)."""
    return [r for r in records if not r.get("stale_code")]


def _generations(a, ctx):
    """Routing-feedback generations between the native rung and deep (PNR_FEEDBACK=1).

    Before generation 1, seed imports evaluated by other code (``stale_code``,
    --import-code-mismatch rebase) are re-evaluated unchanged under this code
    (ids rb-<id>, arm rebase); stale records are never ranked. Generation g
    ranks every same-code native-stage record of generations < g (own, rebased
    or same-code imports and earlier children) with the native rank key.
    Parents are the top ceil(P / 2^(g-1)) with a PULL child. Per parent: up to
    --gen-children PULL children (pnr.feedback.moves, from the parent's
    evaluated pose and its native feedback.json), the top --gen-rand parents one
    RAND control each (matched to the parent's first PULL child: same
    displacement); per generation --gen-fresh 'prior' samples (an unsampled
    start re-placed with the pooled failure-rate attraction; skipped when there
    is none) and, in generation 1, --gen-repeat unchanged re-evaluations of the
    best parent. Children enter rung 1 (best --gen-promote go on) or the native
    rung directly (repeats always go directly) and join the pool; parents are
    never removed. Ids g<g>c<jj> (PULL), g<g>r<i> (RAND), g<g>f<j> (prior),
    g1p<j> (repeat); a 'gen-place' record per child. A resume regenerates the
    plan and reuses the records by id; a regenerated child whose parent, arm or
    poses differ from the stored one stops the run (the plan changed). Returns
    the same-code native pool (ok records) that deep ranks.
    """
    from pnr.feedback.moves import pull_children, rand_child, seed_for
    from pnr.feedback.signals import current_key, key_string, read_round
    from pnr.feedback.table import build
    from pnr.feedback.toplevel import (
        child_graph,
        parent_pose,
        poses_sha,
        prepared,
        source_errors,
        top_board,
    )
    from pnr.graph import BoardGraph

    out, status, save_status = ctx["out"], ctx["status"], ctx["save_status"]
    done_records, native_stage, flags = ctx["done_records"], ctx["native_stage"], ctx["flags"]
    dataset, library = ctx["dataset"], ctx["library"]
    graph, constraints, rules = _load(ctx["inputs"], ctx["constraints_path"])
    con, source = prepared(graph, constraints, rules)
    rkey = current_key("native", a.s2, ctx["inputs"])
    rkey_s = key_string(rkey)
    lib_blocks = sorted(_read_library(library)[0]) if library else []
    power_first = os.environ.get("PNR_POWER_FIRST") == "1"
    status["feedback"] = dict(
        router_key=rkey_s,
        generations=a.generations,
        entry=a.gen_entry,
        max_move_mm=a.max_move,
        lineage_cap_mm=a.lineage_cap,
        pair_weight=a.fb_pair_weight,
        parents=a.gen_parents,
        children=a.gen_children,
        rand=a.gen_rand,
        fresh=a.gen_fresh,
        repeat=a.gen_repeat,
        plateau=a.gen_plateau,
        library_blocks=lib_blocks,
        code_policy=a.import_code_mismatch,
        import_rebase=a.import_rebase,
    )
    save_status()
    fb_cache = {}

    def round_dir(r):
        return Path(r.get("native_dir") or out / "cand" / r["id"] / "native")

    def fb_of(r):
        if r.get("fb"):
            return r["fb"]
        d = str(round_dir(r))
        if d not in fb_cache:
            fb_cache[d] = read_round(d)
        return fb_cache[d]

    def load_parent(r):
        cand = out / "cand" / r["id"]
        pose, src = parent_pose(cand / "placed.json", round_dir(r) / "evaluated-placed.json")
        if src == "evaluated" and source_errors(pose, source, con, rules):
            pose, src = parent_pose(cand / "placed.json")
        return pose, src

    def opens(r):
        o = (r or {}).get("objective")
        return o[5] if o else None

    def ok(r):
        return r.get("status") == "ok" and r.get("objective")

    def check_resume(cid, stored, arm, parent_id, sha):
        """A stored gen-place record must be the child this invocation regenerated."""
        want = dict(arm=arm, parent=parent_id, poses_sha=sha)
        have = {k: stored.get(k) for k in want}
        if have != want:
            raise SystemExit(
                f"resume plan changed for {cid}: stored {have}, regenerated {want}; the seed "
                "import set, the CLI or the code changed since the first start"
            )

    # ---- rebase: stale-code seed imports re-evaluated unchanged under this code
    native = done_records("native")
    stale = sorted(
        (r for r in native.values() if r.get("stale_code") and ok(r)), key=_rank_key("native")
    )
    if a.import_rebase:
        stale = stale[: a.import_rebase]
    if stale:
        tb = time.monotonic()
        gen_place = done_records("gen-place")
        ids = []
        for r in stale:
            cid = f"rb-{r['id']}"
            ids.append(cid)
            src = out / "cand" / r["id"]
            pose = BoardGraph.from_json((src / "placed.json").read_text())
            sha = poses_sha(pose)
            if cid in gen_place:
                check_resume(cid, gen_place[cid], "rebase", r["id"], sha)
            else:
                cand = out / "cand" / cid
                cand.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / "placed.json", cand / "placed.json")
                if (src / "blocks.json").exists():
                    shutil.copy2(src / "blocks.json", cand / "blocks.json")
                rec = dict(
                    id=cid,
                    stage="gen-place",
                    status="legal",
                    gen=0,
                    arm="rebase",
                    parent=r["id"],
                    root=cid,
                    lineage_depth=0,
                    pose_source="placed",
                    poses_sha=sha,
                    router_key=rkey_s,
                    op_detail=dict(
                        arm="rebase",
                        import_code=(r.get("import_code") or {}).get("code"),
                        import_objective=r.get("objective"),
                    ),
                )
                _append(dataset, rec)
                gen_place[cid] = rec
            flags[cid] = dict(
                gen=0,
                arm="rebase",
                parent=r["id"],
                root=cid,
                lineage_depth=0,
                router_key=rkey_s,
                promoted_from="rebase",
            )
        print("rebase", len(ids), "stale-code seed records", flush=True)
        done = native_stage("native", [dict(id=i) for i in ids], a.s2)
        by = {r["id"]: r for r in done}
        pairs = sorted((r["id"], opens(r), opens(by.get(f"rb-{r['id']}"))) for r in stale)
        status["stages"]["rebase"] = dict(
            planned=ids,
            pairs=pairs,
            delta=[b - o for _, o, b in pairs if b is not None],
            complete_import=sum(o == 0 for _, o, _ in pairs),
            complete_rebase=sum(b == 0 for _, _, b in pairs),
            seconds=time.monotonic() - tb,
        )
        save_status()

    history = []
    for g in range(1, a.generations + 1):
        tg = time.monotonic()
        native = done_records("native")
        gen_place = done_records("gen-place")
        pool = [r for r in _same_code(native.values()) if ok(r) and r.get("gen", 0) < g]
        ranked = sorted(pool, key=_rank_key("native"))
        by_id = {r["id"]: r for r in pool}
        history.append(ranked[0]["id"] if ranked else None)
        summary = dict(
            pool=len(pool),
            best_before=history[-1],
            best_objective_before=ranked[0]["objective"] if ranked else None,
        )
        if not ranked:
            status["stages"]["gen%d" % g] = dict(summary, stop="empty pool")
            save_status()
            break
        if (
            a.gen_plateau
            and len(history) > a.gen_plateau
            and len(set(history[-a.gen_plateau - 1 :])) == 1
        ):
            status["stages"]["gen%d" % g] = dict(summary, stop="plateau")
            save_status()
            break
        table = build(
            [dict(id=r["id"], parent=r.get("parent"), fb=fb_of(r)) for r in pool],
            scope=out.name,
            router=rkey["router"],
        )
        (out / ("feedback-table.g%d.json" % g)).write_text(
            json.dumps(dict(table.to_json(), router_key=rkey_s, sha=table.sha()), indent=1)
        )

        def root_of(r):
            seen = set()
            while r.get("parent") in by_id and r["id"] not in seen:
                seen.add(r["id"])
                r = by_id[r["parent"]]
            return r

        children, parents, want = [], [], math.ceil(a.gen_parents / 2 ** (g - 1))
        # children placed by earlier generations or planned above (a resume replays this generation's plan)
        known = {
            r.get("poses_sha")
            for r in gen_place.values()
            if r.get("gen", 0) < g and r.get("poses_sha")
        }
        for r in ranked:
            if len(parents) >= want:
                break
            fb = fb_of(r)
            if fb.get("missing") or fb.get("router") != rkey["router"] or not fb.get("conns"):
                continue
            parent, src = load_parent(r)
            root, origin = root_of(r), None
            if root["id"] != r["id"]:
                origin = {c.ref: tuple(c.pos) for c in load_parent(root)[0].components}
            board = top_board(
                parent,
                graph,
                constraints,
                rules,
                library_blocks=lib_blocks,
                origin=origin,
                power_first=power_first,
            )

            def seen_(poses, parent=parent):
                return poses_sha(child_graph(parent, poses)) in known

            kids = pull_children(
                board,
                fb,
                table,
                r["id"],
                n=a.gen_children,
                max_move=a.max_move,
                lineage_cap=a.lineage_cap,
                skip=seen_,
            )
            if not kids:
                continue
            parents.append(r["id"])
            first_id = None
            for k, kid in enumerate(kids):
                cid = "g%dc%02d" % (g, sum(c["arm"] == "pull" for c in children))
                first_id = first_id or cid
                children.append(
                    dict(
                        id=cid,
                        arm="pull",
                        k=k,
                        parent=r,
                        root=root["id"],
                        graph=child_graph(parent, kid["poses"]),
                        detail=kid["detail"],
                        pose_source=src,
                    )
                )
                known.add(poses_sha(children[-1]["graph"]))
            if len(parents) <= a.gen_rand:
                # the control is matched to the FIRST PULL child (k=0): same parent, same displacement
                first = kids[0]["detail"]
                kid = rand_child(
                    board,
                    seed_for(a.gen_seed, out.name, g, r["id"], "rand"),
                    first["move_mm"],
                    rotate=first["rot"] is not None,
                    lineage_cap=a.lineage_cap,
                    skip=seen_,
                )
                if kid is not None:
                    children.append(
                        dict(
                            id="g%dr%d" % (g, len(parents) - 1),
                            arm="rand",
                            matched=first_id,
                            parent=r,
                            root=root["id"],
                            graph=child_graph(parent, kid["poses"]),
                            detail=kid["detail"],
                            pose_source=src,
                        )
                    )
                    known.add(poses_sha(children[-1]["graph"]))
        if g == 1:
            for j in range(a.gen_repeat):
                best = ranked[0]
                pose = BoardGraph.from_json((out / "cand" / best["id"] / "placed.json").read_text())
                children.append(
                    dict(
                        id="g1p%d" % j,
                        arm="repeat",
                        parent=best,
                        root=root_of(best)["id"],
                        graph=pose,
                        detail=dict(arm="repeat"),
                        pose_source="placed",
                    )
                )
        # write the moved children (a resume keeps what an earlier invocation wrote, if it is the same child)
        seen = set()
        for c in children:
            sha = poses_sha(c["graph"])
            if c["arm"] != "repeat" and sha in seen:
                c["duplicate"] = True
                continue
            seen.add(sha)
            if c["id"] in gen_place:
                check_resume(c["id"], gen_place[c["id"]], c["arm"], c["parent"]["id"], sha)
                continue
            cand = out / "cand" / c["id"]
            cand.mkdir(parents=True, exist_ok=True)
            (cand / "placed.json").write_text(c["graph"].to_json())
            blocks = out / "cand" / c["parent"]["id"] / "blocks.json"
            if blocks.exists():
                shutil.copy2(blocks, cand / "blocks.json")
            d = c["detail"]
            rec = dict(
                id=c["id"],
                stage="gen-place",
                status="legal",
                gen=g,
                arm=c["arm"],
                parent=c["parent"]["id"],
                root=c["root"],
                lineage_depth=c["parent"].get("lineage_depth", 0) + 1,
                pose_source=c["pose_source"],
                poses_sha=sha,
                router_key=rkey_s,
                op_detail={k: v for k, v in d.items() if k != "mover_refs" or len(v) <= 4},
            )
            for k in ("k", "matched"):
                if k in c:
                    rec[k] = c[k]
            _append(dataset, rec)
            gen_place[c["id"]] = rec
        children = [c for c in children if not c.get("duplicate")]
        # fresh 'prior' samples: unsampled legal starts re-placed with the pooled failure-rate attraction
        pairs = [
            [ra, pa, rb, pb, w]
            for (ra, pa, rb, pb), w in sorted(
                table.pair_weights(weight=a.fb_pair_weight, cap=2 * a.fb_pair_weight).items()
            )
        ]
        used = {r.get("source_start") for r in gen_place.values() if r.get("arm") == "prior"}
        sampled = {
            r["id"]
            for st in ("screen", "rung1", "native", "deep")
            for r in done_records(st).values()
        }
        legal_ids = {i for i, r in ctx["placed"].items() if r.get("status") == "legal"}
        spare = [
            s
            for s in ctx["starts"]
            if s["id"] in legal_ids and s["id"] not in sampled and s["id"] not in used
        ]
        jobs, prior_skipped = [], 0
        for j in range(a.gen_fresh):
            cid = "g%df%d" % (g, j)
            if cid in gen_place:
                continue
            if not pairs:
                # without pair weights a 'prior' is an ordinary unweighted start: not a prior
                prior_skipped += 1
                continue
            pick = _sample([s["id"] for s in spare], 1, a.gen_seed, "gen%d-prior%d" % (g, j))
            if pick:
                start = next(s for s in spare if s["id"] == pick[0])
                spare = [s for s in spare if s["id"] != pick[0]]
                start = dict(start, id=cid, source_start=start["id"])
            else:
                start = dict(
                    id=cid,
                    kind="prior-seed",
                    seed=seed_for(a.gen_seed, out.name, g, "prior", j),
                    source_start=None,
                )
            jobs.append(
                (
                    str(ctx["inputs"]),
                    str(ctx["constraints_path"]),
                    start,
                    str(out / "cand" / cid),
                    a.iters,
                    library,
                    a.assemble,
                    pairs,
                )
            )
        if jobs:
            with _process_pool(min(a.procs, len(jobs))) as pool:
                for job, rec in zip(jobs, pool.map(_gen_prior_one, jobs)):
                    rec = dict(
                        rec,
                        stage="gen-place",
                        gen=g,
                        arm="prior",
                        parent=None,
                        lineage_depth=0,
                        source_start=job[2].get("source_start"),
                        pair_weights_n=len(pairs),
                        router_key=rkey_s,
                        status=rec.get("status"),
                    )
                    rec.pop("poses", None)
                    _append(dataset, rec)
                    gen_place[rec["id"]] = rec
        for j in range(a.gen_fresh):
            rec = gen_place.get("g%df%d" % (g, j))
            if rec and rec.get("status") == "legal":
                children.append(dict(id=rec["id"], arm="prior", parent=None, root=None))
        for c in children:
            flags[c["id"]] = dict(
                gen=g,
                arm=c["arm"],
                parent=(c["parent"] or {}).get("id"),
                root=c.get("root"),
                lineage_depth=gen_place[c["id"]].get("lineage_depth", 0),
                router_key=rkey_s,
                promoted_from="gen",
                **{k: c[k] for k in ("k", "matched") if k in c},
            )
        arms = {}
        for c in children:
            arms[c["arm"]] = arms.get(c["arm"], 0) + 1
        print(
            "gen",
            g,
            "parents",
            parents,
            "children",
            arms,
            "table",
            table.sha(),
            "entry",
            a.gen_entry,
            "prior skipped (no pair weights)" * bool(prior_skipped),
            flush=True,
        )
        direct = [c["id"] for c in children if c["arm"] == "repeat" or a.gen_entry == "native"]
        rung = [c["id"] for c in children if c["id"] not in direct]
        r1, promoted = [], []
        if rung:
            r1 = native_stage("rung1", [dict(id=i) for i in rung], a.s2, stop_phase=a.rung1_stop)
            k = a.gen_promote or math.ceil(len(rung) / 2)
            promoted = [r["id"] for r in sorted(r1, key=_rank_key("native"))[:k]]
            for i in promoted:
                flags[i] = dict(flags[i], promoted_from="gen-rung1")
        s2 = native_stage("native", [dict(id=i) for i in direct + promoted], a.s2)
        deltas = {}
        for r in s2:
            parent = by_id.get(flags.get(r["id"], {}).get("parent") or r.get("parent"))
            e = deltas.setdefault(
                flags.get(r["id"], {}).get("arm") or r.get("arm"), dict(n=0, opens=[], delta=[])
            )
            e["n"] += 1
            e["opens"].append(opens(r))
            if parent is not None:
                e["delta"].append(opens(r) - opens(parent))
        after = sorted(
            (r for r in _same_code(done_records("native").values()) if ok(r)),
            key=_rank_key("native"),
        )
        status["stages"]["gen%d" % g] = dict(
            summary,
            parents=parents,
            children=arms,
            entry=a.gen_entry,
            rung1=sorted((opens(r), r["id"]) for r in r1),
            promoted=promoted,
            native={r["id"]: r["objective"] for r in s2},
            arms=deltas,
            repeat_delta=(deltas.get("repeat") or {}).get("delta"),
            prior_skipped=prior_skipped,
            table_sha=table.sha(),
            table_evals=table.n_evals,
            floor=table.floor,
            pair_weights=len(pairs),
            best_after=after[0]["id"] if after else None,
            best_objective_after=after[0]["objective"] if after else None,
            seconds=time.monotonic() - tg,
        )
        save_status()
    return [r for r in _same_code(done_records("native").values()) if ok(r)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--constraints", type=Path, required=True)
    ap.add_argument("--repo", type=Path, required=True, help="cwd for native subprocesses")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n0", type=int, default=64)
    ap.add_argument("--k1", type=int, default=12)
    ap.add_argument("--k2", type=int, default=4)
    ap.add_argument("--k3", type=int, default=1)
    ap.add_argument(
        "--control",
        type=int,
        default=2,
        help="also screen a seeded uniform sample of N non-survivor legal placements (control=True)",
    )
    ap.add_argument(
        "--promote-from",
        choices=["screen", "place"],
        default="screen",
        help="stage-2 source: K2 best screens, or ('place') a seeded uniform sample of K2 legal "
        "placements with no screen stage",
    )
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument("--route-iters", type=int, default=10)
    ap.add_argument("--s2", type=int, default=900, help="native budget per stage-2 candidate")
    ap.add_argument("--s3", type=int, default=5400, help="native budget per stage-3 candidate")
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--native-parallel", type=int, default=2)
    ap.add_argument("--native-workers", type=int, default=4)
    ap.add_argument(
        "--library",
        type=Path,
        help="block layout library: hierarchical placement "
        f"(frozen once into OUT/{LIBRARY_SNAPSHOT}; a resume reuses that snapshot)",
    )
    ap.add_argument(
        "--assemble", action="store_true", help="copy routed block copper before native routing"
    )
    ap.add_argument(
        "--rung1-stop",
        help="native rung 1: stop each evaluation after this native_loop phase "
        "(e.g. 06-signals) and promote the best --k2 of --k1r sampled placements",
    )
    ap.add_argument("--k1r", type=int, default=12, help="placements sampled into rung 1")
    ap.add_argument("--stop-after", choices=["place", "screen", "native", "deep"], default="deep")
    fb = ap.add_argument_group("routing feedback generations (PNR_FEEDBACK=1; see pnr.feedback)")
    fb.add_argument(
        "--generations",
        type=int,
        default=0,
        help="after the native rung: children of the top native candidates (PULL moves of failed "
        "connections, a RAND control) plus fresh prior samples, ranked with the native pool",
    )
    fb.add_argument(
        "--gen-parents",
        type=int,
        default=3,
        help="parents in generation 1; g takes ceil(P / 2^(g-1))",
    )
    fb.add_argument(
        "--gen-children", type=int, default=2, help="PULL children per parent (distinct movers)"
    )
    fb.add_argument(
        "--gen-rand", type=int, default=1, help="top parents that also get one RAND control child"
    )
    fb.add_argument(
        "--gen-fresh",
        type=int,
        default=1,
        help="fresh 'prior' samples per generation (an unsampled start re-placed with the pooled "
        "failure-rate attraction)",
    )
    fb.add_argument(
        "--gen-repeat",
        type=int,
        default=1,
        help="generation 1: re-evaluate the best parent unchanged (measures run-to-run noise)",
    )
    fb.add_argument(
        "--gen-plateau",
        type=int,
        default=2,
        help="stop when the pool best is unchanged this many generations",
    )
    fb.add_argument(
        "--gen-entry",
        choices=["rung1", "native"],
        help="children enter at rung 1 (with --rung1-stop; the best --gen-promote go on to the native "
        "rung) or directly at the native rung (default: rung1 when --rung1-stop is set)",
    )
    fb.add_argument(
        "--gen-promote",
        type=int,
        default=0,
        help="with --gen-entry rung1: children promoted per generation (0 = half, rounded up)",
    )
    fb.add_argument("--max-move", type=float, default=3.0, help="PULL lattice radius (mm)")
    fb.add_argument(
        "--lineage-cap",
        type=float,
        default=4.0,
        help="max distance of a mover from its root pose (mm)",
    )
    fb.add_argument(
        "--fb-pair-weight",
        type=float,
        default=10.0,
        help="'prior' attraction weight x failure rate (capped at 2x)",
    )
    fb.add_argument("--gen-seed", type=int, help="seed of the feedback draws (default --seed)")
    fb.add_argument(
        "--seed-from",
        action="append",
        default=[],
        type=Path,
        help="import the native records (and cand placed.json, read-only feedback) of an earlier run "
        "as generation-0 parents; --n0 0 skips this run's own stages 0-2. The run must have "
        "finished its native stage; the import set is frozen at the first start",
    )
    fb.add_argument(
        "--seed-allow-running",
        action="store_true",
        help="import a --seed-from run that has not finished its native stage (partial pool)",
    )
    fb.add_argument("--import-budget-mismatch", choices=["error", "warn"], default="error")
    fb.add_argument(
        "--import-code-mismatch",
        choices=["error", "warn", "rebase"],
        default="error",
        help="seed records evaluated by different (or unknown) evaluation code: refuse; import as is "
        "with a warning; or rebase: re-evaluate their placements unchanged under this code before "
        "generation 1 (ids rb-<id>, arm rebase) and rank only same-code evaluations",
    )
    fb.add_argument(
        "--import-rebase",
        type=int,
        default=0,
        help="with --import-code-mismatch rebase: re-evaluate the best N stale seed records by their "
        "recorded rank (0 = all)",
    )
    a = ap.parse_args(argv)
    if a.generations < 0:
        ap.error("--generations must be >= 0")
    if a.generations and os.environ.get("PNR_FEEDBACK") != "1":
        ap.error("--generations > 0 needs PNR_FEEDBACK=1")
    if a.seed_from and not a.generations:
        ap.error("--seed-from needs --generations > 0")
    if a.generations and a.library and os.environ.get("PNR_MACRO_HULL") == "1":
        # N-0001: generation moves (pnr.feedback.moves) and their relocations test
        # flat member courtyards only; they do not see assembled block copper
        # around nested parts, so the combination is refused, not warned about.
        ap.error(
            "PNR_MACRO_HULL=1 does not support --generations (feedback moves ignore block copper "
            "around nested parts); run generations without the hull flag"
        )
    if a.import_rebase < 0:
        ap.error("--import-rebase must be >= 0")
    if a.gen_entry == "rung1" and not a.rung1_stop:
        ap.error("--gen-entry rung1 needs --rung1-stop")
    if a.gen_entry is None:
        a.gen_entry = "rung1" if a.rung1_stop else "native"
    if a.gen_seed is None:
        a.gen_seed = a.seed
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs = a.inputs.resolve()
    constraints_path = a.constraints.resolve()
    dataset = out / "dataset.jsonl"
    status = dict(started=time.time(), args={k: str(v) for k, v in vars(a).items()}, stages={})

    def save_status():
        tmp = out / "status.json.tmp"
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(out / "status.json")

    def done_records(stage):
        if not dataset.exists():
            return {}
        recs = [json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]
        return {r["id"]: r for r in recs if r.get("stage") == stage}

    if a.generations and dataset.exists():
        # This run's own records are complete lines (appended by this process); a
        # torn line means an earlier invocation was killed mid-write: stop, don't guess.
        _, bad = _read_records(dataset)
        if bad:
            raise SystemExit(
                f"{dataset}: {bad} unreadable line(s); repair the file before resuming"
            )

    library = None
    if a.library:
        status["library"] = snap = _snapshot_library(a.library.resolve(), out / LIBRARY_SNAPSHOT)
        library = snap["path"]
        print(
            "library",
            "reused" if snap["reused"] else "snapshot",
            snap["path"],
            snap["blocks"],
            "blocks",
            snap["sha256"][:12],
            flush=True,
        )
        if not snap["blocks"]:
            print("warning: library snapshot has no ranked blocks", flush=True)

    seeded = []
    if a.seed_from:
        from pnr.feedback.signals import current_key

        frozen = done_records("seed-from").get("seed-from")
        seeded, errors, warnings = _import_seed_runs(
            a.seed_from,
            out,
            current_key("native", a.s2, inputs),
            a.import_budget_mismatch,
            done_records("native"),
            dataset,
            code_policy=a.import_code_mismatch,
            frozen=frozen,
            allow_running=a.seed_allow_running,
        )
        for w in sorted(set(warnings))[:10]:
            print("seed-from warning:", w, flush=True)
        if errors:
            ap.error("--seed-from runs do not match this run:\n  " + "\n  ".join(errors[:20]))
        status["seed_from"] = dict(
            runs=[str(r) for r in a.seed_from],
            native=[r["id"] for r in seeded],
            frozen=frozen is not None,
            code_policy=a.import_code_mismatch,
            stale_code=[r["id"] for r in seeded if r.get("stale_code")],
            warnings=sorted(set(warnings))[:20],
        )
    elif a.generations and done_records("seed-from"):
        ap.error(
            "this run was started with --seed-from %s; resume with the same --seed-from"
            % done_records("seed-from")["seed-from"].get("runs")
        )

    # ---- stage 0
    placed = done_records("place")
    all_starts = _starts(inputs, constraints_path, a.n0, a.seed) if a.n0 > 0 else []
    starts = [s for s in all_starts if s["id"] not in placed]
    t0 = time.monotonic()
    with _process_pool(a.procs) as pool:
        jobs = [
            (
                str(inputs),
                str(constraints_path),
                s,
                str(out / "cand" / s["id"]),
                a.iters,
                library,
                a.assemble,
            )
            for s in starts
        ]  # PLACE_JOB order
        for rec in pool.map(_place_one, jobs):
            _append(dataset, rec)
            placed[rec["id"]] = rec
            print(
                "place",
                rec["id"],
                rec["status"],
                rec.get("proxy_score"),
                "%.1fs" % rec["seconds"],
                flush=True,
            )
    legal = [r for r in placed.values() if r["status"] == "legal"]
    status["stages"]["place"] = dict(n=len(placed), legal=len(legal), seconds=time.monotonic() - t0)
    save_status()
    if not legal and not seeded:
        raise SystemExit("no legal placement")
    if a.stop_after == "place":
        return
    screened, flags, survivors, s2 = {}, {}, [], []

    # ---- stage 2 / 3 (native)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PNR_ROOT)

    def native_stage(name, cands, seconds, stop_phase=None):
        done = done_records(name)
        todo = [r for r in cands if r["id"] not in done]
        stage_env = dict(env, PNR_STOP_AFTER_PHASE=stop_phase) if stop_phase else env
        with cf.ThreadPoolExecutor(a.native_parallel) as pool:
            futs = {
                pool.submit(
                    _native_one,
                    inputs,
                    constraints_path,
                    out / "cand" / r["id"],
                    name,
                    seconds,
                    a.native_workers,
                    stage_env,
                    a.repo.resolve(),
                    assemble=a.assemble,
                ): r
                for r in todo
            }
            for f in cf.as_completed(futs):
                rec = f.result()
                rec.update(flags.get(rec["id"], {}))
                _append(dataset, rec)
                done[rec["id"]] = rec
                print(
                    name,
                    rec["id"],
                    rec.get("status"),
                    rec.get("objective"),
                    rec.get("error", ""),
                    "%.0fs" % rec["seconds"],
                    flush=True,
                )
        return [done[r["id"]] for r in cands if done.get(r["id"], {}).get("status") == "ok"]

    env.pop("PNR_STOP_AFTER_PHASE", None)

    if legal:
        if a.promote_from == "place":
            # ---- stage 1 skipped: uniform sample of legal placements goes straight to native
            count = a.k1r if a.rung1_stop else a.k2
            survivors = [
                placed[i] for i in _sample([r["id"] for r in legal], count, a.seed, "promote-place")
            ]
            flags = {
                r["id"]: dict(promoted_from="place", sampled=True, control=False) for r in survivors
            }
            status["stages"]["screen"] = dict(
                skipped=True, promote_from="place", sampled=[r["id"] for r in survivors]
            )
            save_status()
            if a.stop_after == "screen":
                return
        else:
            # ---- stage 1: top-K1 by proxy plus a uniform control sample of the rest
            survivors = sorted(legal, key=_rank_key("place"))[: a.k1]
            keep = {r["id"] for r in survivors}
            controls = _sample(
                [r["id"] for r in legal if r["id"] not in keep], a.control, a.seed, "control"
            )
            control_ids = set(controls)
            cands = survivors + [placed[i] for i in controls]
            screened = done_records("screen")
            t1 = time.monotonic()
            todo = [r for r in cands if r["id"] not in screened]
            base_lane = os.environ.get("PNR_LIVE_CANDIDATE", "hier")
            with _process_pool(min(a.procs, max(1, len(todo)))) as pool:
                jobs = [
                    (
                        str(inputs),
                        str(constraints_path),
                        str(out / "cand" / r["id"]),
                        a.route_iters,
                        f"{base_lane}/{r['id']}/screen",
                    )
                    for r in todo
                ]
                for rec in pool.map(_screen_one, jobs):
                    rec["control"] = rec["id"] in control_ids
                    _append(dataset, rec)
                    screened[rec["id"]] = rec
                    print(
                        "screen",
                        rec["id"],
                        rec["status"],
                        rec.get("objective"),
                        "control" * rec["control"],
                        "%.1fs" % rec["seconds"],
                        flush=True,
                    )
            # membership is this run's draw, not whatever an earlier (resumed) record said
            s1 = [
                dict(screened[r["id"]], control=r["id"] in control_ids)
                for r in cands
                if screened.get(r["id"], {}).get("status") == "ok"
            ]
            pairs = lambda rs: [(placed[r["id"]]["proxy_score"], r["objective"][0]) for r in rs]
            rho, n = _agreement(pairs(r for r in s1 if not r["control"]))
            rho_all, n_all = _agreement(pairs(s1))
            status["stages"]["screen"] = dict(
                n=len(s1),
                seconds=time.monotonic() - t1,
                controls=controls,
                spearman_proxy_vs_missing=rho,
                spearman_proxy_vs_missing_n=n,
                spearman_proxy_vs_missing_all=rho_all,
                spearman_proxy_vs_missing_all_n=n_all,
                missing=dict(
                    survivor=sorted(r["objective"][0] for r in s1 if not r["control"]),
                    control=sorted(r["objective"][0] for r in s1 if r["control"]),
                ),
            )
            save_status()
            if a.stop_after == "screen" or not s1:
                return
            # controls compete on their screen result only; ties keep proxy-ranked survivors first
            survivors = sorted(s1, key=_rank_key("screen"))[: a.k2]
            flags = {
                r["id"]: dict(promoted_from="screen", sampled=False, control=r["id"] in control_ids)
                for r in survivors
            }
            status["stages"]["screen"]["promoted_controls"] = [
                r["id"] for r in survivors if r["control"]
            ]
            save_status()

        if a.rung1_stop:
            # ---- native rung 1: every sampled placement through phases up to rung1_stop
            tr = time.monotonic()
            r1 = native_stage("rung1", survivors, a.s2, stop_phase=a.rung1_stop)
            status["stages"]["rung1"] = dict(
                n=len(r1),
                seconds=time.monotonic() - tr,
                stop=a.rung1_stop,
                opens=sorted((r["opens"], r["id"]) for r in r1),
            )
            save_status()
            survivors = [dict(x) for x in sorted(r1, key=_rank_key("native"))[: a.k2]]
            for r in survivors:
                flags[r["id"]] = dict(flags.get(r["id"], {}), promoted_from="rung1")
        t2 = time.monotonic()
        s2 = native_stage("native", survivors, a.s2)
        rho_s, n_s = _agreement(
            (screened[r["id"]]["objective"][0], r["opens"])
            for r in s2
            if screened.get(r["id"], {}).get("status") == "ok"
        )
        rho_p, n_p = _agreement((placed[r["id"]]["proxy_score"], r["opens"]) for r in s2)
        status["stages"]["native"] = dict(
            n=len(s2),
            seconds=time.monotonic() - t2,
            promote_from=a.promote_from,
            promoted=[r["id"] for r in survivors],
            assemble=a.assemble,
            spearman_screen_vs_opens=rho_s,
            spearman_screen_vs_opens_n=n_s,
            spearman_proxy_vs_opens=rho_p,
            spearman_proxy_vs_opens_n=n_p,
        )
        save_status()

    if a.generations:
        s2 = _generations(
            a,
            dict(
                out=out,
                inputs=inputs,
                constraints_path=constraints_path,
                dataset=dataset,
                status=status,
                save_status=save_status,
                done_records=done_records,
                native_stage=native_stage,
                flags=flags,
                library=library,
                placed=placed,
                starts=all_starts,
            ),
        )
    if a.stop_after == "native" or not s2:
        return
    survivors = sorted(s2, key=_rank_key("native"))[: a.k3]
    t3 = time.monotonic()
    s3 = native_stage("deep", survivors, a.s3)
    best = min(s3, key=_rank_key("deep")) if s3 else None
    status["stages"]["deep"] = dict(
        n=len(s3),
        seconds=time.monotonic() - t3,
        best=best["id"] if best else None,
        best_objective=best["objective"] if best else None,
    )
    status["finished"] = time.time()
    save_status()


if __name__ == "__main__":
    main()
