"""The atopile build environment is an allowlist with private directories."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr.frontends.atopile import env

PICKER = "http://127.0.0.1:40001"


class BuildEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def test_caller_environment_does_not_leak(self):
        secrets = {
            "ANTHROPIC_API_KEY": "x",
            "OPENAI_API_KEY": "x",
            "GITHUB_TOKEN": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "HTTPS_PROXY": "http://proxy.example:3128",
            "PYTHONPATH": "/somewhere",
            "ATO_SERVICES_COMPONENTS_URL": "https://legacy.atopileapi.com",
            "YAPNR_PART_CACHE_TOKEN": "x",
        }
        with mock.patch.dict(os.environ, secrets):
            child = env.build_env(self.work, PICKER)
        for name in secrets:
            if name in child:
                self.assertNotEqual(child[name], secrets[name], name)
        self.assertEqual(child["ATO_SERVICES_COMPONENTS_URL"], PICKER)
        self.assertEqual(child["ATO_SERVICES_PACKAGES_URL"], PICKER)
        self.assertEqual(child["PYTHONPATH"], str(env.HOOK_DIR))

    def test_offline_settings(self):
        child = env.build_env(self.work, PICKER)
        expected = {
            "CI": "1",
            "ATO_NON_INTERACTIVE": "1",
            "FBRK_TELEMETRY": "0",
            "YAPNR_ATO_HOOK": "1",
            "YAPNR_ATO_PICKER_URL": PICKER,
            "YAPNR_ATO_OFFLINE": "1",
            "YAPNR_ATO_LOCAL_PARTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        for name, value in expected.items():
            self.assertEqual(child[name], value, name)
        online = env.build_env(self.work, PICKER, offline=False)
        self.assertEqual(online["YAPNR_ATO_OFFLINE"], "0")
        self.assertNotIn("GIT_ALLOW_PROTOCOL", online)

    def test_private_directories_live_in_the_work_directory(self):
        child = env.build_env(self.work, PICKER)
        for name in (
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            path = Path(child[name])
            self.assertTrue(path.is_dir(), name)
            self.assertTrue(path.is_relative_to(self.work), name)

    def test_openssl_armcap_on_aarch64_only(self):
        self.assertEqual(env.build_env(self.work, PICKER, machine="aarch64")["OPENSSL_armcap"], "0")
        self.assertEqual(env.build_env(self.work, PICKER, machine="arm64")["OPENSSL_armcap"], "0")
        self.assertNotIn("OPENSSL_armcap", env.build_env(self.work, PICKER, machine="x86_64"))

    def test_kicad_and_hook_log(self):
        child = env.build_env(
            self.work,
            PICKER,
            kicad_cli=Path("/opt/kicad/bin/kicad-cli"),
            footprints=Path("/opt/kicad/footprints"),
            hook_log=self.work / "hook.jsonl",
        )
        self.assertEqual(child["YAPNR_ATO_KICAD_CLI"], "/opt/kicad/bin/kicad-cli")
        self.assertEqual(child["YAPNR_ATO_KICAD_FOOTPRINTS"], "/opt/kicad/footprints")
        self.assertEqual(child["YAPNR_ATO_HOOK_LOG"], str(self.work / "hook.jsonl"))
        self.assertNotIn("YAPNR_ATO_KICAD_CLI", env.build_env(self.work, PICKER))

    def test_hook_files_exist(self):
        self.assertTrue((env.HOOK_DIR / "sitecustomize.py").is_file())
        self.assertTrue((env.HOOK_DIR / "yapnr_atopile_hook.py").is_file())


if __name__ == "__main__":
    unittest.main()
