#!/bin/sh
# Entry point of the yapnr-part-cache image: serve /data on port 8780 of every interface
# (the container's network is the boundary; publish the port to a reverse proxy).
#
# Settings (environment):
#   YAPNR_PART_CACHE_TOKENS         token file (default /run/secrets/part-cache-tokens, if present);
#                                   without one the cache is read-only
#   YAPNR_PART_CACHE_TOKEN_HASHES   the token file's lines themselves ("<sha256> <scope> <label>",
#                                   one per line), for platforms that pass settings as variables;
#                                   they hold hashes, never the tokens
#   YAPNR_PART_CACHE_PRIVATE_READS  1: reads need a token with the read scope as well
#   YAPNR_PART_CACHE_MAX_FILE_MB    largest accepted file (default 64)
# Extra arguments are passed to `yapnr part-cache serve`.
set -eu

tokens="${YAPNR_PART_CACHE_TOKENS:-/run/secrets/part-cache-tokens}"
if [ -n "${YAPNR_PART_CACHE_TOKEN_HASHES:-}" ]; then
    tokens="$(mktemp)"
    printf '%s\n' "${YAPNR_PART_CACHE_TOKEN_HASHES}" >"${tokens}"
fi
set -- --root /data --create --host 0.0.0.0 --public --port 8780 \
    --max-file-mb "${YAPNR_PART_CACHE_MAX_FILE_MB:-64}" "$@"
if [ -f "${tokens}" ]; then
    set -- "$@" --tokens "${tokens}"
fi
if [ "${YAPNR_PART_CACHE_PRIVATE_READS:-0}" = 1 ]; then
    set -- "$@" --private-reads
fi
exec python3 -m yapnr part-cache serve "$@"
