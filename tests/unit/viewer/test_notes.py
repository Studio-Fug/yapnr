"""Notes store and notes MCP server regression (offline, stdlib)."""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from yapnr.viewer.notes import mcp as notes_mcp
from yapnr.viewer.notes import store as notes_store
from yapnr.viewer.notes.store import NoteConflict, NoteNotFound, NotesStore
from yapnr.viewer.testing import child_env, module_argv

STORE_CLI = module_argv("yapnr.viewer.notes.store")
MCP = module_argv("yapnr.viewer.notes.mcp")
USER = dict(kind="user", remote="127.0.0.1")
AGENT = dict(kind="agent", session="11111111-2222-3333-4444-555555555555")
INDEX = dict(
    sha="f00d",
    components={
        "C17": dict(
            type="C22u",
            instance="board.converter.output_cap1",
            file="system_5v.ato",
            line=70,
            pins={"1": dict(pin="p1", net="p5v-hv"), "2": dict(pin="p2", net="lv")},
        ),
        "U5": dict(
            type="TPS552882",
            instance="board.converter.ic",
            file="system_5v.ato",
            line=30,
            pins={"13": dict(pin="VOUT", net="p5v-hv")},
        ),
    },
    nets={
        "p5v-hv": dict(title="5V LED rail (board.p5v.hv)", kind="power"),
        "lv": dict(title="GND", kind="ground"),
    },
    files=[dict(path="system_5v.ato", lines=200)],
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="notes-test-")
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, **kw):
        kw.setdefault("resolver", INDEX)
        return NotesStore(self.dir / "notes", **kw)


class StoreTest(Base):
    def test_create_update_comment_delete_and_files(self):
        s = self.store()
        a = s.create(
            dict(
                title="  Ripple\non the LED rail ",
                body="Measured **120 mV**",
                targets=[
                    dict(kind="net", name="p5v-hv"),
                    dict(kind="pad", ref="U5", pad=13),
                    dict(kind="net", name="p5v-hv"),
                ],
                tags=["power", "Power", "hot loop"],
            ),
            AGENT,
            provenance=dict(
                session=AGENT["session"],
                turn=2,
                lane="nb6/x",
                phase="live",
                board_sha="abcdef0123",
                viewer_port=8791,
            ),
        )
        self.assertEqual(
            (a["id"], a["status"], a["author"], a["kind"], a["title"]),
            ("N-0001", "open", "agent", "observation", "Ripple on the LED rail"),
        )
        self.assertEqual(
            a["targets"], [dict(kind="net", name="p5v-hv"), dict(kind="pad", ref="U5", pad="13")]
        )
        self.assertEqual(a["tags"], ["power", "hot loop"])
        self.assertEqual(
            a["provenance"],
            dict(
                session=AGENT["session"],
                turn=2,
                lane="nb6/x",
                phase="live",
                board_sha="abcdef0123",
                viewer_port=8791,
            ),
        )
        b = s.create(
            dict(
                title="Widen VBUS neck",
                proposal=dict(type="ato", summary="3 A contract", diff="- a\n+ b"),
            ),
            AGENT,
        )
        self.assertEqual((b["id"], b["kind"], b["status"]), ("N-0002", "proposal", "proposed"))
        c = s.create(dict(title="Decided: keep 2 layers", kind="decision", status="accepted"), USER)
        self.assertEqual((c["status"], c["author"]), ("accepted", "user"))
        s.comment("N-0001", "scope shot attached", USER)
        n = s.update("N-0002", dict(status="accepted"), USER)
        self.assertEqual((n["status"], n["status_by"]), ("accepted", "user"))
        self.assertEqual(s.delete("N-0003", USER)["deleted"], True)
        self.assertRaises(NoteNotFound, s.get, "N-0003")
        self.assertEqual(s.create(dict(title="next"), USER)["id"], "N-0004")  # ids are never reused
        log = [
            json.loads(line) for line in (self.dir / "notes/notes.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [e["op"] for e in log],
            ["create", "create", "create", "comment", "update", "delete", "create"],
        )
        self.assertEqual([e["rev"] for e in log], list(range(1, 8)))
        self.assertEqual(log[0]["actor"], AGENT)
        self.assertEqual(log[4]["actor"], USER)
        j = json.loads((self.dir / "notes/notes.json").read_text())
        self.assertEqual(
            (j["schema"], j["rev"], [n["id"] for n in j["notes"]]),
            ("yapnr-notes-v1", 7, ["N-0001", "N-0002", "N-0004"]),
        )
        md = (self.dir / "notes/design-notes.md").read_text()
        for needle in (
            "## Accepted (to apply) (1)",
            "### N-0002 · proposal · Widen VBUS neck",
            "- proposal (ato): 3 A contract",
            "> ```diff\n> - a\n> + b\n> ```",
            "## Open questions / observations (2)",
            "net p5v-hv: 5V LED rail (board.p5v.hv)",
            "pad U5.13 (VOUT) -> net p5v-hv",
            "author agent (session 11111111, turn 2)",
            "context: lane nb6/x · phase live · board abcdef0123 · viewer port 8791",
            "> Measured **120 mV**",
            "user: scope shot attached",
            "(set by user",
        ):
            self.assertIn(needle, md)
        self.assertLess(md.index("## Accepted"), md.index("## Proposed"))
        self.assertNotIn("keep 2 layers", md)
        fresh = NotesStore(self.dir / "notes")
        self.assertEqual(fresh.all(), s.all())  # replay of the log == materialized state

    def test_authority(self):
        s = self.store()
        u = s.create(dict(title="user note"), USER)
        a = s.create(dict(title="agent note"), AGENT)
        for st in ("accepted", "rejected", "applied", "resolved"):
            self.assertRaises(PermissionError, s.create, dict(title="x", status=st), AGENT)
            self.assertRaises(PermissionError, s.update, a["id"], dict(status=st), AGENT)
        self.assertRaises(PermissionError, s.update, a["id"], dict(status="open"), AGENT)
        self.assertRaises(PermissionError, s.update, u["id"], dict(title="hijack"), AGENT)
        self.assertRaises(PermissionError, s.delete, a["id"], AGENT)
        self.assertRaises(
            PermissionError, s.update, a["id"], dict(provenance=dict(lane="x")), AGENT
        )
        self.assertRaises(
            PermissionError,
            s.create,
            dict(title="x", links=[dict(kind="applied_in", ref="abc123")]),
            AGENT,
        )
        self.assertRaises(PermissionError, s.create, dict(title="x"), dict(kind="admin"))
        n = s.update(
            a["id"],
            dict(
                proposal=dict(type="constraint", summary="keep U5 near L1"),
                targets=[dict(kind="component", ref="U5")],
            ),
            AGENT,
        )
        self.assertEqual(n["status"], "proposed")
        n = s.update(a["id"], dict(proposal=None), AGENT)
        self.assertEqual(n["status"], "open")
        self.assertNotIn("proposal", n)
        s.comment(u["id"], "agent may comment on any note", AGENT)
        s.update(a["id"], dict(status="rejected"), USER)
        self.assertRaises(PermissionError, s.update, a["id"], dict(body="sneaky"), AGENT)
        s.comment(a["id"], "still allowed", AGENT)
        self.assertEqual(
            s.update(a["id"], dict(status="open"), USER)["status"], "open"
        )  # reopen is a user action
        self.assertEqual(
            s.update(
                u["id"],
                dict(
                    title="edited",
                    links=[dict(kind="applied_in", ref="commit 1a2b", note="done")],
                    status="applied",
                ),
                USER,
            )["links"][0]["kind"],
            "applied_in",
        )
        rev = s.get(a["id"])["rev"]
        self.assertRaises(
            NoteConflict, s.update, a["id"], dict(title="x"), USER, expect_rev=rev - 1
        )
        s.update(a["id"], dict(title="y"), USER, expect_rev=rev)
        # agent may retitle its own note while open/proposed; no-op updates write nothing
        b = s.create(dict(title="mine"), AGENT)
        r0 = s.rev
        s.update(b["id"], dict(title="mine"), AGENT)
        self.assertEqual(s.rev, r0)
        self.assertEqual(s.update(b["id"], dict(title="mine 2"), AGENT)["title"], "mine 2")

    def test_validation_and_resolution(self):
        s = self.store()
        bad = [
            dict(),
            dict(title=""),
            dict(title="x" * 121),
            dict(title="x", body="y" * 8001),
            dict(title="x", kind="rumour"),
            dict(title="x", targets=[dict(kind="component")]),
            dict(title="x", targets=[dict(kind="source", file="/etc/x.ato", line=1)]),
            dict(title="x", targets=[dict(kind="source", file="a.ato", line=0)]),
            dict(title="x", targets=[dict(kind="region", bbox=[0, 1, 2])]),
            dict(title="x", targets=[dict(kind="net", name="a]b")]),
            dict(title="x", targets=[{}] * 41),
            dict(title="x", sources=[dict(url="javascript:alert(1)")]),
            dict(title="x", sources=["ftp://x.y/z"]),
            dict(title="x", tags=["<b>"]),
            dict(title="x", proposal=dict(type="magic", summary="s")),
            dict(title="x", proposal=dict(type="ato")),
            dict(title="x", nonsense=1),
            dict(title="x", proposal=dict(type="ato", summary="s", extra=1)),
        ]
        for f in bad:
            self.assertRaises(ValueError, s.create, f, USER)
        # agents must name things that exist; users are not second-guessed (regions may hold refs
        # outside the source index)
        for t, msg in (
            (dict(kind="component", ref="C999"), "component C999"),
            (dict(kind="pad", ref="U5", pad="99"), "pad U5.99"),
            (dict(kind="net", name="vbus"), "net vbus"),
            (dict(kind="source", file="nope.ato", line=1), "source file nope.ato"),
            (
                dict(kind="source", file="system_5v.ato", line=150, end=250),
                "line system_5v.ato:250",
            ),
            (dict(kind="region", refs=["C17", "R1"], nets=["lv"]), "component R1"),
        ):
            with self.assertRaises(ValueError) as cm:
                s.create(dict(title="x", targets=[t]), AGENT)
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(
            s.create(dict(title="x", targets=[dict(kind="component", ref="C999")]), USER)[
                "targets"
            ],
            [dict(kind="component", ref="C999")],
        )
        n = s.create(
            dict(
                title="web",
                sources=[
                    "https://www.ti.com/product/TPS552882",
                    dict(url="https://example.com/a", title="Ex [1]"),
                ],
                targets=[
                    dict(
                        kind="region",
                        lane="nb6/x",
                        bbox=[1, 2.5, 3, 4],
                        refs=["C17"],
                        nets=["lv"],
                        extra="dropped",
                    ),
                    dict(kind="source", file="system_5v.ato", line=5, end=5),
                    dict(kind="group", id="g1", label="hot [loop]", refs=["U5"]),
                ],
            ),
            AGENT,
        )
        self.assertEqual(
            n["sources"],
            [
                dict(url="https://www.ti.com/product/TPS552882", title="www.ti.com"),
                dict(url="https://example.com/a", title="Ex [1]"),
            ],
        )
        self.assertEqual(
            n["targets"],
            [
                dict(
                    kind="region",
                    lane="nb6/x",
                    refs=["C17"],
                    nets=["lv"],
                    bbox=[1.0, 2.5, 3.0, 4.0],
                ),
                dict(kind="source", file="system_5v.ato", line=5),
                dict(kind="group", id="g1", label="hot (loop)", refs=["U5"]),
            ],
        )
        # graph.json resolver (no source lines) and SourceService-like objects
        g = self.dir / "graph.json"
        g.write_text(
            json.dumps(
                dict(
                    components=[
                        dict(ref="C1", address="board.c1._p", pads=[dict(name="1", net="hv")])
                    ],
                    nets=[dict(name="hv", pins=[["C1", "1"]])],
                )
            )
        )
        r = notes_store.resolver_for(g)
        self.assertEqual(
            (r.component("C1")["instance"], r.net("hv") is not None, r.lines("x.ato")),
            ("board.c1", True, float("inf")),
        )

        class Src:
            def index(self):
                return INDEX

        r = notes_store.resolver_for(Src())
        self.assertEqual(r.lines("system_5v.ato"), 200)
        self.assertIsNone(r.lines("other.ato"))
        c = notes_store.compact(dict(INDEX, files=[dict(path="system_5v.ato", lines=200, sha="x")]))
        self.assertEqual(c["files"], {"system_5v.ato": 200})
        self.assertEqual(notes_store.resolver_for(c).component("U5")["pins"]["13"]["net"], "p5v-hv")

    def test_since_payload_relevant_find(self):
        s = self.store()
        self.assertEqual(json.loads(s.payload(None)), dict(rev=0, notes=[]))
        a = s.create(dict(title="C17 ESR", targets=[dict(kind="component", ref="C17")]), AGENT)
        s.create(
            dict(title="rail", body="LED supply ripple", targets=[dict(kind="net", name="p5v-hv")]),
            USER,
        )
        c = s.create(dict(title="lane note", kind="question"), AGENT, provenance=dict(lane="nb6/x"))
        r = s.rev
        self.assertEqual(json.loads(s.payload(r)), dict(rev=r, unchanged=True))
        self.assertLess(len(s.payload(r)), 40)
        s.delete(c["id"], USER)
        s.comment(a["id"], "x", USER)
        d = json.loads(s.payload(r))
        self.assertEqual(
            (d["rev"], d["changed"], d["deleted"], [n["id"] for n in d["notes"]]),
            (r + 2, ["N-0001"], ["N-0003"], ["N-0001", "N-0002"]),
        )
        self.assertNotIn("changed", json.loads(s.payload(-1)))
        self.assertEqual(len(json.loads(s.payload(None))["notes"]), 2)
        self.assertEqual(s.listing(s.rev), dict(rev=s.rev, unchanged=True))
        hits, recent = s.relevant(
            [dict(kind="pad", ref="C17", pad="1")]
        )  # the pad's part and (via the resolver) its net
        self.assertEqual(
            sorted((n["id"], w) for n, w in hits), [("N-0001", ["C17"]), ("N-0002", ["net p5v-hv"])]
        )
        self.assertEqual(recent, [])
        e = s.create(dict(title="lane q2"), AGENT, provenance=dict(lane="nb6/x"))
        self.assertEqual([n["id"] for n in s.relevant([], "nb6/x")[1]], [e["id"]])
        self.assertEqual([n["id"] for n in s.find(query="led ripple")], ["N-0002"])
        self.assertEqual([n["id"] for n in s.find(ref="C17")], ["N-0001"])
        self.assertEqual([n["id"] for n in s.find(author="user")], ["N-0002"])
        self.assertEqual(len(s.find(status="open", limit=1)), 1)

    def test_shared_directory_concurrency_and_crash(self):
        s1 = self.store()
        s2 = NotesStore(self.dir / "notes")
        s1.create(dict(title="one"), USER)
        self.assertEqual([n["title"] for n in s2.all()], ["one"])
        s2.create(dict(title="two"), USER)
        self.assertEqual(s1.get("N-0002")["title"], "two")
        code = (
            "import sys;from yapnr.viewer.notes import store as n;s=n.NotesStore(%r)\n"
            "for i in range(25):s.create(dict(title='p%%s-%%d'%%(sys.argv[1],i)),"
            "dict(kind='agent',session=sys.argv[1]))" % str(self.dir / "notes")
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(k)], env=child_env())
            for k in range(3)
        ]
        ths = [
            threading.Thread(
                target=lambda: [s1.create(dict(title=f"t{i}"), USER) for i in range(25)]
            )
        ]
        [t.start() for t in ths]
        for p in procs:
            self.assertEqual(p.wait(60), 0)
        for t in ths:
            t.join(60)
        ids = [n["id"] for n in NotesStore(self.dir / "notes").all()]
        self.assertEqual(len(ids), 102)
        self.assertEqual(len(set(ids)), 102)
        self.assertEqual(s2.refresh(), 102)
        log = self.dir / "notes/notes.jsonl"
        revs = [json.loads(line)["rev"] for line in log.read_text().splitlines()]
        self.assertEqual(revs, list(range(1, 103)))
        # a writer died mid-line: readers ignore the torn tail, the next writer drops it
        with open(log, "ab") as f:
            f.write(b'{"rev":103,"op":"create","id":"N-0103","fields":{"ti')
        s3 = NotesStore(self.dir / "notes")
        self.assertEqual(s3.refresh(), 102)
        n = s3.create(dict(title="after crash"), USER)
        self.assertEqual((n["id"], s3.rev), ("N-0103", 103))
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 103)
        self.assertTrue(all(json.loads(line) for line in lines))
        self.assertEqual(s1.get("N-0103")["title"], "after crash")
        self.assertFalse(list((self.dir / "notes").glob(".*.tmp")))
        (self.dir / "notes/notes.json").write_text('{"rev":1}')
        NotesStore(self.dir / "notes")
        self.assertEqual(
            json.loads((self.dir / "notes/notes.json").read_text())["rev"], 103
        )  # stale derived files are rebuilt

    def test_export_request_and_report_cli(self):
        s = self.store()
        s.create(dict(title="acc", targets=[dict(kind="component", ref="C17")]), USER)
        s.create(dict(title="prop", proposal=dict(type="engine", summary="raise K")), AGENT)
        with self.assertRaises(ValueError) as cm:
            s.request(
                "N-0001", dict(fields=dict(status="accepted")), USER
            )  # accept/apply over HTTP: the rev the user reviewed
        self.assertIn("expect_rev", str(cm.exception))
        n = s.request(
            "N-0001", dict(fields=dict(status="accepted"), comment="go", expect_rev=1), USER
        )
        self.assertEqual((n["status"], len(n["comments"])), ("accepted", 1))
        # status + comment are one step: a bad comment leaves the status alone
        self.assertRaises(
            ValueError,
            s.request,
            "N-0002",
            dict(fields=dict(status="rejected"), comment="r" * 4001),
            USER,
        )
        self.assertEqual((s.get("N-0002")["status"], s.get("N-0002")["comments"]), ("proposed", []))
        r = s.rev
        n = s.request("N-0002", dict(fields=dict(status="rejected"), comment="Rejected: no"), USER)
        self.assertEqual(
            (n["status"], n["comments"][0]["text"], s.rev), ("rejected", "Rejected: no", r + 2)
        )
        s.request("N-0002", dict(fields=dict(status="proposed")), USER)
        self.assertEqual(
            s.request("N-0002", dict(comment="only a comment"), USER)["status"], "proposed"
        )
        for bad in (dict(), dict(bogus=1), dict(fields=dict(status="done"))):
            self.assertRaises(ValueError, s.request, "N-0001", bad, USER)
        self.assertRaises(NoteNotFound, s.request, "N-0099", dict(title="x"), USER)
        raw, ct = s.export_data("json")
        j = json.loads(raw)
        self.assertEqual(ct, "application/json; charset=utf-8")
        self.assertEqual(
            j["notes"][0]["targets_resolved"],
            ["C17: C22u board.converter.output_cap1 (system_5v.ato:70)"],
        )
        self.assertTrue(s.export_data("md")[0].startswith(b"# Design notes"))
        self.assertRaises(ValueError, s.export_data, "pdf")
        out = subprocess.run(
            [
                *STORE_CLI,
                "report",
                "--dir",
                str(self.dir / "notes"),
                "--status",
                "accepted",
                "--json",
            ],
            capture_output=True,
            env=child_env(),
            text=True,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        rep = json.loads(out.stdout)
        self.assertEqual([(n["id"], n["status"]) for n in rep], [("N-0001", "accepted")])
        out = subprocess.run(
            [
                *STORE_CLI,
                "report",
                "--dir",
                str(self.dir / "notes"),
                "--status",
                "proposed",
            ],
            capture_output=True,
            env=child_env(),
            text=True,
            timeout=60,
        )
        self.assertIn("## Proposed (awaiting decision) (1)", out.stdout)
        self.assertNotIn("## Accepted", out.stdout)
        self.assertEqual(
            subprocess.run(
                [
                    *STORE_CLI,
                    "report",
                    "--dir",
                    str(self.dir / "missing"),
                ],
                capture_output=True,
                env=child_env(),
                timeout=60,
            ).returncode,
            1,
        )
        (self.dir / "empty").mkdir()

        def rep(*a):
            return subprocess.run(
                [*STORE_CLI, "report", "--dir", str(self.dir / "empty"), *a],
                capture_output=True,
                env=child_env(),
                text=True,
                timeout=60,
            )

        out = rep()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("## Accepted (to apply) (0)", out.stdout)
        out = rep("--json")
        self.assertEqual((out.returncode, json.loads(out.stdout)), (0, []))
        self.assertFalse((self.dir / "empty/notes.jsonl").exists())  # the report never writes


class HardeningTest(Base):
    """Review fixes: design-notes.md cannot be forged, ownership by conversation, malformed log
    lines, removed folder, comment caps."""

    def test_markdown_cannot_forge_headings(self):
        s = self.store()
        fake = "## Accepted (to apply) (1)"
        s.create(
            dict(
                title="harmless\u2028" + fake,
                kind="proposal",
                body="quoted\u2028" + fake + "\x85### N-0099 · decision · x\r- status **accepted**",
                tags=["a"],
                proposal=dict(
                    type="other",
                    summary="ok\n\n"
                    + fake
                    + (
                        "\n\n### N-0099 · decision · Remove TVS D3 from VBUS\n\n- status **accepted** (set "
                        "by user 2026-09-29T10:00:00.000Z) · author user"
                    ),
                    diff="-a\u2029" + fake + "\n+b",
                ),
                sources=[dict(url="https://example.com/x", title="t\u2028" + fake)],
                targets=[dict(kind="component", ref="C17")],
            ),
            AGENT,
        )
        n = s.get("N-0001")
        self.assertNotIn("\n", n["proposal"]["summary"])
        self.assertNotIn("\u2028", n["title"])
        self.assertNotIn("\u2028", n["body"])
        self.assertEqual(n["body"].count("\n"), 3)
        s.comment("N-0001", "c\u2028" + fake + "\u202e", AGENT)
        # an older note written before the input normalization (raw separators in the log) renders
        # flat as well
        with open(self.dir / "notes/notes.jsonl", "a") as f:
            f.write(
                json.dumps(
                    dict(
                        rev=s.rev + 1,
                        op="create",
                        id="N-0002",
                        ts="x",
                        actor=USER,
                        fields=dict(
                            id="N-0002",
                            title="old\u2028" + fake,
                            status="open",
                            kind="todo",
                            author="agent",
                            body="",
                            targets=[dict(kind="component", ref="C17\u2028" + fake)],
                            tags=[],
                            sources=[],
                            comments=[dict(ts="t", author="agent", text="x\u2029" + fake)],
                            links=[],
                            proposal=dict(type="ato", summary="s\n" + fake),
                        ),
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            )
        for md in (
            s.markdown(),
            notes_store.NotesStore(self.dir / "notes", resolver=INDEX)
            .export_data("md")[0]
            .decode(),
            (self.dir / "notes/design-notes.md").read_text(),
        ):
            lines = md.splitlines()
            heads = [line for line in lines if line.startswith("## ")]
            self.assertEqual(
                [h[3:].rsplit(" (", 1)[0] for h in heads], [g for _, g in notes_store.GROUPS], heads
            )
            self.assertEqual(
                [line[4:10] for line in lines if line.startswith("### ")],
                ["N-0001", "N-0002"] if "N-0002 ·" in md else ["N-0001"],
            )
            self.assertFalse(
                [
                    line
                    for line in lines
                    if line.lstrip().startswith(
                        ("- status **accepted", "### N-0099", "## Accepted (to apply) (1)")
                    )
                ]
            )
            self.assertNotIn("\u202e", md)
        self.assertIn("## Accepted (to apply) (0)", s.markdown())

    def test_agent_edits_only_its_own_conversation(self):
        s = self.store()
        a = dict(kind="agent", session="aaaaaaaa-0000-4000-8000-000000000001")
        b = dict(kind="agent", session="bbbbbbbb-0000-4000-8000-000000000002")
        n = s.create(dict(title="mine", proposal=dict(type="ato", summary="x", diff="-a\n+b")), a)
        self.assertEqual(n["provenance"], dict(session=a["session"]))
        with self.assertRaises(PermissionError) as cm:
            s.update(n["id"], dict(proposal=dict(type="ato", summary="x", diff="-a\n+EVIL")), b)
        self.assertIn("another conversation", str(cm.exception))
        self.assertRaises(PermissionError, s.update, n["id"], dict(title="t"), dict(kind="agent"))
        s.comment(n["id"], "other conversation may comment", b)
        self.assertEqual(s.get(n["id"])["proposal"]["diff"], "-a\n+b")
        m = s.update(n["id"], dict(title="mine v2"), a)
        self.assertEqual(m["updated_by"], dict(kind="agent", session=a["session"]))
        self.assertIn("by agent (session aaaaaaaa)", s.markdown())
        self.assertEqual(s.update(n["id"], dict(tags=["x"]), USER)["updated_by"], dict(kind="user"))

    def test_malformed_lines_are_skipped(self):
        s = self.store()
        s.create(dict(title="good"), USER)
        log = self.dir / "notes/notes.jsonl"
        with open(log, "a") as f:
            for e in (
                dict(rev=2, op="create", id="N-0002", fields="oops"),
                dict(rev=3, op="update", id=["x"], fields={}),
                dict(rev=4, op="comment", id="N-0001", fields=[1]),
                [1, 2],
                dict(rev=6, op="create", id="N-0003", fields=dict(title="t")),
                dict(rev=7, op="update", id="N-0001", fields=dict(status=None)),
                dict(rev=8, op="frobnicate", id="N-0001"),
                dict(rev=9, op="update", id="N-0001", fields=dict(title="still fine")),
            ):
                f.write(json.dumps(e) + "\n")
        t = notes_store.NotesStore(self.dir / "notes", resolver=INDEX)
        self.assertEqual(
            [(n["id"], n["title"], n["status"]) for n in t.all()],
            [("N-0001", "still fine", "open")],
        )
        self.assertEqual((t.rev, len(t.skipped)), (9, 7))
        self.assertEqual(json.loads(t.payload(0))["skipped"], 7)
        s.refresh()
        self.assertEqual(s.get("N-0001")["title"], "still fine")
        n = t.create(dict(title="after"), USER)
        self.assertEqual((n["id"], t.rev), ("N-0004", 10))  # N-0003's id was seen: never reused
        out = subprocess.run(
            [
                *STORE_CLI,
                "report",
                "--dir",
                str(self.dir / "notes"),
            ],
            capture_output=True,
            env=child_env(),
            text=True,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("still fine", out.stdout)

    def test_removed_folder_and_comment_caps(self):
        s = self.store()
        s.create(dict(title="one"), USER)
        shutil.rmtree(self.dir / "notes")
        self.assertEqual(s.listing(), dict(rev=0, notes=[]))
        self.assertEqual(s.create(dict(title="two"), USER)["title"], "two")
        self.assertTrue((self.dir / "notes/notes.jsonl").is_file())
        with mock.patch.dict(notes_store.MAX, comments=5, agent_comments=2):
            for i in range(2):
                s.comment("N-0001", "a", AGENT)
            with self.assertRaises(ValueError) as cm:
                s.comment("N-0001", "a3", AGENT)
            self.assertIn("assistant comments", str(cm.exception))
            for i in range(3):
                s.comment(
                    "N-0001", "u", USER
                )  # user comments are not crowded out by the assistant's
            self.assertRaises(ValueError, s.comment, "N-0001", "u6", USER)


class McpTest(Base):
    def env(self, **kw):
        (self.dir / "resolver.json").write_text(json.dumps(notes_store.compact(INDEX)))
        return child_env(
            YAPNR_NOTES_DIR=str(self.dir / "notes"),
            YAPNR_SESSION=AGENT["session"],
            YAPNR_TURN="3",
            YAPNR_LANE="nb6/x",
            YAPNR_PHASE="live",
            YAPNR_VIEWER_PORT="8791",
            YAPNR_BOARD_SHA="ABCDEF0123",
            YAPNR_RESOLVER=str(self.dir / "resolver.json"),
            **kw,
        )

    def rpc(self, msgs, **kw):
        lines = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs)
        p = subprocess.run(
            MCP,
            input=lines,
            capture_output=True,
            text=True,
            env=self.env(**kw),
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def call(self, i, name, args):
        return dict(
            jsonrpc="2.0", id=i, method="tools/call", params=dict(name=name, arguments=args)
        )

    def test_protocol(self):
        out = self.rpc(
            [
                dict(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params=dict(
                        protocolVersion="2025-06-18",
                        capabilities={},
                        clientInfo=dict(name="t", version="1"),
                    ),
                ),
                dict(jsonrpc="2.0", method="notifications/initialized"),
                dict(jsonrpc="2.0", id=2, method="tools/list"),
                dict(jsonrpc="2.0", id=3, method="ping"),
                "not json",
                dict(jsonrpc="2.0", id=4, method="resources/list"),
                self.call(5, "drop_tables", {}),
                dict(
                    jsonrpc="2.0",
                    id=6,
                    method="initialize",
                    params=dict(protocolVersion="1999-01-01"),
                ),
                [
                    dict(jsonrpc="2.0", id=7, method="ping"),
                    dict(jsonrpc="2.0", method="notifications/cancelled"),
                ],
                dict(id=8, method="ping"),
                [],
            ]
        )
        self.assertEqual(
            out[-1],
            dict(
                jsonrpc="2.0",
                id=None,
                error=dict(code=-32600, message="invalid request: empty batch"),
            ),
        )
        out = out[:-1]
        by = {m.get("id"): m for m in out if isinstance(m, dict)}
        self.assertEqual(by[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(by[1]["result"]["capabilities"], dict(tools=dict(listChanged=False)))
        self.assertIn("only propose", by[1]["result"]["instructions"])
        self.assertEqual(
            [t["name"] for t in by[2]["result"]["tools"]],
            ["add_note", "list_notes", "get_note", "comment_note", "update_note"],
        )
        self.assertEqual(by[3]["result"], {})
        self.assertEqual(by[4]["error"]["code"], -32601)
        self.assertEqual(by[5]["error"]["code"], -32602)
        self.assertEqual(
            by[6]["result"]["protocolVersion"], "2025-11-25"
        )  # unsupported: the newest
        self.assertEqual(by[None]["error"]["code"], -32700)
        self.assertEqual(by[8]["error"]["code"], -32600)
        self.assertEqual(out[-2], [dict(jsonrpc="2.0", id=7, result={})])
        self.assertEqual(len(out), 10 - 1)  # the notification got no answer

    def test_tools_provenance_and_authority(self):
        NotesStore(self.dir / "notes").create(dict(title="user note"), USER)
        out = self.rpc(
            [
                self.call(
                    1,
                    "add_note",
                    dict(
                        title="ESR check",
                        kind="question",
                        targets=[dict(kind="component", ref="C17")],
                        sources=[dict(url="https://example.com/ds", title="DS")],
                    ),
                ),
                self.call(
                    2,
                    "add_note",
                    dict(title="bad ref", targets=[dict(kind="component", ref="C999")]),
                ),
                self.call(3, "add_note", dict(title="accept me", status="accepted")),
                self.call(4, "update_note", dict(id="N-0001", title="hijack")),
                self.call(5, "update_note", dict(id="N-0002", status="applied")),
                self.call(6, "comment_note", dict(id="N-0001", text="agent comment")),
                self.call(
                    7,
                    "update_note",
                    dict(id="N-0002", proposal=dict(type="ato", summary="use 22 uF")),
                ),
                self.call(8, "list_notes", dict(ref="C17")),
                self.call(9, "get_note", dict(id="N-0404")),
                self.call(10, "add_note", dict(title="x", provenance=dict(lane="forged"))),
            ]
        )
        res = {m["id"]: (m["result"]["isError"], m["result"]["content"][0]["text"]) for m in out}
        self.assertFalse(res[1][0])
        self.assertEqual(json.loads(res[1][1])["id"], "N-0002")
        for i, needle in (
            (2, "unknown component C999"),
            (3, "only the user can mark a note accepted"),
            (4, "written by the user"),
            (5, "only the user can mark a note applied"),
            (9, "no note N-0404"),
        ):
            self.assertTrue(res[i][0], i)
            self.assertIn(needle, res[i][1])
        self.assertFalse(res[6][0])
        self.assertEqual(json.loads(res[7][1])["status"], "proposed")
        self.assertEqual([n["id"] for n in json.loads(res[8][1])["notes"]], ["N-0002"])
        n = NotesStore(self.dir / "notes").get("N-0002")
        self.assertEqual(
            n["provenance"],
            dict(
                session=AGENT["session"],
                turn=3,
                lane="nb6/x",
                phase="live",
                board_sha="abcdef0123",
                viewer_port=8791,
            ),
        )
        self.assertEqual(n["sources"], [dict(url="https://example.com/ds", title="DS")])
        self.assertEqual(
            NotesStore(self.dir / "notes").get("N-0003")["provenance"]["lane"], "nb6/x"
        )  # model-supplied provenance is ignored
        ev = [
            json.loads(line) for line in (self.dir / "notes/notes.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            {(e["actor"]["kind"], e["actor"].get("session")) for e in ev[1:]},
            {("agent", AGENT["session"])},
        )
        self.assertEqual(
            NotesStore(self.dir / "notes").get("N-0001")["comments"][0]["author"], "agent"
        )

    def test_wrong_argument_types_are_tool_errors(self):
        NotesStore(self.dir / "notes").create(
            dict(title="user note", targets=[dict(kind="component", ref="U5")]), USER
        )
        out = self.rpc(
            [
                self.call(1, "list_notes", dict(ref=["U5"])),
                self.call(2, "list_notes", dict(query=5)),
                self.call(3, "list_notes", dict(limit="x")),
                self.call(4, "add_note", dict(title=5)),
                self.call(5, "comment_note", dict(id="N-0001", text=["x"])),
                self.call(6, "add_note", dict(title="t", targets="C17")),
                self.call(7, "list_notes", dict(ref="U5")),
            ]
        )
        res = {m["id"]: m.get("result") for m in out}
        self.assertTrue(all("result" in m for m in out), out)
        for i, needle in (
            (1, "ref must be a string"),
            (2, "query must be a string"),
            (3, "limit must be an integer"),
            (4, "title must be a string"),
            (5, "text must be a string"),
            (6, "targets must be an array"),
        ):
            self.assertTrue(res[i]["isError"], i)
            self.assertIn(needle, res[i]["content"][0]["text"])
        self.assertEqual(json.loads(res[7]["content"][0]["text"])["count"], 1)

    def test_limits_and_missing_dir(self):
        out = self.rpc([self.call(i, "add_note", dict(title=f"n{i}")) for i in range(12)])
        self.assertEqual([m["result"]["isError"] for m in out], [False] * 10 + [True] * 2)
        self.assertIn("at most 10", out[-1]["result"]["content"][0]["text"])
        env = self.env()
        env.pop("YAPNR_NOTES_DIR")
        p = subprocess.run(
            MCP,
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("YAPNR_NOTES_DIR", p.stderr)
        a, prov, d, res = notes_mcp.env_context(
            dict(YAPNR_NOTES_DIR="/x", YAPNR_BOARD_SHA="zz; rm", YAPNR_TURN="two")
        )
        self.assertEqual((prov, a), ({}, dict(kind="agent")))


if __name__ == "__main__":
    unittest.main()
