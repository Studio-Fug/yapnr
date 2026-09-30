"""Provenance of a place-and-route result and its critical path (``pnr-provenance-v1``).

A run is a DAG of *experiments* (a global placement, a legalization, a detailed route, a
native KiCad stage), *artifacts* (the source board, the final board) and *selections* (a
ranking that picks one candidate), linked by *derive* edges (the input of an experiment) and
*select* edges (candidate to selection, selection to chosen).

The **critical path** of a final artifact is its derivation ancestry, where a selection
contributes only its chosen candidate; the other candidates are that selection's
*competitors*. Post-order keeps each input's ancestry contiguous, so independent inputs become
consecutive runs, in time order, before their consumer. :func:`critical_path` also returns
where each visit began, which is where a storyboard shows the competitors of a selection
before the winner's own replay.

Adapters build DAGs from a ``pnr-trace-v1`` directory (:func:`from_trace`), from the saved
results of an untraced ladder case (:func:`coarse_ladder_trace`, "reconstructed from saved
results"), from a successive-halving run (:func:`from_halving`) and from a hierarchical
synthesis library (:func:`from_synthesis`). Everything here only reads its sources.
Stdlib only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

SCHEMA = "pnr-provenance-v1"


# --- the DAG -----------------------------------------------------------------------------
class Node:
    """A DAG node: ``kind`` is artifact, experiment or selection."""

    def __init__(self, id, kind, label=None, stage=None, order=0, scope=None, **fields):
        self.id = id
        self.kind = kind
        self.label = label or id.rsplit("/", 1)[-1]
        self.stage = stage
        self.order = order
        self.scope = scope
        self.status = fields.pop("status", "ok")
        self.metrics = fields.pop("metrics", {})
        self.paths = fields.pop("paths", {})
        self.candidates = list(fields.pop("candidates", []))
        self.chosen = fields.pop("chosen", None)
        self.selected = list(fields.pop("selected", []))
        self.criterion = fields.pop("criterion", None)
        self.scores = dict(fields.pop("scores", {}))
        self.meta = dict(fields.pop("meta", {}))
        self.meta.update(fields)

    def to_json(self):
        doc = dict(id=self.id, kind=self.kind, label=self.label, order=self.order)
        for key in ("stage", "scope", "chosen", "criterion"):
            if getattr(self, key) is not None:
                doc[key] = getattr(self, key)
        if self.status != "ok":
            doc["status"] = self.status
        for key in ("metrics", "paths", "candidates", "selected", "scores", "meta"):
            if getattr(self, key):
                doc[key] = getattr(self, key)
        return doc


class Dag:
    """Nodes by id, derive inputs per node, select edges on the selection nodes."""

    def __init__(self):
        self.nodes = {}
        self.inputs = {}

    def add(self, id, kind, **fields):
        if id in self.nodes:
            raise ValueError("duplicate provenance node " + id)
        node = Node(id, kind, **fields)
        self.nodes[id] = node
        self.inputs.setdefault(id, [])
        return node

    def get(self, id):
        return self.nodes.get(id)

    def derive(self, source, target):
        if source not in self.nodes or target not in self.nodes:
            raise KeyError("derive edge between unknown nodes %s -> %s" % (source, target))
        if source not in self.inputs[target]:
            self.inputs[target].append(source)

    def select(self, selection, candidates, chosen):
        node = self.nodes[selection]
        node.candidates = [c for c in candidates if c in self.nodes]
        node.chosen = chosen if chosen in self.nodes else None

    def derive_inputs(self, node):
        return [self.nodes[i] for i in self.inputs.get(node.id, [])]

    def to_json(self):
        return dict(
            schema=SCHEMA,
            nodes=[n.to_json() for n in sorted(self.nodes.values(), key=lambda n: (n.order, n.id))],
            derive=[[s, t] for t in sorted(self.inputs) for s in self.inputs[t]],
        )


def critical_path(dag, final, roots_first=True):
    """``(order, competitors, entry)`` for the artifact ``final``.

    ``order`` lists the nodes of the path in post-order (inputs before their consumer, the
    chosen candidate before its selection); ``competitors`` maps each selection on the path to
    its candidates that are not on it; ``entry`` maps each node to the index in ``order``
    where its visit began (the start of its exclusive ancestry). With ``roots_first``,
    ancestors without inputs (the source board) come first, so they do not belong to the
    first chosen candidate's ancestry."""
    final = dag.nodes[final] if isinstance(final, str) else final
    order, entry, seen = [], {}, set()

    def ancestors(node, found):
        if node.id in found:
            return
        found.add(node.id)
        if node.kind == "selection" and node.chosen:
            ancestors(dag.nodes[node.chosen], found)
        for source in dag.derive_inputs(node):
            ancestors(source, found)

    if roots_first:
        found = set()
        ancestors(final, found)
        roots = [
            dag.nodes[i]
            for i in found
            if not dag.inputs.get(i) and not (dag.nodes[i].kind == "selection")
        ]
        for root in sorted(roots, key=lambda n: (n.order, n.id)):
            if root.id != final.id:
                entry[root.id] = len(order)
                seen.add(root.id)
                order.append(root)

    def visit(node):
        if node.id in seen:
            return
        seen.add(node.id)
        entry[node.id] = len(order)
        if node.kind == "selection" and node.chosen:
            visit(dag.nodes[node.chosen])
        for source in sorted(dag.derive_inputs(node), key=lambda n: (n.order, n.id)):
            visit(source)
        order.append(node)

    visit(final)
    on_path = {n.id for n in order}
    competitors = {}
    for node in order:
        if node.kind == "selection":
            competitors[node.id] = [c for c in node.candidates if c not in on_path]
    return order, competitors, entry


# --- traces ------------------------------------------------------------------------------
class Scope:
    """A scope of a trace: its type, parent, meta, status and own events."""

    def __init__(self, id, type, parent, meta, lane, seq):
        self.id, self.type, self.parent, self.meta, self.lane = id, type, parent, meta, lane
        self.status, self.metrics = "open", {}
        self.events = []
        self.children = []
        self.begin = seq
        self.end = None


class Trace:
    """A loaded ``pnr-trace-v1`` directory (or one reconstructed in memory)."""

    def __init__(self, root=None, header=None, run=None, lanes=None, blobs=None):
        self.root = Path(root) if root is not None else None
        self.coarse = False
        self._blobs = dict(blobs or {})
        if root is not None:
            header = json.loads((self.root / "header.json").read_text())
            run_path = self.root / "run.json"
            run = json.loads(run_path.read_text()) if run_path.is_file() else {}
            lanes = {}
            for path in sorted((self.root / "streams").glob("*.jsonl")):
                name = path.name[: -len(".jsonl")]
                lanes[name] = [json.loads(line) for line in path.read_text().splitlines() if line]
        self.header = header
        self.run = run or {}
        self.lanes = lanes or {}
        self.scopes = {}
        self.selects = []
        self.boards = []
        self.results = []
        for lane in sorted(self.lanes, key=lambda n: (n != "engine", n)):
            self._index(lane, self.lanes[lane])

    def _index(self, lane, events):
        for event in events:
            event["lane"] = lane
            kind = event["kind"]
            if kind == "scope_begin":
                scope = Scope(
                    event["scope"],
                    event.get("type"),
                    event.get("parent", ""),
                    event.get("meta", {}),
                    lane,
                    event["seq"],
                )
                self.scopes[scope.id] = scope
                if scope.parent in self.scopes:
                    self.scopes[scope.parent].children.append(scope.id)
                continue
            if kind == "scope_end":
                scope = self.scopes.get(event["scope"])
                if scope is not None:
                    scope.status = event.get("status", "ok")
                    scope.metrics = event.get("metrics", {})
                    scope.end = event["seq"]
                continue
            scope = self.scopes.get(event.get("scope", ""))
            if scope is not None:
                scope.events.append(event)
            if kind == "select":
                self.selects.append(event)
            elif kind == "board":
                self.boards.append(event)
            elif kind == "result":
                self.results.append(event)

    def blob(self, digest):
        if digest is None:
            return None
        if digest not in self._blobs:
            path = self.root / "blobs" / (digest + ".json")
            self._blobs[digest] = json.loads(path.read_text())
        return self._blobs[digest]

    def events(self, lane="engine"):
        return self.lanes.get(lane, [])

    def of_type(self, type):
        return [s for s in self.scopes.values() if s.type == type]

    def kind(self, scope_id, kind):
        scope = self.scopes.get(scope_id)
        return [e for e in scope.events if e["kind"] == kind] if scope else []

    def descendants(self, scope_id):
        out, pending = [], [scope_id]
        while pending:
            sid = pending.pop()
            out.append(sid)
            pending.extend(self.scopes[sid].children if sid in self.scopes else [])
        return out


def _round_of(trace, scope_id):
    while scope_id:
        scope = trace.scopes.get(scope_id)
        if scope is None:
            return None
        if scope.type == "round":
            return scope_id
        scope_id = scope.parent
    return None


def _sibling(scope_id, name):
    return scope_id.rsplit("/", 1)[0] + "/" + name if "/" in scope_id else name


def from_trace(trace):
    """The DAG of a traced ladder case (engine and native lanes)."""
    dag = Dag()
    dag.add("source", "artifact", label="source board", stage="source", order=-1)
    for scope in sorted(trace.scopes.values(), key=lambda s: s.begin):
        if scope.lane != "engine":
            continue
        status = scope.status if scope.status != "open" else "ok"
        stage = {"start": "place", "attempt": "place", "route": "route"}.get(scope.type)
        if stage is None:
            continue
        dag.add(
            scope.id,
            "experiment",
            stage=stage,
            order=scope.begin,
            scope=scope.id,
            status=status,
            metrics=scope.metrics,
            meta=scope.meta,
        )
        if scope.type == "start":
            dag.derive("source", scope.id)
    # Selections; a multi-member choice (a shortlist) becomes one node per member.
    members = {}
    for event in trace.selects:
        chosen = event.get("chosen")
        common = dict(
            order=event["seq"],
            criterion=event.get("criterion"),
            scores=event.get("scores", {}),
            label=event["id"].rsplit("/", 1)[-1],
        )
        among = [_candidate(trace, c) for c in event.get("among", [])]
        if isinstance(chosen, list):
            picked = [_candidate(trace, c) for c in chosen]
            for member in picked:
                if member is None or member not in dag.nodes:
                    continue
                sid = "%s[%s]" % (event["id"], member)
                dag.add(sid, "selection", selected=picked, **common)
                dag.select(sid, [a for a in among if a], member)
                members[member] = sid
        else:
            sid = event["id"]
            dag.add(sid, "selection", selected=[_candidate(trace, chosen)], **common)
            dag.select(sid, [a for a in among if a], _candidate(trace, chosen))
    # Finalist routes derive from their start through the shortlist.
    for scope in trace.of_type("route"):
        start = scope.meta.get("start")
        if start and scope.id in dag.nodes:
            start_id = _sibling(scope.id, start)
            source = members.get(start_id, start_id)
            if source in dag.nodes:
                dag.derive(source, scope.id)
    # Rounds: attempts (first legal wins) or the pool's choice, then the round's route.
    rounds = sorted(trace.of_type("round"), key=lambda s: s.begin)
    previous = None
    for scope in rounds:
        route = next(
            (c for c in scope.children if trace.scopes[c].type == "route" and c in dag.nodes),
            None,
        )
        attempts = [
            c for c in scope.children if trace.scopes[c].type == "attempt" and c in dag.nodes
        ]
        pools = [c for c in scope.children if trace.scopes[c].type == "pool"]
        placement = None
        if attempts:
            for attempt in attempts:
                dag.derive(previous or "source", attempt)
            legal = [a for a in attempts if dag.nodes[a].status == "ok"]
            if len(attempts) > 1 and legal:
                placement = scope.id + "/first-legal"
                dag.add(placement, "selection", order=trace.scopes[legal[-1]].end or 0)
                dag.nodes[placement].criterion = "first-legal"
                dag.select(placement, attempts, legal[-1])
            elif legal:
                placement = legal[-1]
        elif pools:
            chosen = [
                e for e in trace.selects if e["id"].startswith(pools[0] + "/") and e is not None
            ]
            chosen = [e for e in chosen if not isinstance(e.get("chosen"), list)]
            if chosen and chosen[-1]["id"] in dag.nodes:
                placement = chosen[-1]["id"]
        elif trace.kind(scope.id, "poses"):
            placement = scope.id + "/placement"
            poses = trace.kind(scope.id, "poses")[-1]
            dag.add(placement, "experiment", stage="place", order=poses["seq"], scope=scope.id)
            dag.derive(previous or "source", placement)
        if route is not None and placement is not None:
            dag.derive(placement, route)
        if route is not None:
            previous = route
    # The best round, the native stages and the final board.
    head = previous
    best = [e for e in trace.selects if e["id"].rsplit("/", 1)[-1] == "best-round"]
    if best and best[-1]["id"] in dag.nodes and dag.nodes[best[-1]["id"]].chosen:
        head = best[-1]["id"]
    for event in trace.boards:
        if event.get("lane") != "native":
            continue
        nid = "native:" + event["stage"]
        if nid in dag.nodes:
            continue
        drc = event.get("drc") or {}
        dag.add(
            nid,
            "experiment",
            stage="native",
            label=event["stage"],
            order=10**9 + event["seq"],
            metrics=dict(unconnected=drc.get("unconnected"), violations=drc.get("violations")),
            event=event["seq"],
        )
        if head is not None:
            dag.derive(head, nid)
        head = nid
    result = trace.results[-1] if trace.results else {}
    dag.add(
        "final",
        "artifact",
        label="final board",
        stage="final",
        order=10**10,
        status="ok" if result.get("passed", True) else "failed",
        metrics={k: result.get(k) for k in ("opens", "violations", "vias", "copper_length_mm")},
    )
    if head is not None:
        dag.derive(head, "final")
    return dag


def _candidate(trace, scope_id):
    """The DAG node a selection candidate names: a round names its route."""
    if scope_id is None:
        return None
    scope = trace.scopes.get(scope_id)
    if scope is not None and scope.type == "round":
        for child in scope.children:
            if trace.scopes[child].type == "route":
                return child
    return scope_id


# --- coarse reconstruction of an untraced ladder case ------------------------------------
def coarse_ladder_trace(case_dir):
    """A :class:`Trace` rebuilt from what an untraced ladder case saved (round poses and
    routes, the saved board and its DRC); marked ``coarse``."""
    from pnr.graph import BoardGraph
    from pnr.trace import board_header, canonical, graph_poses, um
    from pnr.trace_board import read

    case = Path(case_dir)
    spec = json.loads((case / "design.json").read_text())
    graph = BoardGraph.from_json((case / "source-graph.json").read_text())
    rules_path = case / "rules.json"
    rules = json.loads(rules_path.read_text()) if rules_path.is_file() else {}

    class _Board:
        width = spec["constraints"]["board"]["outline"]["w"]
        height = spec["constraints"]["board"]["outline"]["h"]
        layers = spec["constraints"]["board"]["layers"]

    class _Constraints:
        board = _Board()
        constraints = []

    header = board_header(graph, _Constraints(), rules)
    fixed = set(spec["constraints"].get("fixed", {}))
    for comp in header["components"]:
        comp["fixed"] = comp["ref"] in fixed
    source_board = case / "source.kicad_pcb"
    if source_board.is_file():
        from pnr.trace_board import refine_header

        refine_header(header, read(source_board))
    blobs, events, seq = {}, [], [0]
    pins = {n["name"]: n["pins"] for n in header["nets"]}
    layers = header["copper_layers"]
    fab = rules.get("fab") or {}
    via = [um(fab.get("via_diameter_mm", 0.6)), um(fab.get("via_drill_mm", 0.3))]

    def blob(obj):
        import hashlib

        digest = hashlib.sha256(canonical(obj)).hexdigest()
        blobs[digest] = obj
        return digest

    def emit(kind, scope, **fields):
        fields.update(kind=kind, scope=scope, seq=seq[0])
        seq[0] += 1
        events.append(fields)

    total = header["connections_total"]
    rounds = sorted((case / "rounds").glob("round-*")) if (case / "rounds").is_dir() else []
    report_path = case / "pnr-report.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    for folder in rounds:
        rid = folder.name
        emit("scope_begin", rid, type="round", parent="", meta={})
        if (folder / "placed.json").is_file():
            placed = BoardGraph.from_json((folder / "placed.json").read_text())
            emit("poses", rid, stage="round", poses=graph_poses(placed), phase="placement")
        route_id = rid + "/route"
        emit("scope_begin", route_id, type="route", parent=rid, meta={})
        emit("route_begin", route_id, phase="commit", progress=dict(done=0, total=total))
        routes_path = folder / "routes.json"
        if routes_path.is_file():
            routes = json.loads(routes_path.read_text())
            per_net, order = {}, []
            for net, layer, a, b, w in routes.get("tracks", []):
                if net not in per_net:
                    order.append(net)
                row = [layers.index(layer) if layer in layers else 0]
                row += [um(a[0]), um(a[1]), um(b[0]), um(b[1]), um(w)]
                per_net.setdefault(net, ([], []))[0].append(row)
            for net, x, y in routes.get("vias", []):
                if net not in per_net:
                    order.append(net)
                per_net.setdefault(net, ([], []))[1].append([um(x), um(y)] + via)
            unrouted = set(routes.get("unrouted", []))
            done = 0
            nets = {}
            for net in order:
                tracks, vias = per_net[net]
                digest = blob(dict(tracks=sorted(tracks), vias=sorted(vias), zones=[]))
                nets[net] = digest
                count = len(pins.get(net, []))
                groups = (
                    [list(range(count))] if net not in unrouted else [[i] for i in range(count)]
                )
                done += max(0, count - len(groups))
                emit(
                    "net",
                    route_id,
                    net=net,
                    op="commit",
                    provisional=False,
                    copper=digest,
                    groups=groups,
                    progress=dict(done=done, total=total, source="router"),
                    phase="commit",
                    **{"pass": 0},
                )
            emit(
                "route_end",
                route_id,
                nets=nets,
                groups={},
                unrouted=sorted(unrouted),
                deferred=routes.get("deferred", []),
                progress=dict(done=done, total=total, source="router"),
                phase="routed",
            )
        emit("scope_end", route_id, type="route", status="ok", metrics={})
        emit("scope_end", rid, type="round", status="ok", metrics={})
    if rounds:
        names = [f.name for f in rounds]
        best = report.get("best_round") or len(names)
        emit(
            "select",
            "",
            id="best-round",
            among=names,
            chosen="round-%02d" % best,
            criterion="missing-connections",
            scores=dict(zip(names, report.get("connection_history", []))),
            phase="selection",
        )
    native = []
    board = case / "routed.kicad_pcb"
    if board.is_file():
        parsed = read(board)
        drc_path = case / "drc.json"
        drc = json.loads(drc_path.read_text()) if drc_path.is_file() else None
        from pnr.trace import drc_summary

        fields = dict(
            stage="refill",
            phase="refill",
            copper=blob(parsed["copper"]),
            poses=parsed["poses"],
            lane="native",
            seq=len(native),
        )
        if drc is not None:
            summary = drc_summary(drc, parsed["frame"])
            fields["drc"] = summary
            fields["progress"] = dict(
                done=max(0, total - summary["unconnected"]), total=total, source="kicad"
            )
        native.append(dict(fields, kind="board", scope=""))
    result_path = case / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text())
        native.append(
            dict(
                kind="result",
                scope="",
                seq=len(native),
                phase="result",
                case=result.get("case"),
                seed=result.get("seed"),
                passed=bool(result.get("passed")),
                reasons=result.get("reasons", []),
                opens=result.get("opens"),
                violations=result.get("violations"),
                vias=result.get("vias"),
                copper_length_mm=result.get("copper_length_mm"),
            )
        )
    run = dict(
        subject=dict(
            kind="ladder-case",
            case=spec["name"],
            seed=json.loads(result_path.read_text()).get("seed") if result_path.is_file() else 0,
            description=spec["description"],
            parts=len(spec["parts"]),
            layers=spec["constraints"]["board"]["layers"],
        ),
        config={},
    )
    trace = Trace(header=header, run=run, lanes=dict(engine=events, native=native), blobs=blobs)
    trace.coarse = True
    return trace


# --- successive halving and hierarchical synthesis ---------------------------------------
STAGE_ORDER = ("place", "screen", "native", "deep")


def _objective_key(record):
    objective = record.get("objective")
    if isinstance(objective, list) and objective:
        return tuple(float("inf") if v is None else v for v in objective)
    for key in ("proxy_score", "cheap_score"):
        value = record.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return (value,)
    return (float("inf"),)


def read_dataset(path):
    """The records of a ``dataset.jsonl`` (unreadable lines skipped)."""
    records = []
    for line in Path(path).read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def from_halving(run_dir):
    """The DAG of a successive-halving run: a rung per stage, promotions as selections,
    generation parents as derive edges; the final artifact is the best deep (else native)
    candidate."""
    run = Path(run_dir)
    records = read_dataset(run / "dataset.jsonl")
    dag = Dag()
    dag.add("source", "artifact", label="source board", stage="source", order=-1)
    by_stage = {}
    order = 0
    for record in records:
        stage = record.get("stage")
        if stage == "gen-place":
            stage = "place"
        if stage not in STAGE_ORDER or "id" not in record:
            continue
        order += 1
        nid = "halving:%s/%s" % (record["id"], stage)
        if nid in dag.nodes:
            continue
        status = "ok" if record.get("status") in ("ok", "legal") else "failed"
        paths = {}
        if (run / "cand" / record["id"]).is_dir():
            paths["candidate"] = "cand/" + record["id"]
        dag.add(
            nid,
            "experiment",
            stage=stage,
            label=record["id"],
            order=order,
            status=status,
            paths=paths,
            candidate=record["id"],
            objective=record.get("objective"),
            parent=record.get("parent"),
        )
        by_stage.setdefault(stage, []).append((record, nid))
    for record, nid in by_stage.get("place", []):
        parent = record.get("parent")
        parent_node = None
        for stage in reversed(STAGE_ORDER):
            candidate = "halving:%s/%s" % (parent, stage)
            if parent and candidate in dag.nodes:
                parent_node = candidate
                break
        dag.derive(parent_node or "source", nid)
    # Promotion: a rung derives from the candidate's highest earlier rung (generation
    # children may skip the screen) through a selection among that rung's candidates.
    for k, upper in enumerate(STAGE_ORDER[1:], start=1):
        for record, nid in by_stage.get(upper, []):
            lower = next(
                (
                    stage
                    for stage in reversed(STAGE_ORDER[:k])
                    if "halving:%s/%s" % (record["id"], stage) in dag.nodes
                ),
                None,
            )
            if lower is None:
                continue
            below = "halving:%s/%s" % (record["id"], lower)
            pool = [n for _r, n in by_stage.get(lower, []) if dag.nodes[n].status == "ok"]
            scores = {n: list(_objective_key(r)) for r, n in by_stage.get(lower, [])}
            promoted = ["halving:%s/%s" % (r["id"], lower) for r, _n in by_stage.get(upper, [])]
            sid = "halving:promote-%s[%s]" % (upper, record["id"])
            dag.add(
                sid,
                "selection",
                label="promote to " + upper,
                order=dag.nodes[nid].order - 0.5,
                criterion=lower + "-objective",
                scores={k: v for k, v in scores.items() if all(math.isfinite(x) for x in v)},
                selected=[n for n in promoted if n in dag.nodes],
            )
            dag.select(sid, pool, below)
            dag.derive(sid, nid)
    final_pool = by_stage.get("deep") or by_stage.get("native") or by_stage.get("screen") or []
    ok = [(r, n) for r, n in final_pool if dag.nodes[n].status == "ok"]
    dag.add("final", "artifact", label="final board", stage="final", order=10**9)
    if ok:
        best_record, best = min(ok, key=lambda item: (_objective_key(item[0]), item[1]))
        stage = dag.nodes[best].stage
        sid = "halving:best-" + stage
        dag.add(
            sid,
            "selection",
            label="best " + stage,
            order=10**9 - 1,
            criterion=stage + "-objective",
            scores={n: list(_objective_key(r)) for r, n in final_pool},
        )
        dag.select(sid, [n for _r, n in final_pool], best)
        dag.derive(sid, "final")
        dag.nodes["final"].metrics = dict(objective=best_record.get("objective"))
    return dag


def from_synthesis(library_dir):
    """The DAG of a hierarchical synthesis library: per template, a selection among its
    ranked layouts (rank 0 chosen); the final artifact is the set of chosen layouts."""
    root = Path(library_dir)
    dag = Dag()
    dag.add("source", "artifact", label="block templates", stage="source", order=-1)
    dag.add("final", "artifact", label="block library", stage="final", order=10**9)
    order = 0
    for library in sorted(root.glob("*/library.json")) + (
        [root / "library.json"] if (root / "library.json").is_file() else []
    ):
        doc = json.loads(library.read_text())
        template = str(doc.get("template_id") or library.parent.name)
        ranked = doc.get("ranked") or []
        ids = []
        for rank, layout in enumerate(ranked):
            order += 1
            tag = str(layout.get("tag") or layout.get("seed") or rank)
            nid = "block:%s/%s" % (template, tag)
            if nid in dag.nodes:
                continue
            dirs = [i.get("dir") for i in layout.get("instances") or [] if i.get("dir")]
            dag.add(
                nid,
                "experiment",
                stage="block",
                label="%s #%d" % (template, rank + 1),
                order=order,
                rank=rank,
                objective=layout.get("objective"),
                native_dirs=[Path(d).name for d in dirs],
            )
            dag.derive("source", nid)
            ids.append(nid)
        if not ids:
            continue
        sid = "block:%s/rank" % template
        dag.add(
            sid,
            "selection",
            label=template + " ranking",
            order=order + 0.5,
            criterion="native-rank",
            scores={i: dag.nodes[i].meta.get("rank") for i in ids},
        )
        dag.select(sid, ids, ids[0])
        dag.derive(sid, "final")
    return dag


def detect(path):
    """The kind of provenance source at ``path``: trace, ladder-case, ladder-run, halving,
    synthesis, or None."""
    path = Path(path)
    if (path / "header.json").is_file() and (path / "streams").is_dir():
        return "trace"
    if (path / "design.json").is_file() and (path / "source-graph.json").is_file():
        return "ladder-case"
    if (path / "trace" / "header.json").is_file():
        return "ladder-case"
    if (path / "summary.json").is_file() and (path / "provenance.json").is_file():
        return "ladder-run"
    if (path / "dataset.jsonl").is_file():
        return "halving"
    if (path / "library.json").is_file() or any(path.glob("*/library.json")):
        return "synthesis"
    return None
