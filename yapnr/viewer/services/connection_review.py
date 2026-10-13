"""Content-pinned connection review cards and append-only human verdicts.

Agent annotations are hypotheses. Agree/disagree reviews never approve a board,
change a requirement, or satisfy an electrical validator. Geometry is read only
from the viewer's content-addressed store, never from a client-supplied path.
"""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path

from yapnr.feedback import Feedback

VERSION = "yapnr-connection-review-v1"
SHA = re.compile(r"[a-f0-9]{64}\Z")
_LOCK = threading.RLock()


class Conflict(ValueError):
    pass


def schema():
    return dict(
        schema=VERSION,
        transport="POST /api/connection-review/v1; allowlisted Origin",
        actions={
            "scan": ["scope?"],
            "list": ["scope?"],
            "card": ["id"],
            "annotate": ["id", "expect_revision", "annotation"],
            "history": ["id?"],
            "respond": ["id", "expect_revision", "body", "status"],
        },
        annotation_fields=[
            "hypothesis",
            "question",
            "suggestion",
            "confidence",
            "bounds",
            "refs",
            "nets",
            "evidence",
        ],
        coordinates="mm-y-up",
        authority="Agent API cannot submit human verdicts",
        evidence="Unconnected island spans are observations; suggested routes are not validated copper",
    )


def _text(v, limit=8000):
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError("Expected bounded text")
    return v


def _bounds(value):
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in value
        )
        or value[0] >= value[2]
        or value[1] >= value[3]
    ):
        raise ValueError("Expected [left,bottom,right,top] in mm-y-up")
    return value


class ConnectionReview:
    def __init__(self, root):
        self.root = Path(root)
        self.folder = self.root / "connection-review"
        self.folder.mkdir(exist_ok=True)
        self.log = self.folder / "events.jsonl"

    @contextlib.contextmanager
    def locked(self):
        with _LOCK, (self.folder / "store.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def events(self):
        if not self.log.exists():
            return []
        raw = self.log.read_bytes()
        return [json.loads(line) for line in raw.splitlines(keepends=True) if line.endswith(b"\n")]

    def replay(self):
        cards = {}
        events = self.events()
        for event in events:
            card_id = event["id"]
            if event["op"] == "discover":
                cards[card_id] = dict(
                    event["card"],
                    revision=event["revision"],
                    definition_revision=event["revision"],
                    decision=None,
                )
            elif event["op"] == "annotate":
                cards[card_id].update(
                    annotation=event["annotation"],
                    revision=event["revision"],
                    definition_revision=event["revision"],
                )
            elif event["op"] == "respond":
                cards[card_id].update(followup=event["followup"], revision=event["revision"])
            else:
                cards[card_id].update(decision=event["decision"], revision=event["revision"])
        for card in cards.values():
            decision = card.get("decision")
            card["review_state"] = (
                "unreviewed"
                if not decision
                else (
                    "outdated"
                    if decision["definition_revision"] != card["definition_revision"]
                    else decision["verdict"]
                )
            )
        feedback = Feedback(self.root)
        known = feedback.replay()
        for card in cards.values():
            decision = card.get("decision")
            identity = "review-" + card["id"]
            actionable = decision and decision["verdict"] in ("agree", "different")
            if actionable or identity in known:
                source = dict(
                    card_id=card["id"],
                    board_sha256=card["board_sha256"],
                    definition_revision=card["definition_revision"],
                    annotation=card["annotation"],
                    decision=decision,
                )
                text = (
                    (
                        decision.get("body")
                        or card["annotation"]["hypothesis"]
                        or "Review the marked rectangles"
                    )
                    if actionable
                    else "Review withdrawn"
                )
                prior = known.get(identity)
                item = feedback.add(text, source, identity, prior["revision"] if prior else None)
                if not actionable and not item.get("paused"):
                    item = feedback.control(identity, item["revision"], "pause")
                card["resolution"] = feedback.query(identity)
        return cards

    def append(self, event):
        events = self.events()
        event.update(revision=(events[-1]["revision"] if events else 0) + 1, time=time.time())
        raw = (json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n").encode()
        with self.log.open("a+b") as stream:
            stream.seek(0)
            prior = stream.read()
            if prior and not prior.endswith(b"\n"):
                stream.truncate(prior.rfind(b"\n") + 1)
                stream.seek(0, 2)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def geometry(self, sha):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError("Invalid board hash")
        path = self.root / "geometry" / (sha + ".json")
        if not path.is_file() or path.is_symlink():
            raise ValueError("Immutable board geometry is unavailable")
        return json.loads(path.read_text())

    def scan(self, lanes, scope=""):
        _text(scope, 300)
        added = 0
        with self.locked():
            known = self.replay()
            for lane_id, lane in sorted(lanes.items()):
                if scope and lane_id != scope and not lane_id.startswith(scope.rstrip("/") + "/"):
                    continue
                finals = [f for f in lane.get("frames", []) if "final" in f.get("name", "")]
                frame = finals[-1] if finals else None
                if frame is None:
                    continue  # An initial unrouted placement is not a failure case.
                sha = frame.get("board_sha256")
                try:
                    geometry = self.geometry(sha)
                except ValueError:
                    continue
                by_net = {}
                for span in geometry.get("airwires", []):
                    by_net.setdefault(span["net"], []).append(span)
                for net, spans in sorted(by_net.items()):
                    identity = hashlib.sha256(
                        json.dumps([lane_id, sha, net], separators=(",", ":")).encode()
                    ).hexdigest()
                    if identity in known:
                        continue
                    points = [s[end] for s in spans for end in ("a", "b")]
                    xs, ys = zip(*points)
                    bounds = [min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2]
                    refs = sorted(
                        {
                            p["ref"]
                            for p in geometry.get("parts", [])
                            if any(
                                q.get("net") == net
                                and any(math.dist(q["xy"], end) < 0.01 for end in points)
                                for q in p.get("pads", [])
                            )
                        }
                    )
                    card = dict(
                        id=identity,
                        lane=lane_id,
                        board_sha256=sha,
                        event_id=frame.get("event_id"),
                        phase=frame.get("name"),
                        net=net,
                        spans=spans,
                        facts=dict(
                            unconnected_spans=len(spans),
                            native_opens=frame.get("opens"),
                            native_violations=frame.get("violations"),
                            minimum_span_mm=min(math.dist(s["a"], s["b"]) for s in spans),
                        ),
                        annotation=dict(
                            hypothesis="",
                            question="Where is access blocked, and which move would open it?",
                            suggestion="Compare pad escape, alternate layers and coordinated displacement.",
                            confidence="unassessed",
                            bounds=bounds,
                            refs=refs,
                            nets=[net],
                            evidence=[],
                        ),
                    )
                    self.append(dict(op="discover", id=identity, card=card, actor="engine"))
                    added += 1
        return dict(added=added, **self.list(scope))

    def list(self, scope=""):
        _text(scope, 300)
        cards = [
            c
            for c in self.replay().values()
            if not scope or c["lane"] == scope or c["lane"].startswith(scope.rstrip("/") + "/")
        ]
        cards.sort(
            key=lambda c: (not bool(c["annotation"]["hypothesis"]), c["lane"], c["net"], c["id"])
        )
        return dict(schema=VERSION, cards=cards, total=len(cards))

    def card(self, identity):
        if not isinstance(identity, str) or not SHA.fullmatch(identity):
            raise ValueError("Invalid card ID")
        if identity not in self.replay():
            raise ValueError("Unknown card")
        return self.replay()[identity]

    def annotate(self, identity, revision, annotation):
        with self.locked():
            card = self.card(identity)
            if card["revision"] != revision:
                raise Conflict("Card changed; reload before annotating")
            if not isinstance(annotation, dict) or set(annotation) - set(
                schema()["annotation_fields"]
            ):
                raise ValueError("Unknown annotation fields")
            value = dict(card["annotation"], **annotation)
            for key in ("hypothesis", "question", "suggestion"):
                _text(value[key])
            if value["confidence"] not in ("unassessed", "low", "medium", "high"):
                raise ValueError("Invalid confidence")
            _bounds(value["bounds"])
            geometry = self.geometry(card["board_sha256"])
            refs = {p["ref"] for p in geometry.get("parts", [])}
            nets = {q["net"] for p in geometry.get("parts", []) for q in p.get("pads", [])}
            for key, allowed in (("refs", refs), ("nets", nets)):
                if (
                    not isinstance(value[key], list)
                    or len(value[key]) > 60
                    or any(x not in allowed for x in value[key])
                ):
                    raise ValueError("Unknown highlighted " + key)
            if not isinstance(value["evidence"], list) or len(value["evidence"]) > 20:
                raise ValueError("Invalid evidence")
            for item in value["evidence"]:
                _text(item, 2000)
            self.append(dict(op="annotate", id=identity, annotation=value, actor="agent"))
            return self.card(identity)

    def decide(self, identity, revision, verdict, body="", rectangles=()):
        with self.locked():
            card = self.card(identity)
            if card["revision"] != revision:
                raise Conflict("Card changed; reload before reviewing")
            if verdict not in ("agree", "different", "skip", "undo"):
                raise ValueError("Invalid verdict")
            _text(body)
            if verdict == "agree" and not card["annotation"]["hypothesis"].strip():
                raise ValueError("There is no agent hypothesis to agree with yet")
            if verdict == "different" and not body.strip() and not rectangles:
                raise ValueError(
                    "Add a conclusion or rectangle before submitting different findings"
                )
            if not isinstance(rectangles, (list, tuple)) or len(rectangles) > 20:
                raise ValueError("Too many rectangles")
            for rect in rectangles:
                _bounds(rect)
            decision = (
                None
                if verdict == "undo"
                else dict(
                    verdict=verdict,
                    body=body,
                    rectangles=rectangles,
                    definition_revision=card["definition_revision"],
                )
            )
            self.append(dict(op="decision", id=identity, decision=decision, actor="user"))
            return self.card(identity)

    def respond(self, identity, revision, body, status):
        """Acknowledge designer feedback without rewriting their reviewed hypothesis."""
        with self.locked():
            card = self.card(identity)
            if card["revision"] != revision:
                raise Conflict("Card changed; reload before responding")
            _text(body)
            if not body.strip() or status not in ("planned", "working", "tested", "blocked"):
                raise ValueError("Expected a response and explicit work status")
            self.append(
                dict(
                    op="respond",
                    id=identity,
                    actor="agent",
                    followup=dict(
                        body=body,
                        status=status,
                        reviewed_revision=revision,
                        definition_revision=card["definition_revision"],
                    ),
                )
            )
            return self.card(identity)

    def call(self, body, lanes=None):
        action = body.get("action")
        if action == "respond":
            return self.respond(
                body.get("id"), body.get("expect_revision"), body.get("body"), body.get("status")
            )
        if action == "schema":
            return schema()
        if action == "scan":
            return self.scan(lanes or {}, body.get("scope", ""))
        if action == "list":
            return self.list(body.get("scope", ""))
        if action == "card":
            return self.card(body.get("id"))
        if action == "history":
            return dict(
                events=[e for e in self.events() if not body.get("id") or e["id"] == body["id"]]
            )
        if action == "annotate":
            return self.annotate(
                body.get("id"), body.get("expect_revision"), body.get("annotation")
            )
        raise ValueError("Unknown agent action; human verdicts require the review UI")
