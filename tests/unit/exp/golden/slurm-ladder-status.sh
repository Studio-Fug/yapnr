#!/bin/bash
# yapnr exp: status of campaign 20261002-ladder-cca5a3 on Slurm site example-site (bash and Slurm only).
set -euo pipefail
store="$SCRATCH/yapnr-store"
dir="${store}/campaigns/20261002-ladder-cca5a3"
total="$(wc -l < "${dir}/tasks.jsonl" | tr -d " ")"
done_count="$(find "${dir}/tasks" -name _DONE 2>/dev/null | wc -l | tr -d " ")"
echo "campaign 20261002-ladder-cca5a3: ${done_count} of ${total} tasks done"
for record in "${dir}"/submissions/*.json; do
  [ -e "${record}" ] || continue
  job="$(sed -n 's/.*"id": "\([0-9_]*\)".*/\1/p' "${record}")"
  [ -n "${job}" ] && sacct -X -n -j "${job}" --format=JobID,State,Elapsed || true
done
