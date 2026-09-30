#!/usr/bin/env bash
# Is an image reference published in its registry?
#
#   tools/image/published.sh REF
#
# Exit status: 0 published, 1 not published, 2 cannot tell (the error is printed). GHCR answers
# "denied" (or, without credentials, 403 Forbidden) rather than "not found" for a package that
# does not exist yet, and for a private one the caller cannot read; both count as not published.
# That is safe for the callers in .github/workflows/image.yaml: a pull request then builds the
# base itself, and the publishing jobs, which can read every package of the repository, never
# overwrite a published base (a create that is really denied fails on its own).
set -uo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: tools/image/published.sh REF" >&2
    exit 2
fi

if out="$(docker buildx imagetools inspect --raw "$1" 2>&1)"; then
    exit 0
fi
if echo "${out}" | grep -qiE 'not found|manifest unknown|name unknown|denied|unauthorized|403 forbidden'; then
    echo "$1 is not published: $(echo "${out}" | tail -n 1)" >&2
    exit 1
fi
echo "cannot tell whether $1 is published: ${out}" >&2
exit 2
