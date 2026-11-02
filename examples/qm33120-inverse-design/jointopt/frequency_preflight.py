"""Offline pinned-generator preflight; never constructs Problem or runs FDTD."""

import argparse
import hashlib
import json
from pathlib import Path

from yapnr.rf.spec import Spec

from .frequencies import INPUT_SPEC_SHA256, verify_pinned_generation
from .objective import Policy


def preflight(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != INPUT_SPEC_SHA256:
        raise ValueError(
            "Original source spec bytes changed; use the frozen input, not a rewritten equivalent"
        )
    result = verify_pinned_generation(Spec.from_dict(json.loads(raw)))
    return dict(
        schema="jointopt-frequency-offline-preflight/1",
        passed=True,
        frequency_contract=result,
        objective_policy_sha256=Policy().digest(),
        worker_request_schema="jointopt-native-request/2",
        FDTD_runs=0,
        paid_jobs=0,
        retry_authorized=False,
        native_geometry_and_field_qualification=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_spec")
    parser.add_argument("--report")
    args = parser.parse_args()
    result = preflight(args.original_spec)
    text = json.dumps(result, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
