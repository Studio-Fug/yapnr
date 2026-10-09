"""Project cloud configuration preserves schema, concurrency and credential boundaries."""

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from yapnr.agent import cloud
from yapnr.exp import config


class CloudTest(unittest.TestCase):
    def test_roundtrip_and_optimistic_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {config.ENV_CONFIG: str(root / "operator.toml")}):
                original = cloud.read(root)
                settings = {"local": {"workers": 2}, "limits": {"max_tasks": 8}}
                current = cloud.save(root, settings, original["sha256"])
                self.assertEqual(current["settings"], settings)
                self.assertEqual(tomllib.loads((root / cloud.RELATIVE).read_text()), settings)
                self.assertEqual(current["scope"], "project")
                with self.assertRaisesRegex(ValueError, "changed"):
                    cloud.save(root, {"local": {"workers": 3}}, original["sha256"])
                with self.assertRaisesRegex(ValueError, "operator"):
                    cloud.save(
                        root, {"prices": {"price_api_key_command": ["secret"]}}, current["sha256"]
                    )
                with self.assertRaises(config.ConfigError):
                    cloud.save(root, {"local": {"workers": 100}}, current["sha256"])

    def test_project_configuration_precedence_and_nested_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / cloud.RELATIVE
            path.parent.mkdir(parents=True)
            path.write_text("[local]\nworkers = 2\n")
            child = root / "design"
            child.mkdir()
            (child / ".yapnr/parts").mkdir(parents=True)
            with patch.object(config.Path, "cwd", return_value=child), patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertEqual(config.config_path(), path)
                self.assertEqual(config.load().local.workers, 2)
                with patch.dict(os.environ, {config.ENV_CONFIG: str(root / "operator.toml")}):
                    self.assertEqual(config.config_path(), root / "operator.toml")
                    self.assertEqual(
                        config.config_path(str(root / "explicit.toml")), root / "explicit.toml"
                    )

    def test_sdk_does_not_return_unrelated_auth_properties(self):
        profile = (
            '[{"name":"demo","is_active":true,"properties":'
            '{"core":{"project":"demo"},"auth":{"client_secret":"hidden"}}}]'
        )
        account = '[{"account":"operator@example.test","status":"ACTIVE","token":"hidden"}]'
        with patch.object(cloud, "read", return_value={"settings": {}}), patch.object(
            cloud.shutil, "which", return_value="gcloud"
        ), patch.object(
            cloud.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0, profile.encode()),
                subprocess.CompletedProcess([], 0, account.encode()),
            ],
        ) as run:
            result = cloud.sdk(Path("."))
            self.assertNotIn("hidden", str(result))
            self.assertNotIn("auth", result["profiles"][0]["properties"])
            self.assertEqual(run.call_args_list[1].args[0][1:3], ["auth", "list"])


if __name__ == "__main__":
    unittest.main()
