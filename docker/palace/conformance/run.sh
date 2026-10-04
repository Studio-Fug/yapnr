#!/bin/sh
# run.sh PREFIX [LIBRARY...]: build nodend_check.c against the ParMETIS API installed in PREFIX
# (PREFIX/include/parmetis.h and the libraries, by default Scotch's libptscotchparmetisv3 and the
# PT-Scotch libraries under it) and run it under MPI on 3-D grid graphs (README.md here).
#
# The strict cases run at 1, 2, 4 and 8 ranks, the ranks SuperLU_DIST calls it on (a power of
# two); any failure fails the script. Two informational cases follow, whose results are printed
# but not enforced: a graph too small to split into 8 sub-domains, and 6 ranks (not a power of
# two; ParMETIS orders on 4 and fills `sizes` for 4, which SuperLU_DIST never relies on).
set -eu

prefix=$1
shift
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
if [ "$#" -eq 0 ]; then
  set -- "${prefix}/lib/libptscotchparmetisv3.a" "${prefix}/lib/libptscotch.a" \
    "${prefix}/lib/libptscotcherr.a" "${prefix}/lib/libscotch.a" "${prefix}/lib/libscotcherr.a"
fi
mpicc -O2 -Wall -Wextra -I"${prefix}/include" "${here}/nodend_check.c" "$@" -lm \
  -o "${work}/nodend_check"

# Ranks as processes on whatever cores the build machine has, as root in a container
export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_btl_vader_single_copy_mechanism=none OMPI_MCA_btl=self,vader
export OMPI_MCA_rmaps_base_oversubscribe=1
run() {
  np=$1
  shift
  if mpirun -np "${np}" "${work}/nodend_check" "$@" > "${work}/last.txt" 2>&1; then
    cat "${work}/last.txt"
  else
    cat "${work}/last.txt"
    return 1
  fi
}
# twice: the same checksum both times (a deterministic ordering from run to run)
twice() {
  run "$@"
  first=$(grep -o 'checksum [0-9a-f]*' "${work}/last.txt")
  run "$@"
  second=$(grep -o 'checksum [0-9a-f]*' "${work}/last.txt")
  if [ -z "${first}" ] || [ "${first}" != "${second}" ]; then
    echo "nodend_check: FAIL: two runs differ (${first} / ${second})" >&2
    exit 1
  fi
  echo "nodend_check:   same ${first} on a second run"
}

run 1 12 10 8
run 2 30 20 10
run 2 30 20 10 --numflag 1 --skew
twice 4 30 20 10
run 4 24 24 24 --skew
twice 8 40 30 20
run 8 24 24 24 --numflag 1 --skew
run 8 64 16 16
echo "nodend_check: strict cases PASS"

echo "nodend_check: informational: 8 ranks, 6x6x6 grid (27 vertices per sub-domain)"
run 8 6 6 6 || echo "nodend_check: informational case did not pass (not enforced)"
echo "nodend_check: informational: 6 ranks (not a power of two), ordering only"
run 6 30 20 10 --order-only || echo "nodend_check: informational case did not pass (not enforced)"
