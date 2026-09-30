#!/bin/sh
# /usr/local/bin/yapnr in the yapnr image: the yapnr command line (the wheel's console script in
# /opt/venv), run through yapnr-kicad-env so that any UID gets a writable HOME with KiCad's
# library tables. /usr/local/bin is first on PATH, so `docker exec <container> yapnr ...` and
# `docker compose exec` take this path as well as the entrypoint does.
exec /usr/local/bin/yapnr-kicad-env /opt/venv/bin/yapnr "$@"
