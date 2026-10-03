#!/usr/bin/env python3
"""Offline checks of infra/gcp: ``tofu fmt -check``, ``validate``, ``test`` and a plan.

    python3 infra/gcp/tofu_check.py            # from a checkout, with tofu on PATH (or TOFU=...)
    bazel test //infra/gcp:tofu_check --test_env=TOFU=/path/to/tofu \
        --test_env=TF_PLUGIN_CACHE_DIR=/path/to/cache

``tofu init`` downloads the pinned providers (from the OpenTofu registry; a plugin cache avoids
repeats). Nothing talks to Google Cloud: ``tofu test`` uses mock providers, and the plan runs
against the documentation project of ``examples/owner.tfvars.example`` with a placeholder access
token, ``-refresh=false``, and every HTTP(S) request sent to a closed local port, so any API call
the provider tried would fail the check instead of reaching Google.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLACKHOLE = "http://127.0.0.1:9"
COPY = ("versions.tf", "main.tf", "variables.tf", "outputs.tf", "backend.tf", ".terraform.lock.hcl")
DIRS = ("modules", "functions", "examples", "tests")
TIMEOUT_S = 900


def tofu_binary() -> str:
    found = os.environ.get("TOFU") or shutil.which("tofu")
    if not found:
        sys.exit("tofu_check: no tofu (set TOFU=/path/to/tofu or put it on PATH)")
    return found


def run(argv, cwd, env, check=True):
    print("+ " + " ".join(argv[1:]), flush=True)
    done = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=TIMEOUT_S)
    sys.stdout.write(done.stdout[-4000:])
    sys.stderr.write(done.stderr[-4000:])
    if check and done.returncode != 0:
        sys.exit("tofu_check: %s failed (%d)" % (argv[1], done.returncode))
    return done


def main() -> int:
    tofu = tofu_binary()
    with tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as tmp:
        work = Path(tmp) / "gcp"
        work.mkdir()
        for name in COPY:
            shutil.copy(HERE / name, work / name)
        for name in DIRS:
            shutil.copytree(HERE / name, work / name)
        env = dict(os.environ, TF_IN_AUTOMATION="1", TF_DATA_DIR=str(Path(tmp) / "data"))
        run([tofu, "fmt", "-check", "-recursive", "-no-color"], work, env)
        run([tofu, "init", "-backend=false", "-input=false", "-no-color"], work, env)
        run([tofu, "validate", "-no-color"], work, env)
        run([tofu, "test", "-no-color"], work, env)
        # The plan: a local backend instead of gcs, and no way out to the network.
        (work / "backend_override.tf").write_text('terraform {\n  backend "local" {}\n}\n')
        run([tofu, "init", "-reconfigure", "-input=false", "-no-color"], work, env)
        offline = dict(
            env,
            GOOGLE_OAUTH_ACCESS_TOKEN="offline-plan-placeholder",
            HTTPS_PROXY=BLACKHOLE,
            HTTP_PROXY=BLACKHOLE,
            https_proxy=BLACKHOLE,
            http_proxy=BLACKHOLE,
            NO_PROXY="",
            no_proxy="",
        )
        plan = run(
            [
                tofu,
                "plan",
                "-refresh=false",
                "-lock=false",
                "-input=false",
                "-no-color",
                "-var-file=examples/owner.tfvars.example",
            ],
            work,
            offline,
        )
        if "Plan:" not in plan.stdout:
            sys.exit("tofu_check: the plan printed no summary")
    print("tofu_check: fmt, validate, test and the offline plan passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
