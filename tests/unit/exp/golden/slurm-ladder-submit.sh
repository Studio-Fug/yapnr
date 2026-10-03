#!/bin/bash
# yapnr exp: submit campaign 20261002-ladder-cca5a3 on Slurm site example-site.
# Run on the login node from the plan directory copied there:
#   bash backend/slurm/submit.sh
# Needs bash, coreutils, Slurm (sbatch) and Apptainer. Running it again submits every
# task of each class again; array elements skip finished tasks (their _DONE markers) and
# exit in seconds. Run it again only after the earlier arrays ended (status.sh), or a
# task still running there runs twice.
set -euo pipefail
plan="$(cd "$(dirname "$0")/../.." && pwd)"
store="$SCRATCH/yapnr-store"
sif="$HOME/yapnr/yapnr-abababababab.sif"
campaign=20261002-ladder-cca5a3
dir="${store}/campaigns/${campaign}"
if [ -e "${store}/control/frozen" ]; then
  echo "the store is frozen (control/frozen); not submitting" >&2
  exit 3
fi
mkdir -p "${store}/bundles" "${dir}/submissions" "${dir}/slurm"
for bundle in "${plan}"/bundles/*.tar.gz; do
  [ -e "${bundle}" ] || continue
  target="${store}/bundles/$(basename "${bundle}")"
  [ -e "${target}" ] || cp "${bundle}" "${target}"
done
for name in campaign.json tasks.jsonl task.py; do
  if [ -e "${dir}/${name}" ]; then
    cmp -s "${plan}/${name}" "${dir}/${name}" || {
      echo "${dir}/${name} differs from the plan" >&2
      exit 2
    }
  else
    cp "${plan}/${name}" "${dir}/${name}"
  fi
done
if [ ! -e "${sif}" ]; then
  echo "building ${sif} from ghcr.io/studio-fug/yapnr@sha256:abababababababababababababababababababababababababababababababab" >&2
  module load apptainer 2>/dev/null || true
  # The layers and the build need several times the image's size: keep them off HOME and
  # the login node's /tmp (set APPTAINER_CACHEDIR / APPTAINER_TMPDIR to override).
  export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-${store}/.apptainer/cache}"
  export APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-${store}/.apptainer/tmp}"
  mkdir -p "$(dirname "${sif}")" "${APPTAINER_CACHEDIR}" "${APPTAINER_TMPDIR}"
  apptainer pull "${sif}" "docker://ghcr.io/studio-fug/yapnr@sha256:abababababababababababababababababababababababababababababababab"
fi
sif_sha256="$(sha256sum "${sif}" | cut -d" " -f1)"
next=1
for record in "${dir}"/submissions/*.indices; do
  [ -e "${record}" ] || continue
  n="$(basename "${record}" .indices)"
  if [ "${n}" -ge "${next}" ]; then next=$((n + 1)); fi
done
classes=(c1m3)
for class in "${classes[@]}"; do
  n="${next}"
  next=$((next + 1))
  cp "${plan}/backend/slurm/${class}.indices" "${dir}/submissions/${n}.indices"
  count="$(wc -l < "${dir}/submissions/${n}.indices" | tr -d " ")"
  elements=$(((count + 2 - 1) / 2))
  job=$(sbatch --parsable --array=0-$((elements - 1))%16 \
    --output="${dir}/slurm/%A_%a.out" \
    --export="ALL,YAPNR_STORE=${store},YAPNR_SIF=${sif},YAPNR_SIF_SHA256=${sif_sha256},YAPNR_SUBMISSION=${n}" \
    "${plan}/backend/slurm/${class}.sbatch")
  job="${job%%;*}"  # --parsable prints <job id>[;<cluster>]
  cat > "${dir}/submissions/${n}.json" <<EOF
{"schema": "yapnr-submission-v1", "campaign": "${campaign}", "submission": ${n}, "backend": "slurm", "class": "${class}", "tasks": ${count}, "job": {"id": "${job}", "site": "example-site"}}
EOF
  echo "submission ${n}: class ${class}, ${count} task(s), job ${job}"
done
