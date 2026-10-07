#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
timeout 120 "$PYTHON" -m unittest discover -s tests -v
