"""HTTP ingestion regression; no KiCad process or routing changes required."""

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ServerIngestionTest(unittest.TestCase):
    def test_missing_label_replays_as_inspectable_phase_and_snapshots_work(self):
        repo = Path(os.environ.get("PNR_VIEWER_TEST_REPO", Path(__file__).resolve().parents[3]))
        prefs = repo / "output/pnr-settings.json"
        before = prefs.read_bytes()
        with tempfile.TemporaryDirectory(prefix="viewer-schema-") as tmp:
            root = Path(tmp) / "live"
            for directory in ("events", "geometry"):
                (root / directory).mkdir(parents=True)
            sha = "a" * 64
            geometry = dict(
                frame="mm-y-up", width=70, height=55, parts=[], tracks=[], vias=[], zones=[]
            )
            (root / "geometry" / (sha + ".json")).write_text(json.dumps(geometry))
            event = dict(
                schema="pnr-live-event-v1",
                id="event-01",
                time=1,
                kind="phase_complete",
                candidate="underbody123/start02",
                iteration="probe",
                board="cached-board.kicad_pcb",
                board_sha256=sha,
                data=dict(opens=181, violations=0, scope="Signal-only screening"),
            )
            (root / "events/event-01.json").write_text(json.dumps(event))
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("server.py")),
                    str(root),
                    "--port",
                    "0",
                    "--repo",
                    str(repo),
                    "--cost-runtime",
                    os.environ.get("PNR_COST_TEST_RUNTIME", str(repo / "hardware/pnr")),
                    "--net-summaries",
                    "off",
                    "--agent",
                    "off",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                base = process.stdout.readline().strip().removeprefix("Live PnR: ")
                self.assertTrue(base.startswith("http://127.0.0.1:"), base)

                def request(path, body=None):
                    req = Request(
                        base + path,
                        data=None if body is None else json.dumps(body).encode(),
                        headers={} if body is None else {"Content-Type": "application/json"},
                    )
                    with urlopen(req, timeout=5) as response:
                        return json.load(response)

                deadline = time.monotonic() + 5
                while True:
                    state = request("/api/state")
                    if state["revision"] or time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
                self.assertEqual(state["errors"], [])
                lane = state["lanes"][event["candidate"]]
                self.assertEqual(lane["opens"], 181)
                self.assertEqual(lane["violations"], 0)
                self.assertEqual(lane["geometry"], geometry)
                self.assertEqual(lane["frames"][0]["label_source"], "unavailable")
                self.assertEqual(lane["frames"][0]["board_sha256"], sha)
                self.assertEqual(request("/api/geometry/" + sha), geometry)
                pin = request("/api/pin", {})
                snapshot = request(
                    "/api/snapshot",
                    dict(
                        pin_id=pin["pin_id"],
                        view=dict(lane=event["candidate"], phase="0"),
                        annotations=[dict(bounds=[1, 2, 3, 4])],
                        note="schema regression",
                    ),
                )
                saved = request(snapshot["url"])
                self.assertEqual(
                    (
                        saved["selected_geometry"]
                        if "selected_geometry" in saved
                        else saved["state"]["selected_geometry"]
                    ),
                    geometry,
                )
                self.assertEqual(saved["annotations"][0]["bounds"], [1, 2, 3, 4])
                event.update(id="event-02", time=2)
                event["data"].update(name="native-final", opens=0)
                temp = root / "event-02.tmp"
                temp.write_text(json.dumps(event))
                temp.replace(root / "events/event-02.json")
                deadline = time.monotonic() + 5
                while True:
                    state = request("/api/state")
                    if state["revision"] >= 2 or time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
                self.assertEqual(state["errors"], [])
                lane = state["lanes"][event["candidate"]]
                self.assertEqual(lane["phase"], "native-final")
                self.assertEqual(lane["opens"], 0)
                self.assertEqual(len(lane["frames"]), 2)
                self.assertEqual(prefs.read_bytes(), before)
                # --agent off: no assistant, but the notes store (default <root>/../notes) still serves the Notes tab
                status = request("/api/agent/status")
                self.assertEqual(
                    (status["available"], status["web"], status["notes"]), (False, False, True)
                )
                self.assertEqual(request("/api/notes"), dict(rev=0, notes=[]))
                made = json.load(
                    urlopen(
                        Request(
                            base + "/api/notes",
                            data=json.dumps(dict(title="Check C17 ESR")).encode(),
                            headers={"Content-Type": "application/json", "Origin": base},
                        ),
                        timeout=5,
                    )
                )
                self.assertEqual(
                    (made["ok"], made["note"]["id"], made["note"]["author"]),
                    (True, "N-0001", "user"),
                )
                self.assertTrue((Path(tmp) / "notes/design-notes.md").is_file())
                with self.assertRaises(HTTPError) as cm:
                    urlopen(base + "/api/agent/conversations", timeout=5)
                self.assertEqual(cm.exception.code, 503)
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()


HIER = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("PNR_SCHEMATIC_TEST_RUNTIME", HIER / "src11.frozen/hardware/pnr"))
SOURCES = RUNTIME.parent / "splanc_dev/elec/src"
GRAPH = HIER / "inputs10b/graph.json"
# Stand-in for the Claude CLI: stream-json with one text delta that reports whether the
# selection dossier reached the prompt; sleeps so the test can prove the server stays responsive.
# Notes: with a notes MCP server in --mcp-config the init event reports it connected (the server kills the turn otherwise); a
# prompt asking to "record a note" runs add_note through the real notes_mcp tool code with the per-turn env, as the CLI would.
FAKE_CLAUDE = (
    """#!%s
import json,sys,time
from pathlib import Path
a=sys.argv[1:];sid=a[a.index('--session-id')+1] if '--session-id' in a else a[a.index('--resume')+1];prompt=a[a.index('-p')+1]
with open(Path(__file__).with_name('argv.jsonl'),'a') as f:f.write(json.dumps(a)+'\\n')
srv=json.loads(Path(a[a.index('--mcp-config')+1]).read_text()).get('mcpServers',{}).get('splanc_notes')
out=lambda d:print(json.dumps(dict(d,session_id=sid)),flush=True)
ev=lambda e:out(dict(type='stream_event',parent_tool_use_id=None,event=e))
tools=a[a.index('--tools')+1].split(',')+(['mcp__splanc_notes__'+t for t in ('add_note','list_notes','get_note','comment_note','update_note')] if srv else [])
out(dict(type='system',subtype='init',tools=tools,mcp_servers=[dict(name='splanc_notes',status='connected')] if srv else [],model='fake',cwd='.'))
if srv and 'record a note' in prompt:
 sys.path.insert(0,str(Path(srv['args'][0]).parent));import notes_mcp
 args=dict(title='C17 is the 5 V output bulk capacitor',kind='observation',targets=[dict(kind='component',ref='C17')],sources=[dict(url='https://www.ti.com/product/TPS552882',title='TPS552882 product page')])
 text,err=notes_mcp.Server(env=srv['env']).call('add_note',args)
 out(dict(type='assistant',parent_tool_use_id=None,message=dict(id='m0',role='assistant',content=[dict(type='tool_use',id='t1',name='mcp__splanc_notes__add_note',input=args)])))
 out(dict(type='user',parent_tool_use_id=None,message=dict(role='user',content=[dict(type='tool_result',tool_use_id='t1',content=[dict(type='text',text=text)],is_error=err)])))
ev(dict(type='message_start',message=dict(id='m1')));ev(dict(type='content_block_start',index=0,content_block=dict(type='text',text='')))
time.sleep(1.2)
ev(dict(type='content_block_delta',index=0,delta=dict(type='text_delta',text='dossier ok [[ref:C17]]' if 'output_cap1 = new C22u' in prompt and 'lane state:' in prompt else 'dossier missing')))
out(dict(type='result',subtype='success',is_error=False,result='done',total_cost_usd=0,duration_ms=5,num_turns=1))
"""
    % sys.executable
)


@unittest.skipUnless(SOURCES.is_dir() and GRAPH.is_file(), "splanc design sources not present")
class SourceAgentHttpTest(unittest.TestCase):
    """/api/source/* and /api/agent/* with a fake CLI: no model calls, no cost."""

    def test_source_and_agent_routes(self):
        repo = Path(os.environ.get("PNR_VIEWER_TEST_REPO", Path(__file__).resolve().parents[3]))
        with tempfile.TemporaryDirectory(prefix="viewer-agent-") as tmp:
            root = Path(tmp) / "live"
            (root / "events").mkdir(parents=True)
            fake = Path(tmp) / "claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(0o755)
            event = dict(
                schema="pnr-live-event-v1",
                id="1-aa",
                time=1,
                kind="signal_start",
                candidate="t1/lane",
                iteration=None,
                layout=dict(json.loads(GRAPH.read_text()), components=[]),
                data={},
            )
            (root / "events/1-aa.json").write_text(json.dumps(event))
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("server.py")),
                    str(root),
                    "--port",
                    "0",
                    "--repo",
                    str(repo),
                    "--cost-runtime",
                    str(RUNTIME),
                    "--schematic-graph",
                    str(GRAPH),
                    "--net-summaries",
                    "off",
                    "--agent-claude",
                    str(fake),
                    "--agent-model",
                    "sonnet",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                base = p.stdout.readline().strip().removeprefix("Live PnR: ")
                origin = base.replace("127.0.0.1", "127.0.0.1")

                def call(path, body=None, headers=None, raw=False):
                    req = Request(
                        base + path,
                        data=(
                            None
                            if body is None
                            else (body if isinstance(body, bytes) else json.dumps(body).encode())
                        ),
                        headers=dict(
                            {"Content-Type": "application/json"} if body is not None else {},
                            **(headers or {})
                        ),
                    )
                    try:
                        with urlopen(req, timeout=20) as r:
                            data = r.read()
                            return r.status, dict(r.headers), data if raw else json.loads(data)
                    except HTTPError as e:
                        return e.code, dict(e.headers), json.loads(e.read() or b"{}")

                status, headers, index = call("/api/source/index")
                self.assertEqual(status, 200)
                self.assertEqual(
                    (index["version"], index["validation"]["nets_matched"], len(index["nets"])),
                    (1, 93, 93),
                )
                self.assertEqual(index["components"]["C17"]["text"], "output_cap1 = new C22u")
                status, headers, gz = call(
                    "/api/source/index", headers={"Accept-Encoding": "gzip"}, raw=True
                )
                self.assertEqual((status, headers.get("Content-Encoding")), (200, "gzip"))
                self.assertEqual(json.loads(gzip.decompress(gz))["sha"], index["sha"])
                self.assertEqual(
                    call("/api/source/index", headers={"If-None-Match": headers["ETag"]}, raw=True)[
                        0
                    ],
                    304,
                )
                status, _, f = call("/api/source/file?path=system_5v.ato")
                self.assertEqual(status, 200)
                self.assertIn("output_cap1 = new C22u", f["text"])
                for bad in (
                    "../x.ato",
                    "/etc/passwd",
                    "parts/../../../x.ato",
                    "%2e%2e/server.ato",
                    "..%2Fx.ato",
                    "system_5v.ato%00.ato",
                    "ato.yaml",
                ):
                    self.assertEqual(call("/api/source/file?path=" + bad)[0], 400, bad)
                self.assertEqual(call("/api/source/file?path=nope.ato")[0], 404)
                status, _, st = call("/api/agent/status")
                self.assertTrue(st["available"])
                self.assertEqual((st["default"], st["net_summaries"]["state"]), ("sonnet", "off"))
                body = dict(
                    message="What is C17 for?",
                    model="sonnet",
                    context=dict(
                        lane="t1/lane",
                        phase="live",
                        view="pcb",
                        selection=[dict(kind="component", ref="C17"), dict(kind="net", name="hv")],
                    ),
                )
                self.assertEqual(
                    call("/api/agent/chat", body)[0], 403
                )  # no Origin: rejected for the assistant
                self.assertEqual(
                    call("/api/agent/chat", body, {"Origin": "http://evil.example"})[0], 403
                )
                self.assertEqual(
                    call("/api/agent/chat", dict(body, message=""), {"Origin": origin})[0], 400
                )
                self.assertEqual(
                    call("/api/agent/chat", dict(body, model="gpt"), {"Origin": origin})[0], 400
                )
                result = {}

                def stream():
                    result["answer"] = call("/api/agent/chat", body, {"Origin": origin}, raw=True)

                worker = threading.Thread(target=stream)
                worker.start()
                time.sleep(0.4)
                t0 = time.monotonic()
                self.assertEqual(call("/api/agent/status")[2]["active"], 1)
                call("/api/state")
                self.assertLess(
                    time.monotonic() - t0, 1.0
                )  # threaded: a streaming turn does not block other requests
                worker.join(20)
                status, headers, raw = result["answer"]
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
                events = [
                    (
                        b.split("\n")[0].removeprefix("event: "),
                        json.loads(b.split("\n")[1].removeprefix("data: ")),
                    )
                    for b in raw.decode().strip().split("\n\n")
                ]
                self.assertEqual([e for e, _ in events], ["session", "delta", "done"])
                self.assertEqual(events[1][1]["text"], "dossier ok [[ref:C17]]")
                self.assertEqual(events[2][1]["session"], events[0][1]["id"])
                self.assertEqual(
                    call("/api/agent/cancel", dict(session=events[0][1]["id"]), {"Origin": origin})[
                        2
                    ],
                    dict(ok=False, error="not running"),
                )
                self.assertEqual(
                    call("/api/agent/cancel", dict(session="x"), {"Origin": "http://evil.example"})[
                        0
                    ],
                    403,
                )
                self.assertEqual(
                    call("/api/agent/cancel", [1, 2], {"Origin": origin})[:3:2],
                    (400, {"error": "invalid body"}),
                )
                rebind = {"Host": "rebind.attacker.example:" + base.rsplit(":", 1)[1]}
                for path in (
                    "/api/source/file?path=system_5v.ato",
                    "/api/source/index",
                    "/api/state",
                    "/",
                ):
                    self.assertEqual(
                        call(path, headers=rebind)[0], 421, path
                    )  # DNS-rebinding guard
                self.assertEqual(call("/api/agent/chat", body, dict(rebind, Origin=origin))[0], 421)
                self.assertEqual(
                    call("/api/source/file?path=system_5v.ato", headers={"Host": "localhost:1"})[0],
                    200,
                )
                st = call("/api/agent/status")[2]
                self.assertEqual((st["spent_usd"], st["max_total_usd"]), (0.0, 20.0))
                state = call("/api/state")[2]
                self.assertNotIn("source", state["lanes"]["t1/lane"])
                self.assertNotIn("lane_meta", state)
            finally:
                p.terminate()
                p.wait(10)
                p.stdout.close()
                p.stderr.close()

    def test_notes_and_conversation_routes(self):
        """/api/notes* (user actor only, Origin required, export md/json), agent notes through the per-turn MCP server,
        /api/agent/conversations* and the web flag passthrough; fake CLI, no model calls."""
        import uuid

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notes_mcp
        from notes_store import NotesStore

        repo = Path(os.environ.get("PNR_VIEWER_TEST_REPO", Path(__file__).resolve().parents[3]))
        with tempfile.TemporaryDirectory(prefix="viewer-notes-") as tmp:
            root = Path(tmp) / "live"
            (root / "events").mkdir(parents=True)
            fake = Path(tmp) / "claude"
            fake.write_text(FAKE_CLAUDE)
            fake.chmod(0o755)
            event = dict(
                schema="pnr-live-event-v1",
                id="1-aa",
                time=1,
                kind="signal_start",
                candidate="t1/lane",
                iteration=None,
                layout=dict(json.loads(GRAPH.read_text()), components=[]),
                data={},
            )
            (root / "events/1-aa.json").write_text(json.dumps(event))
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("server.py")),
                    str(root),
                    "--port",
                    "0",
                    "--repo",
                    str(repo),
                    "--cost-runtime",
                    str(RUNTIME),
                    "--schematic-graph",
                    str(GRAPH),
                    "--net-summaries",
                    "off",
                    "--agent-claude",
                    str(fake),
                    "--agent-model",
                    "sonnet",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                base = p.stdout.readline().strip().removeprefix("Live PnR: ")
                port = int(base.rsplit(":", 1)[1])
                origin = {"Origin": base}
                notes_dir = Path(tmp) / "notes"  # default: <root>/../notes

                def call(path, body=None, headers=None, raw=False):
                    req = Request(
                        base + path,
                        data=None if body is None else json.dumps(body).encode(),
                        headers=dict(
                            {"Content-Type": "application/json"} if body is not None else {},
                            **(headers or {})
                        ),
                    )
                    try:
                        with urlopen(req, timeout=20) as r:
                            data = r.read()
                            return r.status, dict(r.headers), data if raw else json.loads(data)
                    except HTTPError as e:
                        return e.code, dict(e.headers), json.loads(e.read() or b"{}")

                def chat(body):
                    status, _, raw = call("/api/agent/chat", body, origin, raw=True)
                    self.assertEqual(status, 200)
                    return [
                        (
                            b.split("\n")[0].removeprefix("event: "),
                            json.loads(b.split("\n")[1].removeprefix("data: ")),
                        )
                        for b in raw.decode().strip().split("\n\n")
                    ]

                argv = lambda: [
                    json.loads(l) for l in (Path(tmp) / "argv.jsonl").read_text().splitlines()
                ]
                st = call("/api/agent/status")[2]
                self.assertEqual((st["available"], st["web"], st["notes"]), (True, True, True))
                self.assertEqual(call("/api/notes")[2], dict(rev=0, notes=[]))
                self.assertEqual(call("/api/notes?since=0")[2], dict(rev=0, unchanged=True))
                # writes: allowlisted Origin required (a missing one too), Host guard, user actor only
                user = dict(
                    title="Is C17 enough for the ripple current?",
                    kind="question",
                    targets=[dict(kind="component", ref="C17")],
                    provenance=dict(lane="t1/lane", viewer_port=port),
                )
                self.assertEqual(call("/api/notes", user)[0], 403)
                self.assertEqual(
                    call("/api/notes", user, {"Origin": "http://evil.example"})[0], 403
                )
                self.assertEqual(
                    call(
                        "/api/notes",
                        user,
                        {"Origin": base, "Host": "rebind.attacker.example:%d" % port},
                    )[0],
                    421,
                )
                self.assertEqual(
                    call("/api/notes/N-0001", dict(fields=dict(status="accepted")))[0], 403
                )
                self.assertEqual(call("/api/notes/N-0001/delete", {})[0], 403)
                self.assertEqual(
                    call("/api/notes", dict(user, author="agent"), origin)[0], 400
                )  # nobody can claim to be the agent
                self.assertEqual(
                    call("/api/notes", dict(user, actor=dict(kind="agent")), origin)[0], 400
                )
                self.assertEqual(call("/api/notes", dict(user, title=""), origin)[0], 400)
                status, _, made = call("/api/notes", user, origin)
                self.assertEqual(status, 200)
                self.assertEqual(
                    (
                        made["ok"],
                        made["rev"],
                        made["note"]["id"],
                        made["note"]["author"],
                        made["note"]["status"],
                        made["note"]["provenance"],
                    ),
                    (True, 1, "N-0001", "user", "open", dict(lane="t1/lane", viewer_port=port)),
                )
                self.assertEqual(
                    json.loads((notes_dir / "notes.jsonl").read_text().splitlines()[0])["actor"],
                    dict(kind="user", remote="127.0.0.1"),
                )
                # an agent turn that records a note: tool + note events, done.notes_created, provenance from the per-turn env
                body = dict(
                    message="What is C17 for? Please record a note.",
                    model="sonnet",
                    context=dict(
                        lane="t1/lane",
                        phase="live",
                        view="pcb",
                        selection=[dict(kind="component", ref="C17")],
                    ),
                )
                events = chat(body)
                names = [e for e, _ in events]
                self.assertEqual(names, ["session", "tool", "note", "delta", "done"], events)
                sid = events[0][1]["id"]
                self.assertEqual(
                    (events[0][1]["turn"], events[0][1]["web"], events[0][1]["notes"]),
                    (1, True, True),
                )
                self.assertEqual(
                    events[1][1],
                    dict(
                        name="mcp__splanc_notes__add_note",
                        detail="observation: C17 is the 5 V output bulk capacitor",
                    ),
                )
                self.assertEqual(
                    (events[2][1]["id"], events[2][1]["op"], events[2][1]["note"]["author"]),
                    ("N-0002", "create", "agent"),
                )
                self.assertEqual(
                    (events[-1][1]["notes_created"], events[-1][1]["turn"], events[-1][1]["web"]),
                    (["N-0002"], 1, True),
                )
                a = argv()[-1]
                self.assertEqual(a[a.index("--tools") + 1], "Read,Grep,Glob,WebSearch,WebFetch")
                self.assertIn("mcp__splanc_notes__add_note", a[a.index("--allowedTools") + 1])
                self.assertNotIn(
                    "WebFetch", a[a.index("--allowedTools") + 1]
                )  # approved only by the web_guard.py hook
                self.assertIn("WebFetch(domain:*.ts.net)", a[a.index("--disallowedTools") + 1])
                self.assertIn(
                    "design notes already recorded on the selection",
                    a[a.index("--append-system-prompt") + 1],
                )
                self.assertIn(
                    "## Design notes on this selection (1", a[a.index("-p") + 1]
                )  # N-0001 targets C17: in the dossier
                listing = call("/api/notes?since=1")[2]
                self.assertEqual(
                    (listing["rev"], listing["changed"], [n["id"] for n in listing["notes"]]),
                    (2, ["N-0002"], ["N-0001", "N-0002"]),
                )
                agent_note = listing["notes"][1]
                self.assertEqual(
                    (
                        agent_note["author"],
                        agent_note["status"],
                        agent_note["targets"],
                        agent_note["sources"][0]["url"],
                    ),
                    (
                        "agent",
                        "open",
                        [dict(kind="component", ref="C17")],
                        "https://www.ti.com/product/TPS552882",
                    ),
                )
                self.assertEqual(
                    {
                        k: agent_note["provenance"][k]
                        for k in ("session", "turn", "lane", "phase", "viewer_port")
                    },
                    dict(session=sid, turn=1, lane="t1/lane", phase="live", viewer_port=port),
                )
                # authority: the agent (its MCP tools or the store with an agent actor) cannot accept or delete; the user can
                env = dict(SPLANC_NOTES_DIR=str(notes_dir), SPLANC_SESSION=sid)
                text, err = notes_mcp.Server(env=env).call(
                    "update_note", dict(id="N-0002", status="accepted")
                )
                self.assertTrue(err and "not allowed" in text, text)
                with self.assertRaises(PermissionError):
                    NotesStore(notes_dir).update(
                        "N-0002", dict(status="accepted"), dict(kind="agent", session=sid)
                    )
                with self.assertRaises(PermissionError):
                    NotesStore(notes_dir).delete("N-0002", dict(kind="agent", session=sid))
                self.assertEqual(call("/api/notes/N-0002")[0], 404)  # no GET per note
                self.assertEqual(
                    call(
                        "/api/notes/N-0002",
                        dict(fields=dict(status="accepted"), expect_rev=1),
                        origin,
                    )[0],
                    409,
                )
                self.assertEqual(
                    call("/api/notes/N-0002", dict(fields=dict(status="done")), origin)[0], 400
                )
                self.assertEqual(
                    call("/api/notes/N-0099", dict(fields=dict(status="accepted")), origin)[0], 404
                )
                self.assertEqual(call("/api/notes/N-0002/bogus", {}, origin)[0], 404)
                status, _, err = call(
                    "/api/notes/N-0002",
                    dict(fields=dict(status="accepted"), comment="Agreed."),
                    origin,
                )
                self.assertEqual(
                    (status, "expect_rev" in err["error"]), (400, True)
                )  # accepting needs the rev the user reviewed
                self.assertEqual(
                    call("/api/notes")[2]["notes"][1]["comments"], []
                )  # and nothing of the request was applied
                status, _, acc = call(
                    "/api/notes/N-0002",
                    dict(
                        fields=dict(status="accepted"),
                        comment="Agreed.",
                        expect_rev=agent_note["rev"],
                    ),
                    origin,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    (
                        acc["note"]["status"],
                        acc["note"]["status_by"],
                        acc["note"]["comments"][0]["text"],
                        acc["note"]["comments"][0]["author"],
                    ),
                    ("accepted", "user", "Agreed.", "user"),
                )
                text, err = notes_mcp.Server(env=env).call(
                    "update_note", dict(id="N-0002", title="changed")
                )
                self.assertTrue(
                    err and "accepted" in text, text
                )  # decided: the agent may only comment now
                text, err = notes_mcp.Server(env=env).call(
                    "comment_note", dict(id="N-0002", text="Datasheet section 8.2 agrees.")
                )
                self.assertFalse(err, text)
                # export (the design-loop feed) and the files on disk
                status, headers, md = call("/api/notes/export?format=md", raw=True)
                self.assertEqual(
                    (status, headers["Content-Type"]), (200, "text/markdown; charset=utf-8")
                )
                md = md.decode()
                for needle in (
                    "## Accepted (to apply) (1)",
                    "### N-0002 · observation · C17 is the 5 V output bulk capacitor",
                    "(set by user",
                    "[TPS552882 product page](https://www.ti.com/product/TPS552882)",
                    "C17: C22u board.converter.output_cap1 (system_5v.ato:",
                    "## Open questions / observations (1)",
                    "agent: Datasheet section 8.2 agrees.",
                ):
                    self.assertIn(needle, md)
                disk = (
                    notes_dir / "design-notes.md"
                ).read_text()  # regenerated by whichever process wrote last (here the MCP server)
                self.assertIn("## Accepted (to apply) (1)", disk)
                self.assertIn("agent: Datasheet section 8.2 agrees.", disk)
                status, headers, js = call("/api/notes/export?format=json")
                self.assertEqual(
                    (status, headers["Content-Type"]), (200, "application/json; charset=utf-8")
                )
                self.assertEqual(
                    ([n["id"] for n in js["notes"]], js["notes"][1]["targets_resolved"][0][:4]),
                    (["N-0001", "N-0002"], "C17:"),
                )
                self.assertEqual(call("/api/notes/export?format=xml")[0], 400)
                # delete (user) and the since delta
                rev = call("/api/notes")[2]["rev"]
                status, _, gone = call("/api/notes/N-0001/delete", {}, origin)
                self.assertEqual(
                    (status, gone["ok"], gone["id"], gone["deleted"]), (200, True, "N-0001", True)
                )
                delta = call("/api/notes?since=%d" % rev)[2]
                self.assertEqual(
                    (delta["deleted"], [n["id"] for n in delta["notes"]]), (["N-0001"], ["N-0002"])
                )
                # conversations: listing, one conversation, resume after the stored turn, web off for this turn
                convs = call("/api/agent/conversations")[2]["conversations"]
                self.assertEqual(
                    [
                        (
                            c["session"],
                            c["turns"],
                            c["title"],
                            c["lane"],
                            c["notes_created"],
                            c["resumable"],
                        )
                        for c in convs
                    ],
                    [(sid, 1, "What is C17 for? Please record a note.", "t1/lane", 1, True)],
                )
                conv = call("/api/agent/conversations/" + sid)[2]
                t1 = conv["turns"][0]
                self.assertEqual(
                    (
                        t1["turn"],
                        t1["message"],
                        t1["answer"],
                        t1["notes_created"],
                        t1["web"],
                        t1["selection"],
                        t1["model"],
                    ),
                    (
                        1,
                        body["message"],
                        "dossier ok [[ref:C17]]",
                        ["N-0002"],
                        True,
                        [dict(kind="component", ref="C17")],
                        "sonnet",
                    ),
                )
                self.assertEqual(
                    t1["tools"],
                    [
                        dict(
                            name="mcp__splanc_notes__add_note",
                            detail="observation: C17 is the 5 V output bulk capacitor",
                        )
                    ],
                )
                self.assertNotIn("cli_session", t1)
                self.assertEqual(call("/api/agent/conversations/not-a-uuid")[0], 400)
                self.assertEqual(call("/api/agent/conversations/" + str(uuid.uuid4()))[0], 404)
                self.assertEqual(call("/api/agent/conversations/" + sid + "/x")[0], 400)
                events = chat(dict(body, message="And C18?", session=sid, web=False))
                self.assertEqual([e for e, _ in events], ["session", "delta", "done"], events)
                self.assertEqual(
                    (events[0][1]["id"], events[0][1]["turn"], events[0][1]["web"]), (sid, 2, False)
                )
                a = argv()[-1]
                self.assertEqual(
                    (a[a.index("--tools") + 1], a[a.index("--resume") + 1]), ("Read,Grep,Glob", sid)
                )
                self.assertNotIn("--disallowedTools", a)
                self.assertNotIn("--settings", a)
                self.assertEqual(
                    [t["turn"] for t in call("/api/agent/conversations/" + sid)[2]["turns"]], [1, 2]
                )
                self.assertEqual(call("/api/agent/chat", dict(body, web="yes"), origin)[0], 400)
            finally:
                p.terminate()
                p.wait(10)
                p.stdout.close()
                p.stderr.close()


if __name__ == "__main__":
    unittest.main()
