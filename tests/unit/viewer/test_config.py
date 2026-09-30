"""Viewer configuration: flags over the config file over values derived from the root; no machine
defaults; the paid features are off unless enabled."""

import tempfile
import unittest
from pathlib import Path

from yapnr.viewer import config

NO_MACHINE = {"YAPNR_USER_CONFIG": ""}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-config-")
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, *argv, env=None):
        return config.load(list(argv), dict(NO_MACHINE, **(env or {})))

    def test_defaults_are_derived_from_the_root(self):
        root = self.dir / "exp/live"
        cfg = self.load(str(root))
        exp = self.dir / "exp"
        self.assertEqual(
            (cfg.root, cfg.experiment, cfg.notes, cfg.preferences, cfg.capture_root, cfg.cache),
            (root, exp, exp / "notes", exp / "viewer-preferences.json", exp, root),
        )
        self.assertEqual((cfg.listen, cfg.port, cfg.allow_origin), (["127.0.0.1"], 8766, []))
        self.assertEqual(cfg.cache_dir("schematic"), root / "schematic")
        # nothing points at a machine: no design inputs, no tools, no engine override
        for name in ("graph", "rules", "atopile_root", "parts", "engine_runtime", "dist"):
            self.assertIsNone(getattr(cfg, name), name)
        for name in ("kicad_cli", "kicad_python", "claude"):
            self.assertIsNone(getattr(cfg, name), name)

    def test_paid_features_are_off_by_default(self):
        cfg = self.load(str(self.dir / "live"))
        self.assertEqual((cfg.agent, cfg.agent_web, cfg.net_summaries), (False, False, False))
        on = self.load(
            str(self.dir / "live"), "--agent", "on", "--agent-web", "on", "--net-summaries", "on"
        )
        self.assertEqual((on.agent, on.agent_web, on.net_summaries), (True, True, True))
        # no "auto": the agent follows neither the listener nor the summaries
        with self.assertRaises(SystemExit):
            self.load(str(self.dir / "live"), "--agent", "auto")

    def test_precedence_and_relative_paths(self):
        cfg_file = self.dir / "conf/viewer.toml"
        cfg_file.parent.mkdir()
        cfg_file.write_text(
            'schema = "yapnr-viewer-v1"\n'
            'root = "../runs/live"\n'
            '[server]\nport = 9000\ntitle = "Demo"\n'
            '[design]\ngraph = "inputs/graph.json"\n'
            '[design.atopile]\nroot = "../design"\nbuild = "default"\n'
            "[agent]\nenabled = true\nturn_budget_usd = 0.5\n"
            '[net_summaries]\nmodel = "haiku"\n'
        )
        cfg = self.load("--config", str(cfg_file), "--port", "9001")
        self.assertEqual(cfg.root, self.dir / "runs/live")  # relative to the file
        self.assertEqual(cfg.port, 9001)  # the flag wins
        self.assertEqual(cfg.title, "Demo")
        self.assertEqual(cfg.graph, self.dir / "conf/inputs/graph.json")
        self.assertEqual(cfg.rules, self.dir / "conf/inputs/rules.json")  # next to the graph
        self.assertEqual((cfg.atopile_root, cfg.atopile_build), (self.dir / "design", "default"))
        self.assertEqual((cfg.agent, cfg.agent_budget_usd), (True, 0.5))
        self.assertEqual(cfg.net_summary_model, "haiku")
        # relative flags are relative to where `bazel run` started
        cfg = self.load("rel/live", "--graph", "g.json", env={"BUILD_WORKING_DIRECTORY": "/w"})
        self.assertEqual((cfg.root, cfg.graph), (Path("/w/rel/live"), Path("/w/g.json")))

    def test_errors(self):
        bad = self.dir / "bad.toml"
        bad.write_text('schema = "yapnr-viewer-v1"\n[server]\nbogus = 1\n')
        with self.assertRaisesRegex(config.ConfigError, "unknown settings server.bogus"):
            self.load("--config", str(bad), "x")
        bad.write_text('schema = "other"\n')
        with self.assertRaisesRegex(config.ConfigError, "schema"):
            self.load("--config", str(bad), "x")
        bad.write_text('[kicad]\ncli = "/opt/kicad-cli"\n')  # machine keys: never in this file
        with self.assertRaisesRegex(config.ConfigError, "unknown settings kicad.cli"):
            self.load("--config", str(bad), "x")
        with self.assertRaisesRegex(config.ConfigError, "no live telemetry directory"):
            self.load()
        with self.assertRaisesRegex(config.ConfigError, "allow-origin"):
            self.load("x", "--allow-origin", "viewer.example.com")
        with self.assertRaisesRegex(config.ConfigError, "agent model"):
            self.load("x", "--agent-model", "gpt")

    def test_machine_config(self):
        machine = self.dir / "machine.toml"
        machine.write_text(
            '[kicad]\ncli = "bin/kicad-cli"\npython = "/opt/kicad/python3"\n'
            '[agent]\nclaude = "/opt/claude/claude"\n'
        )
        cfg = config.load(["x"], {"YAPNR_USER_CONFIG": str(machine)})
        self.assertEqual(cfg.machine_kicad_cli, self.dir / "bin/kicad-cli")
        self.assertEqual(cfg.machine_kicad_python, Path("/opt/kicad/python3"))
        self.assertEqual(cfg.claude, "/opt/claude/claude")
        self.assertIsNone(cfg.kicad_cli)  # the flag and the environment still come first
        cfg = config.load(["x", "--claude", "claude"], {"YAPNR_USER_CONFIG": str(machine)})
        self.assertEqual(cfg.claude, "claude")
        self.assertEqual(
            config.user_config_path({"HOME": "/h"}), Path("/h/.config/yapnr/config.toml")
        )
        self.assertEqual(
            config.user_config_path({"XDG_CONFIG_HOME": "/x"}), Path("/x/yapnr/config.toml")
        )
        self.assertIsNone(config.user_config_path({"YAPNR_USER_CONFIG": ""}))


if __name__ == "__main__":
    unittest.main()
