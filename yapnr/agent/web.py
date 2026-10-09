"""Integrated OpenCode workspace views; provider credentials stay in OpenCode's home."""

import base64
import html
import http.client
import json
import mimetypes
import re
import select
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from yapnr.agent import threads, workspace
from yapnr.agent.workflow import init, query

WEB = Path(__file__).with_name("web")


def api(upstream, path, data=None):
    request = urllib.request.Request(
        upstream.rstrip("/") + path,
        data=workspace.encoded(data) if data is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


class Recorder:
    def __init__(self, root, directory, upstream):
        self.root, self.directory, self.upstream = root, directory, upstream
        self.stop = threading.Event()
        self.status = "starting"
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.snapshot_thread = threading.Thread(target=self.snapshots, daemon=True)
        self.snapshot_thread.start()

    def snapshot(self, session, messages):
        if session.get("directory") != self.directory or not threads.SESSION.fullmatch(
            session["id"]
        ):
            return
        content = workspace.encoded({"info": session, "messages": messages})
        with workspace.lock(self.root) as folder:
            path = folder / "opencode" / (session["id"] + ".json")
            if not path.is_file() or path.read_bytes() != content:
                workspace.atomic(path, content)

    def snapshots(self):
        suffix = "?directory=" + quote(self.directory, safe="")
        while not self.stop.is_set():
            try:
                sessions = api(self.upstream, "/session" + suffix)
                for session in (
                    sessions if isinstance(sessions, list) else sessions.get("items", [])
                ):
                    if session.get("directory") == self.directory:
                        messages = api(
                            self.upstream, "/session/" + session["id"] + "/message" + suffix
                        )
                        self.snapshot(session, messages)
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
                pass
            self.stop.wait(5)

    def run(self):
        backfilled = False
        suffix = "?directory=" + quote(self.directory, safe="")
        while not self.stop.is_set():
            try:
                # Subscribe first so events emitted during backfill are buffered, not lost.
                with urllib.request.urlopen(
                    self.upstream + "/event" + suffix, timeout=30
                ) as stream:
                    self.status = "connected"
                    if not backfilled:
                        sessions = api(self.upstream, "/session" + suffix)
                        for session in (
                            sessions if isinstance(sessions, list) else sessions.get("items", [])
                        ):
                            if session.get("directory") != self.directory:
                                continue
                            messages = api(
                                self.upstream, "/session/" + session["id"] + "/message" + suffix
                            )
                            self.snapshot(session, messages)
                            workspace.record(
                                self.root,
                                {
                                    "source": "opencode-backfill",
                                    "session": session,
                                    "messages": messages,
                                },
                            )
                        backfilled = True
                    for line in stream:
                        if self.stop.is_set():
                            break
                        if line.startswith(b"data:"):
                            event = json.loads(line[5:])
                            if event.get("type") in ("server.heartbeat", "server.connected"):
                                continue
                            workspace.record(
                                self.root,
                                {"source": "opencode-event", "event": event},
                            )
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
                self.status = "reconnecting"
                self.stop.wait(2)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        upstream,
        projects,
        public_origin="",
        experiments=None,
        agent_projects="/projects",
    ):
        super().__init__(address, Handler)
        self.upstream = upstream.rstrip("/")
        self.projects = Path(projects).resolve()
        self.public_origin = public_origin.rstrip("/")
        self.recorders = {}
        self.recorder_lock = threading.Lock()
        self.experiments = experiments or {}
        self.agent_projects = str(Path(agent_projects)).rstrip("/")

    def project(self, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            raise ValueError("Invalid project")
        root = self.projects / name
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Unknown project")
        return root

    def directory(self, name):
        self.project(name)
        return self.agent_projects + "/" + name

    def start_recorder(self, name):
        with self.recorder_lock:
            if name not in self.recorders:
                self.recorders[name] = Recorder(
                    self.project(name), self.directory(name), self.upstream
                )

    def server_close(self):
        for recorder in self.recorders.values():
            recorder.stop.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Navigation closes an SSE connection normally.

    def log_message(self, *_):
        # Conversation logging is explicit, not raw HTTP/OAuth URL logging.
        pass

    def send_bytes(self, data, kind, status=200, cors=False, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in headers.items() if isinstance(headers, dict) else headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.send_bytes(workspace.encoded(data), "application/json", status)

    def same_origin(self):
        origin = self.headers.get("Origin", "")
        if self.server.public_origin:
            return origin == self.server.public_origin
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")

    def payload(self):
        if not self.same_origin():
            raise ValueError("Same-origin workspace editing required")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 12 * 1024**2:
            raise ValueError("Request exceeds its size budget")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value

    def registry(self, root):
        return workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})

    def artifact(self, root, identifier):
        item = next((a for a in self.registry(root)["artifacts"] if a["id"] == identifier), None)
        if item is None:
            raise ValueError("Unknown artifact")
        path = workspace.relative_file(root, item["path"])
        if workspace.file_sha(path) != item["sha256"]:
            raise ValueError("Artifact integrity failure")
        return item, path

    def project_state(self, name):
        root = self.server.project(name)
        self.server.start_recorder(name)
        artifacts = self.registry(root)["artifacts"]
        experiment = self.server.experiments.get(name) or next(
            (
                a["metadata"]["viewer_url"]
                for a in reversed(artifacts)
                if a["kind"] in ("board", "schematic") and a.get("metadata", {}).get("viewer_url")
            ),
            None,
        )
        if experiment and urlsplit(experiment).scheme not in ("http", "https"):
            experiment = None
        try:
            workflow = query(root)
        except (OSError, ValueError):
            workflow = {"state": "not_initialized"}
        return {
            "project": name,
            "agent_directory": self.server.directory(name),
            "artifacts": artifacts,
            "scratchpad": workspace.scratchpad(root),
            "experiment_url": experiment,
            "workflow": workflow,
            "recording": self.server.recorders[name].status,
        }

    def thread_state(self, name):
        root = self.server.project(name)
        self.server.start_recorder(name)
        suffix = "?directory=" + quote(self.server.directory(name), safe="")
        sessions = api(self.server.upstream, "/session" + suffix)
        sessions = sessions if isinstance(sessions, list) else sessions.get("items", [])
        chats = [
            {
                "id": s["id"],
                "kind": "opencode",
                "title": s.get("title", "Chat"),
                "parent": s.get("parentID"),
                "updated": s.get("time", {}).get("updated"),
            }
            for s in sessions
            if s.get("directory") == self.server.directory(name)
        ]
        main = workspace.read_json(root / ".yapnr/workspace/thread-state.json", {})
        return {
            "threads": threads.focused(root) + chats,
            "notes": threads.notes(root).all(),
            "main_thread": main.get("main_thread"),
        }

    def do_GET(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/" or re.fullmatch(
                r"/(?:workspace(?:/[a-z0-9-]+)?(?:/session/ses_[A-Za-z0-9]+)?"
                r"|server/[^/]+/session(?:/ses_[A-Za-z0-9]+)?|[^/]+/session(?:/ses_[A-Za-z0-9]+)?)",
                parsed.path,
            ):
                return self.send_bytes(
                    (WEB / "index.html").read_bytes(), "text/html; charset=utf-8"
                )
            if parsed.path == "/yapnr/workspace.js":
                return self.send_bytes((WEB / "workspace.js").read_bytes(), "text/javascript")
            vendor = re.fullmatch(
                r"/yapnr/vendor/(three\.core\.js|three\.module\.js|OrbitControls\.js|LICENSE)",
                parsed.path,
            )
            if vendor:
                return self.send_bytes(
                    (WEB / "vendor" / vendor[1]).read_bytes(),
                    "text/plain" if vendor[1] == "LICENSE" else "text/javascript",
                    cors=True,
                )
            if parsed.path == "/yapnr/api/projects":
                names = [
                    p.name
                    for p in sorted(self.server.projects.iterdir())
                    if p.is_dir() and not p.is_symlink() and (p / "AGENTS.md").is_file()
                ]
                return self.json({"projects": names})
            session_project = re.fullmatch(
                r"/yapnr/api/session-project/(ses_[A-Za-z0-9]+)", parsed.path
            )
            if session_project:
                info = api(self.server.upstream, "/session/" + session_project[1])
                directory = info.get("directory", "")
                match = re.fullmatch(
                    re.escape(self.server.agent_projects) + r"/([a-z0-9-]+)", directory
                )
                if not match:
                    raise ValueError("Session is outside the project workspace")
                self.server.project(match[1])
                return self.json({"project": match[1], "session": info["id"]})
            state = re.fullmatch(r"/yapnr/api/project/([a-z0-9-]+)", parsed.path)
            if state:
                return self.json(self.project_state(state[1]))
            listing = re.fullmatch(r"/yapnr/api/threads/([a-z0-9-]+)", parsed.path)
            if listing:
                return self.json(self.thread_state(listing[1]))
            asset = re.fullmatch(
                r"/yapnr/(artifact|program|scene)/([a-z0-9-]+)/([0-9a-f]{64})", parsed.path
            )
            if asset:
                kind, name, identifier = asset.groups()
                root = self.server.project(name)
                item, path = self.artifact(root, identifier)
                if kind == "scene":
                    if item["kind"] != "scene":
                        raise ValueError("This artifact is not an interactive scene")
                    return self.scene(name, item)
                if kind == "program":
                    if item["kind"] != "scene":
                        raise ValueError("This artifact is not a scene program")
                    return self.send_bytes(path.read_bytes(), "text/javascript", cors=True)
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                return self.send_bytes(path.read_bytes(), mime)
            if parsed.path.startswith("/yapnr/"):
                return self.json({"error": "Unknown workspace resource"}, 404)
            return self.proxy()
        except (OSError, ValueError, KeyError, TypeError):
            self.json({"error": "Workspace resource unavailable or invalid"}, 400)

    def scene(self, name, item):
        identifier = item["id"]
        title = html.escape(item["title"])
        # Opaque iframe origin; no network, storage, forms or top-level navigation.
        host = self.server.public_origin or "http://" + self.headers["Host"]
        parsed = urlsplit(host)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.\[\]:_-]+", parsed.netloc)
        ):
            raise ValueError("Invalid public workspace origin")
        body = (WEB / "scene.html").read_text()
        values = {
            "ORIGIN": host,
            "TITLE": title,
            "ARTIFACT": identifier,
            "PROJECT": name,
            "SEED": str(int(item["metadata"].get("seed", 0)) & 0xFFFFFFFF),
        }
        for key, value in values.items():
            body = body.replace("@@" + key + "@@", value)
        body = body.encode()
        csp = (
            f"default-src 'none'; script-src 'unsafe-inline' {host}/yapnr/vendor/ {host}/yapnr/program/; "
            "style-src 'unsafe-inline'; img-src data:; "
            f"connect-src {host}/yapnr/vendor/ {host}/yapnr/program/; "
            "worker-src 'none'; base-uri 'none'; form-action 'none'"
        )
        return self.send_bytes(
            body, "text/html; charset=utf-8", headers={"Content-Security-Policy": csp}
        )

    def do_POST(self):
        if urlsplit(self.path).path == "/yapnr/api/create-project":
            try:
                data = self.payload()
                name = data["name"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
                    raise ValueError("Invalid project name")
                root = self.server.projects / name
                root.mkdir()  # Never overwrite an existing article.
                init(root, str(data.get("directive", "")))
                return self.json({"project": name})
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                return self.json(
                    {"error": "Project creation failed; use a new lowercase project name"}, 400
                )
        match = re.fullmatch(
            r"/yapnr/api/(scratchpad|annotation|review|thread|note|attach-note|main-thread)/([a-z0-9-]+)",
            urlsplit(self.path).path,
        )
        if match is None:
            return self.proxy()
        try:
            data = self.payload()
            action, name = match.groups()
            root = self.server.project(name)

            def remote(path, value=None):
                return api(self.server.upstream, path, value)

            if action == "main-thread":
                if data["session"]:
                    threads.session_info(remote, name, data["session"], self.server.directory(name))
                result = {"main_thread": data["session"]}
                with workspace.lock(root) as folder:
                    workspace.atomic(folder / "thread-state.json", workspace.encoded(result))
                workspace.record(root, {"source": "user-main-thread-selection", **result})
                return self.json(result)

            if action == "thread":
                return self.json(
                    threads.transcript(
                        root, remote, name, data["kind"], data["id"], self.server.directory(name)
                    )
                )
            if action == "note":
                return self.json(
                    threads.save_note(root, remote, name, data, self.server.directory(name))
                )
            if action == "attach-note":
                return self.json(
                    threads.attach_note(root, remote, name, data, self.server.directory(name))
                )
            if action == "scratchpad":
                try:
                    result = workspace.scratchpad(
                        root, data["text"], data["revision"], source="user"
                    )
                except ValueError:
                    return self.json(
                        {
                            "error": (
                                "Document changed or edit is invalid; keep your edits "
                                "and reconcile the latest revision"
                            )
                        },
                        409,
                    )
                return self.json({"revision": result["revision"]})
            if action == "annotation":
                original, _ = self.artifact(root, data["artifact"])
                image = base64.b64decode(data["image"].split(",", 1)[1], validate=True)
                if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 8 * 1024**2:
                    raise ValueError("Annotation needs a bounded PNG render")
                with workspace.lock(root) as folder:
                    _, image_path = workspace.blob(folder, image, ".png")
                    _, record_path = workspace.blob(folder, workspace.encoded(data), ".json")
                item = workspace.publish(
                    root,
                    image_path,
                    "image",
                    "Annotated: " + original["title"],
                    {
                        "original_sha256": original["sha256"],
                        "annotation": record_path,
                        "view": data.get("view"),
                        "note": data.get("note", ""),
                    },
                )
                workspace.record(root, {"source": "user-annotation", "artifact": item})
                return self.json(item)
            session = data["session"]
            if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
                raise ValueError("Select an existing project session first")
            directory = "?directory=" + quote(self.server.directory(name), safe="")
            info = api(self.server.upstream, "/session/" + session + directory)
            if info.get("directory") != self.server.directory(name):
                raise ValueError("Review request belongs to another project")
            messages = api(self.server.upstream, "/session/" + session + "/message" + directory)
            previous = next(
                (m["info"] for m in reversed(messages) if m["info"].get("role") == "user"), {}
            )
            prompt = {"parts": [{"type": "text", "text": str(data["text"])}]}
            if data.get("model"):
                prompt["model"] = data["model"]
            elif previous.get("model"):
                prompt["model"] = previous["model"]
            if previous.get("agent"):
                prompt["agent"] = previous["agent"]
            if data.get("artifact"):
                item, path = self.artifact(root, data["artifact"])
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                prompt["parts"].append(
                    {
                        "type": "file",
                        "mime": mime,
                        "filename": path.name,
                        "url": "data:"
                        + mime
                        + ";base64,"
                        + base64.b64encode(path.read_bytes()).decode(),
                    }
                )
            workspace.record(
                root, {"source": "user-review-request", "session": session, "request": prompt}
            )
            api(self.server.upstream, "/session/" + session + "/prompt_async" + directory, prompt)
            return self.json({"status": "sent"})
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            self.json({"error": "Workspace edit or review request rejected"}, 400)

    def proxy(self):
        upstream = urlsplit(self.server.upstream)
        connection_class = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(upstream.hostname, upstream.port, timeout=45)
        started = False
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 16 * 1024**2:
                return self.json({"error": "Request exceeds proxy budget"}, 413)
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in ("host", "connection", "accept-encoding")
            }
            headers["Host"] = upstream.netloc
            headers["Accept-Encoding"] = "identity"
            websocket = self.headers.get("Upgrade", "").lower() == "websocket"
            if websocket:
                headers["Connection"] = "Upgrade"
            connection.request(
                self.command, self.path, self.rfile.read(size) if size else None, headers
            )
            transport = connection.sock
            response = connection.getresponse()
            if response.status == 101 and websocket:
                self.send_response(101)
                for key, value in response.getheaders():
                    self.send_header(key, value)
                self.end_headers()
                started = True
                self.close_connection = True
                transport.settimeout(None)
                while True:
                    ready, _, _ = select.select([self.connection, transport], [], [], 30)
                    for source in ready:
                        block = source.recv(65536)
                        if not block:
                            return
                        (transport if source is self.connection else self.connection).sendall(block)
            kind = response.getheader("Content-Type", "application/octet-stream")
            if "text/event-stream" in kind:
                self.send_response(response.status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                started = True
                while True:
                    line = response.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                return
            data = response.read()
            forwarded = [
                (key, value)
                for key, value in response.getheaders()
                if key.lower()
                not in (
                    "content-length",
                    "content-type",
                    "transfer-encoding",
                    "connection",
                    "cache-control",
                )
            ]
            started = True
            self.send_bytes(data, kind, response.status, headers=forwarded)
        except (OSError, http.client.HTTPException):
            if not started and not self.wfile.closed:
                self.json({"error": "OpenCode connection unavailable"}, 502)
        finally:
            connection.close()

    do_DELETE = proxy
    do_PATCH = proxy
    do_PUT = proxy
    do_OPTIONS = proxy


def run(args):
    child = None
    Path(args.projects).mkdir(parents=True, exist_ok=True)
    upstream = args.upstream
    agent_projects = getattr(args, "agent_projects", "") or (
        "/projects" if upstream else str(Path(args.projects).resolve())
    )
    if not upstream:
        child = subprocess.Popen(
            ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(args.port + 1)],
            cwd=args.projects,
        )
        upstream = "http://localhost:" + str(args.port + 1)
    experiments = {}
    for value in args.experiment:
        name, url = value.split("=", 1)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) or urlsplit(url).scheme not in (
            "http",
            "https",
        ):
            raise ValueError("Experiment view must be PROJECT=http(s)://viewer")
        experiments[name] = url
    server = Server(
        (args.listen, args.port),
        upstream,
        args.projects,
        args.public_origin,
        experiments,
        agent_projects,
    )
    print(f"yapnr workspace UI listening on port {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    return 0


def register(commands):
    parser = commands.add_parser(
        "web", help="Design workspace with conversation, artifacts, annotations and scratchpad"
    )
    parser.add_argument("--projects", default="/projects")
    parser.add_argument(
        "--upstream", default="", help="reuse an existing OpenCode server instead of launching one"
    )
    parser.add_argument(
        "--agent-projects",
        default="",
        help="project mount path visible to the reused agent server (default: /projects)",
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4097)
    parser.add_argument("--public-origin", default="")
    parser.add_argument(
        "--experiment",
        action="append",
        default=[],
        metavar="PROJECT=URL",
        help="embed this project's running experiment viewer in the same workspace",
    )
    parser.set_defaults(func=run)
