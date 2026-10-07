#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ENGINE:?Set ENGINE to a checkout of yapnr at dd1ba7d47c2e18607f1772a9a48acabe8a62045d}"
: "${KICAD_PYTHON:?Set KICAD_PYTHON to a headless KiCad 10 Python with Shapely and NumPy}"
: "${KICAD_CLI:?Set KICAD_CLI to the matching headless KiCad 10 CLI}"
export PYTHONPATH="$PWD:$ENGINE:$ENGINE/hardware/pnr${PYTHONPATH:+:$PYTHONPATH}"
export PNR_FAB_PROFILE=pcbway-adv-6l-rf
mkdir -p results/reproduction
# Every worker, native DRC subprocess, and the planner has a finite budget.
timeout 600 "$KICAD_PYTHON" -m prototype.real_case \
  --board fixtures/input/candidate.kicad_pcb --case fixtures/case.json \
  --rules fixtures/rules.json --out results/reproduction --kicad-cli "$KICAD_CLI" "$@"
