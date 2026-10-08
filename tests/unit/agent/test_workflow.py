"""Article resume/preservation and installed guidance contracts."""

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from yapnr.agent.cli import chat_command, initialize, instructions, provider_environment, run


class WorkflowTest(unittest.TestCase):
    def test_endpoint_uses_environment_reference_without_storing_secret(self):
        args = Namespace(
            provider="opencode",
            base_url="http://localhost:8000/v1",
            model="local/model",
            api_key_env="TEST_MODEL_KEY",
        )
        with patch.dict("os.environ", {"TEST_MODEL_KEY": "test-secret"}):
            environment, model = provider_environment(args)
        config = environment["OPENCODE_CONFIG_CONTENT"]
        self.assertNotIn("test-secret", config)
        self.assertIn("{env:TEST_MODEL_KEY}", config)
        self.assertEqual(model, "yapnr_endpoint/local/model")
        command = chat_command("opencode", "/bin/opencode", "context.md", "Design X", model)
        self.assertEqual(command[1], "--prompt")
        self.assertEqual(command[-2:], ["--model", model])
        for endpoint in ("https://user:secret@example.com/v1", "https://example.com/?key=secret"):
            args.base_url = endpoint
            with self.assertRaises(ValueError):
                provider_environment(args)

    def test_bootstrap_and_resume_preserve_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = initialize(tmp, "Design a divider")
            root = Path(tmp)
            checkpoint = root / ".yapnr/agent/checkpoint.json"
            data = json.loads(checkpoint.read_text())
            self.assertFalse(data["requirements_approved"])
            self.assertEqual(data["requested_feature"], "Design a divider")
            (root / "requirements/manifest.md").write_text("REQ-1 accepted 50 ohms\n")
            checkpoint.write_text('{"stage":"validation"}\n')
            (root / "AGENTS.md").write_text("Article instructions\n")
            second = initialize(tmp, "Replacement directive")
            self.assertEqual(second["created"], [])
            self.assertEqual(checkpoint.read_text(), '{"stage":"validation"}\n')
            self.assertEqual(
                (root / "requirements/manifest.md").read_text(), "REQ-1 accepted 50 ohms\n"
            )
            self.assertEqual((root / "AGENTS.md").read_text(), "Article instructions\n")
            self.assertEqual(Path(state["context"]).read_text(), instructions())

    def test_provider_prompt_has_guidance_and_literal_directive(self):
        directive = "Design X; $(echo not-a-shell)"
        for provider in ("codex", "claude"):
            command = chat_command(provider, "/bin/" + provider, "context.md", directive)
            self.assertEqual(len(command), 2)
            self.assertIn("AGENTS.md", command[1])
            self.assertIn("context.md", command[1])
            self.assertTrue(command[1].endswith(directive))
            self.assertNotIn("--dangerously", command[0])

    def test_dry_run_does_not_create_article_or_call_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new"
            args = Namespace(
                action="chat",
                provider="codex",
                project=str(target),
                directive="Design X",
                dry_run=True,
            )
            with patch("shutil.which", return_value="/bin/codex"), patch("os.execv") as execute:
                self.assertEqual(run(args), 0)
                execute.assert_not_called()
            self.assertFalse(target.exists())

    def test_missing_provider_blocks_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = Namespace(
                action="chat",
                provider="claude",
                project=str(Path(tmp) / "new"),
                directive="",
                dry_run=False,
            )
            with patch("shutil.which", return_value=None):
                self.assertEqual(run(args), 2)
            self.assertFalse((Path(tmp) / "new").exists())

    def test_guidance_preserves_acceptance_and_actual_capabilities(self):
        self.assertIn("<DONE>", instructions())
        self.assertIn("total route limit", instructions())
        self.assertIn("#97", instructions(True))
        self.assertIn("read-only", instructions(True))


if __name__ == "__main__":
    unittest.main()
