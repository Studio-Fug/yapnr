#!/usr/bin/env bash
# Smoke-test a yapnr or yapnr-kicad container image (docs/containers.md).
#
#   tools/image/smoke_image.sh [options] IMAGE
#
#   --kicad-only        IMAGE is a yapnr-kicad base image (skip the yapnr checks)
#   --kicad-version V   expected KiCad version (default: from docker/yapnr-kicad/TAG)
#   --version V         expected `yapnr --version` (PEP 440; default: not checked)
#   --revision SHA      expected YAPNR_SOURCE_REVISION (default: not checked)
#
# Every image:
#   - runs as UID 1000 by default, with tini as PID 1, and without DISPLAY/WAYLAND_DISPLAY;
#   - carries the notices in /usr/share/doc/yapnr (SOURCES, THIRD_PARTY.md) and records the
#     Ubuntu archive snapshot its packages come from (/etc/yapnr/ubuntu-snapshot, in SOURCES);
#   - the build left nothing in /tmp, /root/.cache or /root/.local;
#   - `kicad-cli version` prints the expected KiCad version;
#   - tools/image/pcbnew_smoke.py: pcbnew loads a footprint through the seeded global
#     footprint table, saves a board, and `kicad-cli pcb drc` checks it;
#   all of that as the default user, as an arbitrary UID (4242) and with a read-only root file
#   system (`--read-only --tmpfs /tmp`).
# yapnr images, in addition:
#   - `yapnr --version`, and `yapnr doctor --json`: Python 3.11, the numpy and torch pins of
#     docker/yapnr/runtime-<arch>.lock, kicad-cli and KiCad's Python configured and executable,
#     the source revision; the default command (doctor) succeeds;
#   - the controller Python imports torch and numpy and converts between them, and cannot
#     import pcbnew (KiCad-side code runs under YAPNR_KICAD_PYTHON);
#   - tools/image/rf_native_smoke.py: yapnr.rf's native FDTD library from the wheel loads, is
#     the default backend and steps fields bit-identically to numpy;
#   - the license texts of the bundled native code: python-build-standalone's in
#     /usr/share/doc/yapnr/licenses/python-build-standalone, third_party/image-licenses in
#     /usr/share/doc/yapnr/licenses, GPL-3 and LGPL-2.1 in /usr/share/common-licenses.
# The image must be present locally (docker pull, or a build with --load).
set -euo pipefail

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

KICAD_ONLY=0
KICAD_VERSION=""
VERSION=""
REVISION=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --kicad-only) KICAD_ONLY=1 ;;
        --kicad-version) KICAD_VERSION="$2"; shift ;;
        --version) VERSION="$2"; shift ;;
        --revision) REVISION="$2"; shift ;;
        -h | --help) usage ;;
        -*) echo "unknown option: $1" >&2; usage ;;
        *) break ;;
    esac
    shift
done
[ "$#" -eq 1 ] || usage
IMAGE="$1"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
if [ -z "${KICAD_VERSION}" ]; then
    KICAD_VERSION="$(sed -E 's/-[0-9]+$//' "${ROOT}/docker/yapnr-kicad/TAG")"
fi

FAILURES=0
pass() { echo "  ok    $*"; }
fail() {
    echo "  FAIL  $*" >&2
    FAILURES=$((FAILURES + 1))
}

# docker run with the image's own entrypoint replaced by the KiCad environment wrapper.
kicad_run() {
    docker run --rm --entrypoint /usr/local/bin/yapnr-kicad-env "$@"
}

echo "Smoke-testing ${IMAGE} (KiCad ${KICAD_VERSION})"

arch="$(docker image inspect --format '{{.Architecture}}' "${IMAGE}")"
entrypoint="$(docker image inspect --format '{{json .Config.Entrypoint}}' "${IMAGE}")"
echo "  architecture ${arch}, entrypoint ${entrypoint}"
case "${entrypoint}" in
    '["/usr/bin/tini","--",'*) pass "tini is PID 1" ;;
    *) fail "entrypoint does not start with tini: ${entrypoint}" ;;
esac

uid="$(docker run --rm --entrypoint id "${IMAGE}" -u)"
[ "${uid}" = 1000 ] && pass "default user is UID 1000" || fail "default user is UID ${uid}"

if docker run --rm --entrypoint env "${IMAGE}" | grep -Eq '^(DISPLAY|WAYLAND_DISPLAY)='; then
    fail "a GUI session variable is set in the image"
else
    pass "no DISPLAY or WAYLAND_DISPLAY"
fi

notices="/usr/share/doc/yapnr/SOURCES /usr/share/doc/yapnr/THIRD_PARTY.md"
if [ "${KICAD_ONLY}" = 0 ]; then
    notices="${notices} /usr/share/doc/yapnr/LICENSE /usr/share/common-licenses/GPL-3"
    notices="${notices} /usr/share/common-licenses/LGPL-2.1"
    for text in "${ROOT}"/third_party/image-licenses/*; do
        notices="${notices} /usr/share/doc/yapnr/licenses/$(basename "${text}")"
    done
    for name in cpython openssl-3 sqlite tcl ncurses libedit libffi zlib; do
        notices="${notices} /usr/share/doc/yapnr/licenses/python-build-standalone/LICENSE.${name}.txt"
    done

fi
for notice in ${notices}; do
    if docker run --rm --entrypoint test "${IMAGE}" -s "${notice}"; then
        pass "${notice}"
    else
        fail "${notice} is missing or empty"
    fi
done

snapshot="$(docker run --rm --entrypoint cat "${IMAGE}" /etc/yapnr/ubuntu-snapshot 2>/dev/null || true)"
if echo "${snapshot}" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' \
    && docker run --rm --entrypoint grep "${IMAGE}" -q \
        "snapshot.ppa.launchpadcontent.net/kicad/kicad-10.0-releases/ubuntu/${snapshot}/" \
        /usr/share/doc/yapnr/SOURCES; then
    pass "Ubuntu archive snapshot ${snapshot}, in SOURCES"
else
    fail "no Ubuntu archive snapshot in /etc/yapnr/ubuntu-snapshot and SOURCES ('${snapshot}')"
fi

# The build leaves nothing behind in /tmp or in root's caches (KiCad's instance locks, uv).
if leftovers="$(docker run --rm --user 0 --entrypoint sh "${IMAGE}" -c '
    for d in /tmp /root/.cache /root/.local; do
        if [ -d "$d" ]; then find "$d" -mindepth 1 -maxdepth 1; fi
    done')"; then
    if [ -z "${leftovers}" ]; then
        pass "no build leftovers in /tmp, /root/.cache or /root/.local"
    else
        fail "build leftovers in the image: $(echo "${leftovers}" | tr '\n' ' ')"
    fi
else
    fail "cannot list /tmp and root's HOME in the image"
fi

cli="$(kicad_run "${IMAGE}" kicad-cli version 2>/dev/null | tail -n 1)"
case "${cli}" in
    "${KICAD_VERSION}" | "${KICAD_VERSION}"[-+~]*) pass "kicad-cli version ${cli}" ;;
    *) fail "kicad-cli version printed '${cli}', expected ${KICAD_VERSION}" ;;
esac

# The same KiCad checks as the image user, as another UID, and on a read-only root file system.
for mode in default uid readonly uid-readonly; do
    args=(-v "${HERE}:/smoke:ro")
    case "${mode}" in
        uid) args+=(--user 4242:4242) ;;
        readonly) args+=(--read-only --tmpfs /tmp) ;;
        uid-readonly) args+=(--user 4242:4242 --read-only --tmpfs /tmp) ;;
    esac
    if out="$(kicad_run "${args[@]}" "${IMAGE}" /usr/bin/python3 /smoke/pcbnew_smoke.py \
        "${KICAD_VERSION}" 2>&1)"; then
        pass "pcbnew + DRC (${mode}): $(echo "${out}" | tail -n 1)"
    else
        fail "pcbnew + DRC (${mode}):"
        echo "${out}" | sed 's/^/        /' >&2
    fi
done

if [ "${KICAD_ONLY}" = 0 ]; then
    lock="${ROOT}/docker/yapnr/runtime-${arch}.lock"
    numpy_pin="$(sed -nE 's/^numpy==([^ ]+).*/\1/p' "${lock}")"
    torch_pin="$(sed -nE 's/^torch==([^ ]+).*/\1/p' "${lock}")"

    printed="$(docker run --rm "${IMAGE}" --version)"
    if [ -z "${VERSION}" ] || [ "${printed}" = "yapnr ${VERSION}" ]; then
        pass "${printed}"
    else
        fail "--version printed '${printed}', expected 'yapnr ${VERSION}'"
    fi

    if docker run --rm "${IMAGE}" >/dev/null; then
        pass "default command (doctor)"
    else
        fail "the default command failed"
    fi

    for mode in default uid-readonly; do
        args=()
        [ "${mode}" = default ] || args=(--user 4242:4242 --read-only --tmpfs /tmp)
        if ! report="$(docker run --rm ${args[@]+"${args[@]}"} "${IMAGE}" doctor --json)"; then
            fail "doctor --json (${mode}) failed"
            continue
        fi
        if problems="$(
            EXPECT_NUMPY="${numpy_pin}" EXPECT_TORCH="${torch_pin}" EXPECT_VERSION="${VERSION}" \
                EXPECT_REVISION="${REVISION}" python3 -c '
import json, os, sys
r = json.load(sys.stdin)
e = os.environ
problems = []
if not r["python"].startswith("3.11."):
    problems.append("python " + r["python"])
if r["numpy"] != e["EXPECT_NUMPY"]:
    problems.append("numpy %s != %s" % (r["numpy"], e["EXPECT_NUMPY"]))
if r["torch"] != e["EXPECT_TORCH"]:
    problems.append("torch %s != %s" % (r["torch"], e["EXPECT_TORCH"]))
if e["EXPECT_VERSION"] and r["yapnr"] != e["EXPECT_VERSION"]:
    problems.append("yapnr %s != %s" % (r["yapnr"], e["EXPECT_VERSION"]))
if e["EXPECT_REVISION"] and r["source_revision"] != e["EXPECT_REVISION"]:
    problems.append("source_revision %s != %s" % (r["source_revision"], e["EXPECT_REVISION"]))
for tool, path in (("kicad_cli", "/usr/bin/kicad-cli"), ("kicad_python", "/usr/bin/python3")):
    if r[tool]["path"] != path or not r[tool]["executable"]:
        problems.append("%s: %s" % (tool, r[tool]))
print("; ".join(problems))
sys.exit(1 if problems else 0)
' <<<"${report}"
        )"; then
            pass "doctor --json (${mode}): python, numpy ${numpy_pin}, torch ${torch_pin}, KiCad paths"
        else
            fail "doctor --json (${mode}): ${problems}"
        fi
    done

    interop='import numpy, torch; t = torch.from_numpy(numpy.arange(4.0)); assert float(t.sum()) == 6.0'
    if docker run --rm --entrypoint /opt/venv/bin/python "${IMAGE}" -c "${interop}"; then
        pass "controller Python: torch <-> numpy"
    else
        fail "controller Python cannot convert between torch and numpy"
    fi
    if docker run --rm --entrypoint /opt/venv/bin/python "${IMAGE}" -c 'import pcbnew' 2>/dev/null; then
        fail "the controller Python imports pcbnew; KiCad-side code must use YAPNR_KICAD_PYTHON"
    else
        pass "controller Python does not see pcbnew"
    fi

    # yapnr.rf's native FDTD library from the wheel: loads, is the default backend, and steps
    # fields bit-identically to numpy (also as another UID on a read-only root file system).
    for mode in default uid-readonly; do
        args=(-v "${HERE}:/smoke:ro")
        if [ "${mode}" = uid-readonly ]; then
            args+=(--user 4242:4242 --read-only --tmpfs /tmp)
        fi
        if out="$(docker run --rm "${args[@]}" --entrypoint /opt/venv/bin/python "${IMAGE}" \
            /smoke/rf_native_smoke.py 2>&1)"; then
            pass "native FDTD (${mode}): $(echo "${out}" | tail -n 1)"
        else
            fail "native FDTD (${mode}):"
            echo "${out}" | sed 's/^/        /' >&2
        fi
    done
    # No checkout is mounted: run the installed RF workflow outside the package,
    # bounded independently of the image build job's timeout.
    if out="$(docker run --rm -v "${HERE}:/smoke:ro" --tmpfs /tmp \
        --entrypoint /usr/bin/timeout "${IMAGE}" 180 \
        /opt/venv/bin/python -I /smoke/rf_workflow_smoke.py 2>&1)"; then
        pass "external installed RF workflow: $(echo "${out}" | tail -n 1)"
    else
        fail "external installed RF workflow:"
        echo "${out}" | sed 's/^/        /' >&2
    fi
fi

if [ "${FAILURES}" -gt 0 ]; then
    echo "${IMAGE}: ${FAILURES} check(s) failed" >&2
    exit 1
fi
echo "${IMAGE}: all checks passed"

# Installed workflow and bundled agent: no authentication or paid model calls.
timeout 30 docker run --rm --read-only --tmpfs /tmp "${IMAGE}" agent instructions > /dev/null
timeout 30 docker run --rm --entrypoint codex "${IMAGE}" --version
timeout 30 docker run --rm --entrypoint rg "${IMAGE}" --version
timeout 30 docker run --rm "${IMAGE}" agent chat --dry-run
