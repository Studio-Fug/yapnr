#!/usr/bin/env bash
# Build the part cache image and smoke-test it (docs/part-cache.md, "Running a public instance"):
# start it with a write and an admin token, upload a synthetic part through the HTTP client,
# read it back, materialize it (hash-checked), take it down, then remove the container, its
# volume and the image.
#
#   tools/image/smoke_part_cache.sh
#
# Needs Docker with BuildKit, Bazel (for the wheel) and python3 (3.9+) for the client.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
IMAGE="yapnr-part-cache:smoke"
NAME="yapnr-part-cache-smoke-$$"

work="$(mktemp -d)"
cleanup() {
    docker rm -f "${NAME}" >/dev/null 2>&1 || true
    docker volume rm "${NAME}" >/dev/null 2>&1 || true
    docker image rm "${IMAGE}" >/dev/null 2>&1 || true
    rm -rf "${work}"
}
trap cleanup EXIT

echo "==> building the wheel"
bazel build //release:wheel_for_test
mkdir -p "${work}/dist"
cp "$(bazel info bazel-bin)"/release/yapnr-*.whl "${work}/dist/"

echo "==> building ${IMAGE}"
docker buildx build --load --file docker/yapnr-part-cache/Dockerfile \
    --build-context dist="${work}/dist" --tag "${IMAGE}" .

export PYTHONPATH="${PWD}"
write_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
admin_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
hashes="$(python3 - "${write_token}" "${admin_token}" <<'PY'
import sys
from yapnr.partcache.server import hash_token
write, admin = sys.argv[1:]
print(f"{hash_token(write)} write smoke-writer\n{hash_token(admin)} admin smoke-admin")
PY
)"

echo "==> starting the container"
docker run --detach --name "${NAME}" --publish 127.0.0.1::8780 \
    --volume "${NAME}:/data" \
    --env YAPNR_PART_CACHE_TOKEN_HASHES="${hashes}" \
    "${IMAGE}" >/dev/null
port="$(docker port "${NAME}" 8780/tcp | head -n 1 | sed 's/.*://')"
url="http://127.0.0.1:${port}"
for _ in $(seq 1 60); do
    if python3 -c "import urllib.request; urllib.request.urlopen('${url}/v1/health', timeout=2)" \
        2>/dev/null; then
        break
    fi
    sleep 1
done

echo "==> exercising ${url}"
python3 - "${url}" "${write_token}" "${admin_token}" "${work}" <<'PY'
import json, sys, urllib.request
from pathlib import Path
from yapnr.frontends.atopile import testing
from yapnr.partcache.client import CacheError, HttpPartCache, materialize, read_part_dir

url, write, admin, work = sys.argv[1:]
part = testing.write_part(Path(work) / "parts")
request, blobs = read_part_dir(part, {"source": "smoke test"}, {"spdx": "CC0-1.0"})
anonymous = HttpPartCache(url, token="")
try:
    anonymous.upload_part(request, blobs)
    raise SystemExit("an anonymous upload was accepted")
except CacheError as err:
    assert "401" in str(err), err
manifest = HttpPartCache(url, token=write).upload_part(request, blobs)
assert anonymous.current(testing.SYNTHETIC_LCSC)["id"] == manifest["id"]
HttpPartCache(url, token=write).put_catalog(testing.catalog_entry(), {"source": "smoke test"})
components = json.load(urllib.request.urlopen(f"{url}/v0/component/lcsc/990000001"))["components"]
assert components[0]["part_number"] == "SR1K", components
target = materialize(anonymous, manifest["id"], Path(work) / "project")
assert (target / f"{testing.SYNTHETIC_PART}.ato").is_file()
with urllib.request.urlopen(f"{url}/v1/blobs/{manifest['files'][0]['sha256']}") as response:
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"] == "attachment"
HttpPartCache(url, token=admin).delete_part(manifest["id"], "smoke test takedown")
print("part cache smoke test passed:", manifest["id"][:12])
PY
docker exec "${NAME}" id -u | grep -qx 10001
echo "==> ok"
