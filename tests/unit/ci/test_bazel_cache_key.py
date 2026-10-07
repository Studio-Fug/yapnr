"""Cache keys retain compatible work while separating toolchains and run attempts."""

import unittest
from unittest.mock import patch

from tools.ci.bazel_cache_key import cache_keys, toolchain_identity


class CacheKeyTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(
            RUNNER_OS="macOS",
            RUNNER_ARCH="ARM64",
            BAZEL_INPUTS_HASH="locked-inputs",
            GITHUB_RUN_ID="100",
            GITHUB_RUN_ATTEMPT="1",
            ImageOS="macos26",
            ImageVersion="20260907.0351.1",
        )

    def test_new_run_and_retry_snapshot_keep_compatible_restore_prefix(self):
        first = cache_keys(self.env, "compiler/sdk")
        for changes in ({"GITHUB_RUN_ID": "101"}, {"GITHUB_RUN_ATTEMPT": "2"}):
            with self.subTest(changes=changes):
                next_keys = cache_keys(dict(self.env, **changes), "compiler/sdk")
                self.assertNotEqual(first["disk-key"], next_keys["disk-key"])
                self.assertEqual(first["disk-prefix"], next_keys["disk-prefix"])
                self.assertEqual(first["repo-key"], next_keys["repo-key"])
                self.assertTrue(next_keys["disk-key"].startswith(first["disk-prefix"]))

    def test_platform_arch_dependencies_and_toolchain_isolate_build_outputs(self):
        first = cache_keys(self.env, "compiler/sdk")
        for changes in (
            {"RUNNER_OS": "Linux"},
            {"RUNNER_ARCH": "X64"},
            {"BAZEL_INPUTS_HASH": "new-lock"},
        ):
            self.assertNotEqual(
                first["disk-prefix"],
                cache_keys(dict(self.env, **changes), "compiler/sdk")["disk-prefix"],
            )
        self.assertNotEqual(first["disk-prefix"], cache_keys(self.env, "new SDK")["disk-prefix"])
        self.assertEqual(first["repo-key"], cache_keys(self.env, "new SDK")["repo-key"])

    @patch("tools.ci.bazel_cache_key.subprocess.check_output", return_value="version")
    def test_macos_probes_compiler_xcode_sdk_with_timeouts(self, probe):
        identity = toolchain_identity(self.env)
        self.assertTrue(identity.startswith("macos26"))
        self.assertIn(self.env["ImageVersion"], identity)
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            [["cc", "--version"], ["xcodebuild", "-version"], ["xcrun", "--show-sdk-version"]],
        )
        self.assertTrue(all(call.kwargs["timeout"] == 30 for call in probe.call_args_list))


if __name__ == "__main__":
    unittest.main()
