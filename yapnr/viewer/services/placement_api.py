"""Versioned agent API for the live lab. Uses the browser's service and canonical notes."""

import hashlib
import json
import math
import re
from urllib.parse import urlencode

from yapnr.placement import VERSION

from .placement_lab import checked, digest, write
from .placement_objective import DEFAULT, TERMS, parse
from .placement_surface_expression import NAMES

EVIDENCE = (
    "request.json",
    "input.json",
    "runtime.json",
    "status.json",
    "control.json",
    "initial.json",
    "frame.json",
    "continuous.json",
    "placed.json",
    "review.json",
    "experiment.json",
)
# Required and optional argument names form the discoverable, strict action contract.
ACTIONS = {
    "schema": ((), ()),
    "catalog": ((), ()),
    "source": (("source",), ()),
    "register-source": (("document",), ()),
    "validate-objective": (("expression",), ()),
    "start": (
        ("source",),
        (
            "seed",
            "iterations",
            "frame_delay_ms",
            "expression",
            "paused",
            "run_forever",
            "annealing",
            "shove",
            "placement_banks",
            "legalize_only",
        ),
    ),
    "status": (("id",), ()),
    "run": (("id",), ()),
    "surface": (("id", "ref", "expression"), ("pitch",)),
    "surface-result": (("id", "key"), ()),
    "surface-render": (("id", "key"), ("rotation",)),
    "decisions": (("id",), ()),
    "decision": (("id",), ("step", "ref")),
    "render": (("id",), ("step", "ref", "alternative", "airwires")),
    "pause": (("id",), ("expect_revision",)),
    "resume": (("id",), ("expect_revision",)),
    "step": (("id",), ("expect_revision", "count")),
    "stop": (("id",), ("expect_revision",)),
    "apply": (("id", "expression"), ("expect_revision",)),
    "annealing": (("id", "annealing"), ("expect_revision",)),
    "compare": (("id", "other"), ()),
    "history": (("id",), ("cursor", "limit")),
    "evidence": (("id", "name"), ()),
    "notes": (("id",), ("ref",)),
    "note": (("id", "body"), ("step", "ref", "alternative")),
    "reply": (("id", "note", "body"), ()),
}


def schema():
    return dict(
        schema=VERSION,
        transport="POST /api/placement-lab/v1 with JSON {action, ...arguments}; allowlisted Origin required",
        actions={k: dict(required=list(v[0]), optional=list(v[1])) for k, v in ACTIONS.items()},
        indices="Decision step and alternative index are zero-based; decision/note require exactly one of step/ref",
        objective=dict(
            default=DEFAULT,
            terms=list(TERMS),
            syntax="nonnegative weighted sums; +, *, parentheses; source_constraints exactly once",
        ),
        annealing=dict(
            modes={
                "hold": {"mode": "hold", "temperature": 1.0},
                "schedule": {
                    "mode": "schedule",
                    "start_temperature": 2.0,
                    "end_temperature": 0.2,
                    "steps": 400,
                },
            },
            temperature_range=[0.05, 10],
            schedule="Applying a schedule restarts it from the recorded current iteration; final temperature is held",
        ),
        surface=dict(
            terms=sorted(NAMES),
            rotations=[0, 90, 180, 270],
            scope="Rigid displayed poses; absolute board cost; separate from live objective edits",
            functions=["abs", "sqrt", "log1p", "min", "max"],
        ),
        limits=dict(
            request_bytes=1000000,
            components=500,
            iterations=5000,
            worker_seconds=600,
            run_forever="Opt-in: no iteration or wall-time limit; iterations is the annealing horizon",
            history_page=100,
        ),
        evidence=list(EVIDENCE),
        qualification="Hard placement checks only; native routing and electrical acceptance remain unverified",
        notes="Always agent-authored; cannot approve, resolve, delete or impersonate user notes",
    )


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


class PlacementAPI:
    def __init__(self, lab, notes):
        self.lab, self.notes = lab, notes

    def call(self, body):
        action = body.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError("Unknown placement action; GET /api/placement-lab/v1 for schema")
        required, optional = ACTIONS[action]
        if set(required) - body.keys() or body.keys() - {"action", *required, *optional}:
            raise ValueError(f"{action}: required {list(required)}, optional {list(optional)}")
        return dict(schema=VERSION, result=self._dispatch(action, body))

    def _dispatch(self, action, b):
        lab = self.lab
        if action == "schema":
            return schema()
        if action == "catalog":
            return lab.catalog()
        if action == "validate-objective":
            return dict(expression=b["expression"], weights=parse(b["expression"]))
        if action == "source":
            ident = b["source"]
            if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
                raise ValueError("Invalid source identity")
            path = lab.root / "sources" / (ident + ".json")
            return dict(id=ident, sha256=digest(path), document=json.loads(path.read_text()))
        if action == "register-source":
            doc = b["document"]
            if not isinstance(doc, dict) or not isinstance(doc.get("constraints"), dict):
                raise ValueError("Source needs a graph and compiled constraints object")
            graph = doc.get("graph")
            if not isinstance(graph, dict) or not isinstance(graph.get("components"), list):
                raise ValueError("Source graph needs components")
            parts = graph["components"]
            if not 1 <= len(parts) <= 500:
                raise ValueError("Source needs 1..500 components")
            refs = [p.get("ref") if isinstance(p, dict) else None for p in parts]
            if any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
                raise ValueError("Component references must be nonempty and unique")
            raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            sha = hashlib.sha256(raw).hexdigest()
            ident = "source-" + sha
            path = lab.root / "sources" / (ident + ".json")
            with lab.lock:
                if path.exists() and path.read_bytes() != raw:
                    raise ValueError("Registered source hash mismatch")
                if not path.exists():
                    # write() uses insertion order; normalize to the hashed canonical order.
                    write(path, json.loads(raw))
            return dict(id=ident, sha256=sha, title=doc.get("title", ident))
        if action in ("start", "apply", "annealing", "pause", "resume", "step", "stop"):
            result = lab.command(b)
            result["view"] = self.link(result["id"])
            return result
        ident = b["id"]
        folder = lab.folder(ident)
        if not (folder / "status.json").is_file():
            raise ValueError("Unknown placement run")
        if action == "surface":
            return lab.surfaces.request(b)
        if action == "surface-result":
            return lab.surfaces.result(ident, b["key"])
        if action == "surface-render":
            from .placement_render import render

            result = lab.surfaces.result(ident, b["key"])
            if result["status"] != "ready":
                raise ValueError("Wait for the surface before rendering")
            rotation = b.get("rotation", 0)
            if type(rotation) is not int or rotation not in (0, 90, 180, 270):
                raise ValueError("Choose a cardinal rotation")
            source = json.loads((folder / "surfaces" / b["key"] / "request.json").read_text())
            svg = render(source["frame"]["graph"], surface=result["report"], rotation=rotation)
            return dict(
                media_type="image/svg+xml",
                svg=svg,
                sha256=hashlib.sha256(svg.encode()).hexdigest(),
                surface_sha256=result["sha256"],
            )
        if action == "history":
            return self.history(folder, b.get("cursor", 0), b.get("limit", 25))
        if action == "evidence":
            name = b["name"]
            if name not in EVIDENCE:
                raise ValueError("Choose a named evidence file from status.evidence")
            raw = (folder / name).read_bytes()
            return dict(name=name, sha256=hashlib.sha256(raw).hexdigest(), document=json.loads(raw))
        if action == "render":
            from .placement_render import render

            d, alt = None, None
            if "step" in b or "ref" in b:
                _, d = self.selected(ident, b)
                graph = d["graph"]
                if "alternative" in b:
                    i = integer(b["alternative"], "alternative", 0, len(d["alternatives"]) - 1)
                    alt = d["alternatives"][i]
            else:
                if "alternative" in b:
                    raise ValueError("Select a decision to render an alternative")
                run = lab.get(ident)
                graph = run.get("frame", {}).get("graph") or run.get("review", {}).get("graph")
                if not graph:
                    raise ValueError("No placement frame yet")
            if "airwires" in b and type(b["airwires"]) is not bool:
                raise ValueError("airwires must be boolean")
            svg = render(graph, d, alt, airwires=b.get("airwires", True))
            return dict(
                media_type="image/svg+xml",
                svg=svg,
                sha256=hashlib.sha256(svg.encode()).hexdigest(),
                decision_sha256=d["decision_sha256"] if d else None,
            )
        if action == "run":
            return lab.get(ident)
        if action == "status":
            run = lab.get(ident)
            return dict(
                id=ident,
                status=run["status"],
                control=run.get("control"),
                evidence=[n for n in EVIDENCE if (folder / n).exists()],
                view=self.link(ident),
            )
        if action == "decisions":
            review = self.review(ident)
            rows = []
            for i, item in enumerate(review["decisions"]):
                d = checked(folder, item)
                rows.append(
                    dict(
                        step=i,
                        ref=d["ref"],
                        decision_sha256=item["sha256"],
                        translation_mm=math.dist(
                            d.get("original_position", d["target"]), d["position"]
                        ),
                        total=d["total"],
                        view=self.link(ident, i),
                    )
                )
            return dict(
                id=ident,
                motion=review.get("motion"),
                backtracks=review.get("backtracks"),
                decisions=rows,
            )
        if action == "compare":
            return self.compare(ident, b["other"])
        if action in ("decision", "note"):
            step, data = self.selected(ident, b)
            if action == "decision":
                return dict(id=ident, step=step, decision=data, view=self.link(ident, step))
            return self.note(ident, step, data, b)
        if self.notes is None:
            raise ValueError("Notes are not enabled on this server")
        if action == "notes":
            self.lab.get(ident)  # Reject invented run identities.
            notes = self.notes.all()
            return dict(
                notes=[
                    n
                    for n in notes
                    if n.get("provenance", {}).get("lane") == ident
                    and (
                        not b.get("ref")
                        or any(t.get("ref") == b["ref"] for t in n.get("targets", []))
                    )
                ]
            )
        if action == "reply":
            try:
                note = self.notes.get(b["note"])
            except LookupError as exc:
                raise ValueError("Unknown note identity") from exc
            if note.get("provenance", {}).get("lane") != ident:
                raise ValueError("Note does not belong to this run")
            return dict(note=self.notes.comment(b["note"], b["body"], self.actor(ident)))
        raise ValueError("Unsupported placement action")

    @staticmethod
    def link(ident, step=None, compare=None):
        q = {"run": ident}
        if step is not None:
            q["step"] = step
        if compare:
            q["compare"] = compare
        return "/placement-lab.html?" + urlencode(q)

    def review(self, ident):
        run = self.lab.get(ident)
        if "review" not in run:
            raise ValueError("No legalization decisions yet; inspect status/history")
        return run["review"]

    def selected(self, ident, b):
        items = self.review(ident)["decisions"]
        if ("step" in b) == ("ref" in b):
            raise ValueError("Provide exactly one of step or ref")
        if "ref" in b:
            hits = [i for i, d in enumerate(items) if d["ref"] == b["ref"]]
            if len(hits) != 1:
                raise ValueError("Reference must identify exactly one decision")
            step = hits[0]
        else:
            step = integer(b["step"], "step", 0, len(items) - 1)
        return step, self.lab.get(ident, step)["decision"]

    @staticmethod
    def actor(ident):
        return dict(kind="agent", name="placement-api", session="placement:" + ident)

    def note(self, ident, step, d, b):
        if self.notes is None:
            raise ValueError("Notes are not enabled on this server")
        alternative = None
        if "alternative" in b:
            i = integer(b["alternative"], "alternative", 0, len(d["alternatives"]) - 1)
            alternative = d["alternatives"][i]
        context = dict(
            run=ident,
            step=step,
            decision_sha256=d["decision_sha256"],
            original=d.get("original_position"),
            chosen=d["position"],
            rotation=d["rotation"],
            terms=d["terms"],
            alternative=alternative,
        )
        note = self.notes.create(
            dict(
                kind="observation",
                title=f"Legalization {d['ref']} · decision {step+1}",
                body=b["body"],
                targets=[
                    dict(kind="component", ref=d["ref"]),
                    dict(
                        kind="probe",
                        label="Recorded legalization decision",
                        summary=json.dumps(context)[:1900],
                    ),
                ],
            ),
            self.actor(ident),
            provenance=dict(
                lane=ident, phase="legalization:" + d["decision_sha256"], view="placement-lab"
            ),
        )
        return dict(note=note, rev=self.notes.rev, view=self.link(ident, step))

    def compare(self, ident, other):
        a, b = self.lab.get(ident), self.lab.get(other)
        am, bm = a.get("review", {}).get("motion", {}), b.get("review", {}).get("motion", {})
        ap, bp = am.get("parts", {}), bm.get("parts", {})
        differences = [
            dict(
                ref=r,
                translation_mm=ap[r][0],
                baseline_translation_mm=bp[r][0],
                delta_mm=ap[r][0] - bp[r][0],
            )
            for r in sorted(ap.keys() & bp.keys())
        ]
        same = a["request"].get("source_sha256") == b["request"].get("source_sha256")
        same = same and a["request"].get("source_sha256") is not None
        return dict(
            id=ident,
            baseline=other,
            same_source=bool(same),
            same_seed=a["request"].get("seed") == b["request"].get("seed"),
            status=a["status"],
            baseline_status=b["status"],
            motion=am,
            baseline_motion=bm,
            delta_translation_mm=(
                am["sum_mm"] - bm["sum_mm"] if "sum_mm" in am and "sum_mm" in bm else None
            ),
            parts=differences,
            only_in_run=sorted(ap.keys() - bp.keys()),
            only_in_baseline=sorted(bp.keys() - ap.keys()),
            view=self.link(ident, compare=other),
            qualification="Geometric placement comparison only; differing inputs are not a controlled A/B",
        )

    @staticmethod
    def history(folder, cursor, limit):
        integer(cursor, "cursor", 0, 2**53 - 1)
        integer(limit, "limit", 1, 100)
        path = folder / "iterations.jsonl"
        if not path.exists():
            if cursor:
                raise ValueError("Cursor exceeds history")
            return dict(events=[], cursor=0, more=False)
        events = []
        with path.open("rb") as f:
            size = path.stat().st_size
            if cursor > size:
                raise ValueError("Cursor exceeds history")
            if cursor:
                f.seek(cursor - 1)
                if f.read(1) != b"\n":
                    raise ValueError("Cursor must be a returned line boundary")
            f.seek(cursor)
            start = cursor
            for _ in range(limit):
                line = f.readline(2000000)
                if not line.endswith(b"\n"):
                    break  # Retry a concurrently written partial record on the next poll.
                events.append(json.loads(line))
                cursor = f.tell()
                if cursor - start >= 1000000:
                    break
        return dict(events=events, cursor=cursor, more=cursor < size)
