"""The atopile hook: parsed-URL guard, empty queries, local parts, offline, and that it reaches
multiprocessing workers (atopile 0.15.8 builds in a forkserver worker).

Uses a fake ``atopile``/``faebryk``/``kicadcliwrapper`` written into a temporary directory; the
real atopile is exercised by tests/e2e/atopile.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

from yapnr.frontends.atopile.env import HOOK_DIR

_spec = importlib.util.spec_from_file_location(
    "yapnr_atopile_hook", HOOK_DIR / "yapnr_atopile_hook.py"
)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

PICKER = "http://127.0.0.1:43123"

FAKE_MODULES = {
    "atopile/__init__.py": "",
    "atopile/config.py": """
        import os
        from types import SimpleNamespace as N
        config = N(project=N(services=N(components=N(url=os.environ.get("FAKE_URL", ""))),
                             paths=N(parts=os.environ.get("FAKE_PARTS", "/nonexistent"))))
    """,
    "atopile/errors.py": """
        class UserException(Exception):
            def __init__(self, message, title=None):
                super().__init__(message)
                self.title = title
    """,
    "faebryk/__init__.py": "",
    "faebryk/libs/__init__.py": "",
    "faebryk/libs/picker/__init__.py": "",
    "faebryk/libs/picker/api/__init__.py": "",
    "faebryk/libs/picker/api/api.py": """
        from dataclasses import dataclass
        from atopile.config import config

        class ApiClient:
            @dataclass
            class ApiConfig:
                api_url: str = config.project.services.components.url
                api_key: str = None

            posted = None

            @property
            def _cfg(self):
                raise RuntimeError("sign-in required (unpatched)")

            def fetch_parts_multiple(self, params):
                ApiClient.posted = list(params)
                return [["posted"]]
    """,
    "kicadcliwrapper/__init__.py": "",
    "kicadcliwrapper/lib.py": """
        def find_kicad_cli():
            return "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    """,
}

CHILD = """
import json, multiprocessing as mp, sys

def probe(queue):
    from faebryk.libs.picker.api.api import ApiClient
    from kicadcliwrapper import lib
    client = ApiClient()
    out = {"empty": client.fetch_parts_multiple([]), "posted": ApiClient.posted}
    try:
        cfg = client._cfg
        out["cfg"] = [cfg.api_url, cfg.api_key]
    except Exception as err:
        out["error"] = f"{type(err).__name__}: {err}"
    try:
        out["kicad_cli"] = str(lib.find_kicad_cli())
    except FileNotFoundError as err:
        out["kicad_cli_error"] = str(err)
    queue.put(out)

if __name__ == "__main__":
    ctx = mp.get_context(sys.argv[1])
    queue = ctx.Queue()
    worker = ctx.Process(target=probe, args=(queue,))
    worker.start()
    result = queue.get(timeout=120)
    worker.join(timeout=60)
    print(json.dumps(result))
"""


def write_tree(root: Path, files) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))


class UrlGuardTest(unittest.TestCase):
    def test_the_runner_picker_is_accepted(self):
        self.assertIsNone(hook.url_problem(PICKER, PICKER))
        self.assertIsNone(hook.url_problem(PICKER + "/", PICKER))
        self.assertIsNone(hook.url_problem("http://[::1]:5000", "http://[::1]:5000"))

    def test_anything_else_is_refused(self):
        refused = [
            "https://legacy.atopileapi.com",
            "http://127.0.0.1:43124",
            "https://127.0.0.1:43123",
            "http://localhost:43123",
            "http://127.0.0.1:43123/v2",
            "http://user:pw@127.0.0.1:43123",
            "http://127.0.0.2:43123",
            "http://127.0.0.1.example.com:43123",
            "",
        ]
        for url in refused:
            self.assertIsNotNone(hook.url_problem(url, PICKER), url)

    def test_the_allowed_url_itself_must_be_loopback_http_with_a_port(self):
        for allowed in ("", "http://192.0.2.1:80", "http://127.0.0.1", "https://127.0.0.1:1"):
            self.assertIsNotNone(hook.url_problem(allowed or PICKER, allowed), allowed)


class PatchTest(unittest.TestCase):
    """The patch functions, applied to small fake modules in this process."""

    def fake_atopile(self, url):
        config = types.ModuleType("atopile.config")
        project = types.SimpleNamespace(
            services=types.SimpleNamespace(components=types.SimpleNamespace(url=url)),
            paths=types.SimpleNamespace(parts="/nonexistent"),
        )
        config.config = types.SimpleNamespace(project=project)
        errors = types.ModuleType("atopile.errors")

        class UserException(Exception):
            def __init__(self, message, title=None):
                super().__init__(message)

        errors.UserException = UserException
        return {
            "atopile": types.ModuleType("atopile"),
            "atopile.config": config,
            "atopile.errors": errors,
        }

    def api_module(self, url):
        module = types.ModuleType("faebryk.libs.picker.api.api")

        class ApiClient:
            class ApiConfig:
                api_url = url

                def __init__(self, api_url=None, api_key=None):
                    self.api_url, self.api_key = api_url, api_key

            calls = []

            @property
            def _cfg(self):
                raise RuntimeError("unpatched")

            def fetch_parts_multiple(self, params):
                ApiClient.calls.append(params)
                return ["network"]

        module.ApiClient = ApiClient
        return module

    def test_empty_queries_never_reach_the_client(self):
        module = self.api_module(PICKER)
        hook.patch_api(module)
        client = module.ApiClient()
        self.assertEqual(client.fetch_parts_multiple([]), [])
        self.assertEqual(module.ApiClient.calls, [])
        self.assertEqual(client.fetch_parts_multiple([{"lcsc": 1}]), ["network"])

    def test_token_only_for_the_runner_picker(self):
        for url, ok in ((PICKER, True), ("https://legacy.atopileapi.com", False)):
            module = self.api_module(url)
            hook.patch_api(module)
            env = {hook.PICKER_URL: PICKER}
            with mock.patch.dict(sys.modules, self.fake_atopile(url)), mock.patch.dict(
                os.environ, env
            ):
                client = module.ApiClient()
                if ok:
                    self.assertEqual(client._cfg.api_key, hook.TOKEN)
                    self.assertEqual(client._cfg.api_url, PICKER)
                else:
                    with self.assertRaisesRegex(Exception, "local picker only"):
                        client._cfg

    def test_missing_attributes_fail_loudly(self):
        with self.assertRaises(hook.HookError):
            hook.patch_api(types.ModuleType("faebryk.libs.picker.api.api"))
        with self.assertRaises(hook.HookError):
            hook.patch_lcsc(types.ModuleType("faebryk.libs.picker.lcsc"))

    def test_kicad_cli_comes_from_the_runner_only(self):
        module = types.ModuleType("kicadcliwrapper.lib")
        module.find_kicad_cli = lambda: "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
        hook.patch_kicadcli(module)
        with mock.patch.dict(os.environ, {hook.KICAD_CLI: ""}):
            with self.assertRaises(FileNotFoundError):
                module.find_kicad_cli()
        with mock.patch.dict(os.environ, {hook.KICAD_CLI: sys.executable}):
            self.assertEqual(str(module.find_kicad_cli()), sys.executable)

    def test_pcbnew_is_never_started_and_footprints_are_redirected(self):
        module = types.ModuleType("faebryk.libs.kicad.paths")
        module.find_pcbnew = lambda: "/Applications/KiCad/KiCad.app/Contents/MacOS/pcbnew"
        module.GLOBAL_FP_DIR_PATH = Path("/Applications/KiCad/footprints")
        with mock.patch.dict(os.environ, {hook.KICAD_FOOTPRINTS: "/opt/kicad/footprints"}):
            hook.patch_kicad_paths(module)
        with self.assertRaises(FileNotFoundError):
            module.find_pcbnew()
        self.assertEqual(module.GLOBAL_FP_DIR_PATH, Path("/opt/kicad/footprints"))

    def lcsc_module(self):
        module = types.ModuleType("faebryk.libs.picker.lcsc")

        class LCSC_ServiceException(Exception):
            def __init__(self, partno, *args):
                super().__init__(partno, *args)

        module.LCSC_ServiceException = LCSC_ServiceException
        module.downloads = []
        module.download_easyeda_info = lambda lcsc_id, get_model=True: module.downloads.append(
            lcsc_id
        )
        module.get_raw = lambda lcsc_id: module.downloads.append(("raw", lcsc_id))
        return module

    def test_offline_refuses_easyeda(self):
        module = self.lcsc_module()
        hook.patch_lcsc(module)
        with mock.patch.dict(os.environ, {hook.OFFLINE: "1", hook.LOCAL_PARTS: "0"}):
            with self.assertRaisesRegex(module.LCSC_ServiceException, "offline"):
                module.download_easyeda_info("C900201")
            with self.assertRaises(module.LCSC_ServiceException):
                module.get_raw("C900201")
        self.assertEqual(module.downloads, [])
        with mock.patch.dict(os.environ, {hook.OFFLINE: "0", hook.LOCAL_PARTS: "0"}):
            module.download_easyeda_info("C900201")
        self.assertEqual(module.downloads, ["C900201"])

    def test_local_parts_are_found_by_supplier_partno(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = Path(tmp)
            for name, lcsc in (("B_Part", "C900301"), ("A_Part", "C900301"), ("C_Part", "C9")):
                (parts / name).mkdir()
                (parts / name / f"{name}.ato").write_text(
                    f'    trait has_part_picked::by_supplier<supplier_id="lcsc",'
                    f' supplier_partno="{lcsc}", manufacturer="m", partno="p">\n'
                )
            found = hook.find_local_part("C900301", parts, load=lambda d: d.name)
            self.assertEqual(found.atopart, "A_Part")
            self.assertIsNone(hook.find_local_part("C900302", parts, load=lambda d: d.name))
            self.assertIsNone(hook.find_local_part("C90030", parts, load=lambda d: d.name))

    def test_local_part_skips_ingestion(self):
        module = types.ModuleType("faebryk.libs.part_lifecycle")
        inserted = []

        class Library:
            def ingest_part_from_easyeda(self, epart):
                return "downloaded"

            def _insert_fp_lib(self, name):
                inserted.append(name)

        module.PartLifecycle = types.SimpleNamespace(Library=Library)
        hook.patch_part_lifecycle(module)
        atopart = types.SimpleNamespace(path=Path("/p/Synthetic_Part"))
        self.assertIs(Library().ingest_part_from_easyeda(hook.LocalPart("C1", atopart)), atopart)
        self.assertEqual(inserted, ["Synthetic_Part"])
        self.assertEqual(Library().ingest_part_from_easyeda(object()), "downloaded")


class InstallTest(unittest.TestCase):
    def test_disabled_without_the_flag(self):
        with mock.patch.dict(os.environ, {hook.ENABLE: ""}):
            self.assertFalse(hook.install())

    def test_too_late_is_an_error(self):
        with mock.patch.dict(os.environ, {hook.ENABLE: "1"}), mock.patch.dict(
            sys.modules, {"kicadcliwrapper.lib": types.ModuleType("kicadcliwrapper.lib")}
        ):
            with self.assertRaises(hook.HookError):
                hook.install()


class WorkerTest(unittest.TestCase):
    """sitecustomize installs the hook in every interpreter of the tree, workers included."""

    def run_child(self, method, enabled):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_tree(root / "site", FAKE_MODULES)
            (root / "child.py").write_text(CHILD)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": tmp,
                "PYTHONPATH": os.pathsep.join([str(HOOK_DIR), str(root / "site")]),
                "FAKE_URL": PICKER,
                "YAPNR_ATO_HOOK": "1" if enabled else "0",
                "YAPNR_ATO_PICKER_URL": PICKER,
                "YAPNR_ATO_KICAD_CLI": "",
                "YAPNR_ATO_HOOK_LOG": str(root / "hook.jsonl"),
            }
            result = subprocess.run(
                [sys.executable, str(root / "child.py"), method],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            events = []
            if (root / "hook.jsonl").exists():
                events = [json.loads(x) for x in (root / "hook.jsonl").read_text().splitlines()]
            return json.loads(result.stdout), events

    def test_forkserver_and_spawn_workers_are_patched(self):
        methods = ["spawn"] + (["forkserver"] if sys.platform != "win32" else [])
        for method in methods:
            with self.subTest(method=method):
                out, events = self.run_child(method, enabled=True)
                self.assertEqual(out["empty"], [])
                self.assertIsNone(out["posted"])
                self.assertEqual(out["cfg"], [PICKER, hook.TOKEN])
                self.assertIn("yapnr runs only the headless KiCad", out["kicad_cli_error"])
                worker_pids = {e["pid"] for e in events if e["event"] == "patched"}
                self.assertTrue(worker_pids)
                self.assertIn({"event": "empty-query"}, [{"event": e["event"]} for e in events])

    def test_unpatched_when_disabled(self):
        out, events = self.run_child("spawn", enabled=False)
        self.assertEqual(out["empty"], [["posted"]])
        self.assertIn("unpatched", out["error"])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
