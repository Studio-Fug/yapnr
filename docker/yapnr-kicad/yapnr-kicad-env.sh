#!/bin/sh
# yapnr-kicad-env COMMAND [ARGS...]
#
# Run COMMAND with a writable HOME that holds KiCad's global library tables. Installed as
# /usr/local/bin/yapnr-kicad-env in the yapnr-kicad and yapnr images, where it is part of the
# entrypoint, so every UID works: `docker run --user "$(id -u):$(id -g)"`, `--read-only`, ...
#
# KiCad keeps its settings and the global library tables (fp-lib-table, sym-lib-table, ...) in
# $HOME/.config/kicad/<series>; kicad-cli and pcbnew find footprints and symbols through them.
# The image's own user (UID 1000) has them in its HOME already. Any other UID, or a read-only
# root file system, gets a private HOME under /tmp seeded from /etc/yapnr/kicad-seed.
set -eu

if [ "$#" -eq 0 ]; then
    echo "usage: yapnr-kicad-env COMMAND [ARGS...]" >&2
    exit 2
fi

if [ -z "${HOME:-}" ] || [ ! -d "${HOME}" ] || [ ! -w "${HOME}" ]; then
    HOME="${TMPDIR:-/tmp}/yapnr-home-$(id -u)"
    export HOME
    mkdir -p "${HOME}"
    chmod 0700 "${HOME}"
fi

# An arbitrary UID has no passwd entry; Python's getpass and others fall back to these.
if [ -z "${USER:-}" ]; then
    USER="$(id -un 2>/dev/null || echo "uid$(id -u)")"
    export USER
fi
if [ -z "${LOGNAME:-}" ]; then
    LOGNAME="${USER}"
    export LOGNAME
fi

config="${HOME}/.config/kicad/${YAPNR_KICAD_SERIES:?YAPNR_KICAD_SERIES is not set}"
for table in /etc/yapnr/kicad-seed/*-lib-table; do
    name="$(basename "${table}")"
    if [ ! -e "${config}/${name}" ]; then
        mkdir -p "${config}"
        cp "${table}" "${config}/${name}"
    fi
done

exec "$@"
