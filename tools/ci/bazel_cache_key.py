"""Cache compatibility keys; no workspace/output-base/runfiles caches.

Partition by hosted image and compiler/SDK to avoid cross-toolchain snapshots.
Bazel action keys still decide which individual build outputs can be reused.
"""

from __future__ import annotations

import hashlib
import os
import subprocess


def cache_keys(env, toolchain):
    platform = f"{env['RUNNER_OS']}-{env['RUNNER_ARCH']}"
    digest = hashlib.sha256(toolchain.encode()).hexdigest()[:20]
    inputs = env["BAZEL_INPUTS_HASH"]
    prefix = f"bazel-disk-v2-{platform}-{digest}-{inputs}-"
    return {
        "disk-prefix": prefix,
        "disk-key": f"{prefix}{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}-{env['GITHUB_JOB']}",
        "repo-key": f"bazel-repo-v2-{platform}-{inputs}",
    }


def toolchain_identity(env):
    commands = [["cc", "--version"]]
    if env["RUNNER_OS"] == "macOS":
        commands += [["xcodebuild", "-version"], ["xcrun", "--show-sdk-version"]]
    parts = [env.get("ImageOS", ""), env.get("ImageVersion", "")]
    for command in commands:
        parts.append(subprocess.check_output(command, text=True, timeout=30))
    return "\n".join(parts)


if __name__ == "__main__":
    for key, value in cache_keys(os.environ, toolchain_identity(os.environ)).items():
        print(f"{key}={value}")
