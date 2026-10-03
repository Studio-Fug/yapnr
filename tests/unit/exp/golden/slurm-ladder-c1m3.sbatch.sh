#!/bin/bash
# yapnr exp: campaign 20261002-ladder-cca5a3, class c1m3.
#SBATCH --job-name=yapnr-20261002-ladder-cca5a3-c1m3
#SBATCH --account=example-account
#SBATCH --partition=cpu
#SBATCH --cpus-per-task=1
#SBATCH --mem=3072M
#SBATCH --time=1:20:00
#SBATCH --requeue
#SBATCH --signal=B:USR1@300
#
# submit.sh passes --array, --output and --export (YAPNR_STORE, YAPNR_SIF,
# YAPNR_SIF_SHA256, YAPNR_SUBMISSION) on the sbatch command line.
set -euo pipefail
module load apptainer 2>/dev/null || true
: "${YAPNR_STORE:?}" "${YAPNR_SIF:?}" "${YAPNR_SUBMISSION:?}"
scratch="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/yapnr-${SLURM_JOB_ID}"
mkdir -p "${scratch}"
restarts="${SLURM_RESTART_COUNT:-0}"
cmd=(apptainer exec --cleanenv --containall
  --bind "${YAPNR_STORE}:/store" --bind "${scratch}:/scratch"
  --env "YAPNR_BACKEND=slurm,YAPNR_SIF_SHA256=${YAPNR_SIF_SHA256:-}"
  "${YAPNR_SIF}" /usr/local/bin/yapnr-kicad-env /opt/venv/bin/python
  /store/campaigns/20261002-ladder-cca5a3/task.py --store /store --inputs /store/bundles
  --campaign 20261002-ladder-cca5a3 --submission "${YAPNR_SUBMISSION}"
  --index "${SLURM_ARRAY_TASK_ID}" --chunk 2 --retry "${restarts}"
  --toolchain image --work-root /scratch)
requeue() {
  kill -USR1 "${child}" 2>/dev/null || true
  wait "${child}" || true
  scontrol requeue "${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
  exit 0
}
trap requeue USR1
"${cmd[@]}" &
child=$!
status=0
wait "${child}" || status=$?
rm -rf "${scratch}"
# 75: a transient failure (staging, upload); requeue a bounded number of times.
if [ "${status}" -eq 75 ] && [ "${restarts}" -lt 3 ]; then
  scontrol requeue "${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
  exit 0
fi
exit "${status}"
