# Container images

People who only want to run yapnr need Docker and nothing else: the image bundles KiCad 10
(`kicad-cli` and the `pcbnew` Python module), CPython 3.11 with numpy and CPU-only torch, and yapnr
itself. Bazel is only for contributors (see [DEVELOPERS.md](../DEVELOPERS.md)).

| Image                            | What                                                           |
| -------------------------------- | -------------------------------------------------------------- |
| `ghcr.io/studio-fug/yapnr`       | the yapnr command line, on top of the KiCad base               |
| `ghcr.io/studio-fug/yapnr-kicad` | Ubuntu 24.04 with KiCad 10 from the KiCad team's PPA, headless |

Both are built natively for **linux/amd64** and **linux/arm64** (Apple Silicon runs the arm64
image without emulation), run as an unprivileged user (UID 1000), and carry an SBOM, build
provenance and a GitHub attestation.

> **Status:** the images are built and smoke-tested on every pull request that changes what goes
> into them, and `edge` is published from `main` whenever a merge changes it. The engine commands
> (`init`, `import`, `run`, `export`, the viewer) arrive with the migration pull requests; until
> then the image runs `yapnr --version` and `yapnr doctor`. The examples below that need those
> commands are marked as planned.

## Tags

`ghcr.io/studio-fug/yapnr`:

| Tag          | Meaning                                                                          |
| ------------ | -------------------------------------------------------------------------------- |
| `X.Y.Z`      | a release; never changes                                                         |
| `X.Y`        | the newest patch release of `X.Y`: **pin this** (or a digest)                    |
| `latest`     | the newest stable release                                                        |
| `X.Y.Z-rc.N` | a release candidate                                                              |
| `edge`       | the newest image build of `main` (versions below)                                |
| `sha-<7>`    | the image of one `main` commit that changed the image (see below)                |
| `X`          | from 1.0 on: the newest release of major version `X` (never published while 0.x) |

`edge` and `sha-<7>` are published only for the `main` commits that change what goes into an image
(the code, the Dockerfiles, the locks, the notices); a documentation-only merge publishes nothing,
so `edge` stays on the last image build. Their version is `X.Y.(Z+1).devN+g<sha>` after the
release `vX.Y.Z`, `X.Y.Zrc(K+1).devN+g<sha>` after the release candidate `vX.Y.Z-rc.K`, and
`0.0.0.devN+g<sha>` before the first release.

Pin `X.Y`, or a digest (`ghcr.io/studio-fug/yapnr@sha256:...`) for reproducible runs. Results can
change between minor versions (see [releases](releases.md)). They also differ between platforms
(operating system and architecture, for example the arm64 image and a Mac): the same inputs and
seed give a different board, of the same quality on average, because the torch builds round a few
float operations differently ([decisions](decisions.md)). Compare runs on one platform. The KiCad
version is part of the image, not of the tag: a KiCad patch update ships as a yapnr patch release.
The image label `io.github.studio-fug.yapnr.kicad.version` and the release notes name it.

`ghcr.io/studio-fug/yapnr-kicad` is tagged `<KiCad version>-<N>` (for example `10.0.6-1`; `N`
counts rebuilds of one KiCad version, and such a tag is never overwritten), plus the moving
`10.0.6` and `10.0`. `10.0.6-1-src` holds the source packages of that KiCad build (see
[Licenses and source](#licenses-and-source)). The base is rebuilt under a new `N` for Ubuntu
security updates (see [releases](releases.md#maintaining-the-images)).

## Quick start

```sh
IMG=ghcr.io/studio-fug/yapnr:edge          # a release tag such as 0.1 once one exists

docker run --rm "$IMG" doctor              # the toolchain report (the default command)
docker run --rm "$IMG" --version
```

`doctor` prints the yapnr version and the source revision the image was built from, the Python,
numpy and torch versions, and the KiCad CLI and KiCad Python the image is configured with.

### Working on a project

The project directory is bind-mounted at `/project`, the image's working directory. On Linux,
run as your own user so that new files belong to you:

```sh
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD":/project "$IMG" doctor
```

Planned (the commands arrive with PR3d to PR3f; the store defaults to `/project/.yapnr`):

```sh
U="$(id -u):$(id -g)"
docker run --rm -u "$U" -v "$PWD":/project "$IMG" \
  init --frontend kicad --from-kicad design/board.kicad_pro
docker run --rm -u "$U" -v "$PWD":/project "$IMG" import kicad design/board.kicad_pro

# A long run in its own container: detached by Docker, in the foreground inside it.
docker run -d --name yapnr-run -u "$U" -v "$PWD":/project \
  --cpus 6 --memory 12g --shm-size 1g --pids-limit 4096 -e OMP_NUM_THREADS=6 \
  --stop-timeout 120 "$IMG" run experiments/default.toml
docker logs -f yapnr-run
docker exec yapnr-run yapnr stop <run-id>     # in the container that owns the run

# The best candidate as a standalone KiCad project, with its DRC report.
docker run --rm -u "$U" -v "$PWD":/project "$IMG" export kicad <run-id>:best -o out/board
```

`yapnr stop` identifies processes by PID and start time, so it must run in the PID namespace of the
run: use `docker exec` into the container that owns it.

### The viewer (planned, PR4a)

The viewer serves the project store over HTTP on port 8781. It has no authentication: publish it on
the loopback interface only, and use an SSH tunnel (`ssh -L 8781:127.0.0.1:8781 <host>`) or an
authenticating reverse proxy for remote access. Never publish the port on all host interfaces.

```sh
docker run -d --name yapnr-viewer -u "$U" -v "$PWD":/project \
  -p 127.0.0.1:8781:8781 "$IMG" viewer --listen 0.0.0.0 --port 8781
docker exec yapnr-viewer yapnr run --detach experiments/default.toml   # controls see this run
```

### Docker Compose

A `compose.yaml` next to the project (planned services marked; `doctor` works today):

```yaml
# YAPNR_UID=$(id -u) YAPNR_GID=$(id -g) docker compose run --rm doctor
# YAPNR_UID=$(id -u) YAPNR_GID=$(id -g) docker compose up -d viewer      (planned, PR4a)
# docker compose exec viewer yapnr run --detach experiments/default.toml
name: yapnr
x-yapnr: &yapnr
  image: ghcr.io/studio-fug/yapnr:${YAPNR_TAG:-edge}
  user: '${YAPNR_UID:-1000}:${YAPNR_GID:-1000}'
  volumes: ['./:/project']
services:
  doctor:
    <<: *yapnr
    profiles: [tools]
    command: [doctor]
  viewer:
    <<: *yapnr
    command: [viewer, --listen, 0.0.0.0, --port, '8781']
    ports: ['127.0.0.1:8781:8781']
    shm_size: 1g
    stop_grace_period: 2m
    restart: unless-stopped
    healthcheck:
      test:
        [
          'CMD',
          'python',
          '-c',
          "import urllib.request as u; u.urlopen('http://127.0.0.1:8781/', timeout=3)",
        ]
      interval: 30s
  batch:
    <<: *yapnr
    profiles: [batch]
    command: [run, experiments/default.toml]
    shm_size: 1g
    cpus: 6
    mem_limit: 12g
    pids_limit: 4096
    environment: { OMP_NUM_THREADS: '6' }
    stop_grace_period: 2m
```

Compose cannot see the shell's `UID` and `GID` (they are not exported, and `UID` is read-only in
bash), hence `YAPNR_UID` and `YAPNR_GID`; a `.env` file next to `compose.yaml` works as well. The
image defines no `HEALTHCHECK`, because it would mark every `run` or `doctor` container unhealthy;
the viewer service brings its own.

## Users, file ownership and permissions

- The image runs as `yapnr`, UID/GID 1000, with `HOME=/var/lib/yapnr`. Nothing needs root, and
  there is no `sudo`.
- **Any UID works.** The entrypoint goes through `yapnr-kicad-env`, which gives a UID without a
  writable `HOME` a private one under `/tmp` and seeds KiCad's global library tables there. The
  same holds for `docker exec <container> yapnr ...`: `/usr/local/bin/yapnr` is the wrapper.
- **Linux hosts:** pass `-u "$(id -u):$(id -g)"` so that files written to the project belong to
  you rather than to UID 1000.
- **macOS:** Docker Desktop and OrbStack map ownership through their file sharing: files the
  container writes belong to your macOS user, and `-u` is not needed. The same holds for colima
  with virtiofs mounts (`colima start --vm-type vz --mount-type virtiofs`; checked with colima on
  2026-09-29: containers running as UID 1000 and 4242 both wrote to a project owned by the macOS
  user, and the files belonged to that user). colima's sshfs and 9p mounts behave differently:
  there, write a test file first, and pass `-u "$(id -u):$(id -g)"` if it fails or lands with the
  wrong owner.
- **Rootless Podman:** `--userns=keep-id`. **SELinux hosts:** add `:Z` to the bind mount
  (`-v "$PWD":/project:Z`).
- **Read-only root file system:** supported: `--read-only --tmpfs /tmp`.

## Resources

- **Shared memory.** torch and the worker pools use `/dev/shm`, and Docker's default is 64 MB:
  pass `--shm-size 1g` for real runs.
- **CPU and memory.** Limit a run with `--cpus` and `--memory`, and set `OMP_NUM_THREADS` to the
  same CPU count so torch does not oversubscribe. `--pids-limit 4096` bounds the worker pools.
- **Stopping.** `tini` is PID 1 and forwards signals, so `docker stop` asks yapnr to stop
  gracefully. Give it time: `--stop-timeout` (or compose's `stop_grace_period`) at least as long
  as the longest worker timeout.
- **Store size.** A project store can grow large. On macOS, bind mounts are slower than Docker
  volumes: for heavy runs, set `YAPNR_STORE=/store` with a named volume mounted there, and move
  results in and out with `yapnr pack` and `yapnr unpack` (planned).
- **Nothing GUI-bound.** There is no X or Wayland session and `DISPLAY` is never set; `kicad-cli`
  and `pcbnew` run headless.

## Apple Silicon

- Use the native **arm64** image (the default on an Apple Silicon host). It runs under Docker
  Desktop, colima, OrbStack or Podman without emulation.
- An amd64-only image, such as the official `kicad/kicad` image (its `9.0` and `10.0` tags are
  amd64-only, checked 2026-09-29; the planned CI KiCad lane of PR6a runs it), needs emulation:
  enable Rosetta (Docker Desktop: on by default from macOS 14.1; colima:
  `colima start --vm-type vz --vz-rosetta`). Rosetta costs about 20%; QEMU without it was measured
  six to eight times slower on KiCad work.
- Docker Desktop and colima run a VM with a fixed memory size: give it more than the run's
  `--memory` plus the viewer. The Mac itself must stay awake during long runs (for example
  `caffeinate -i` around the session); keep-awake inside the container does nothing.
- Windows: Docker Desktop with WSL2 runs the linux/amd64 image. There is no native Windows support.

## What is inside

| Path                     | What                                                                                                                                                                                                      |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/usr/local/bin/yapnr`   | the command line (the entrypoint, through `yapnr-kicad-env`)                                                                                                                                              |
| `/opt/venv`              | the controller: CPython 3.11 with the runtime lock (numpy, torch, ...) and the yapnr wheel, whose `yapnr/rf/libyapnr_fdtd.so` is the RF solver's native kernel ([solver backends](rf-solver-backends.md)) |
| `/opt/python`            | that CPython, from python-build-standalone (installed by uv)                                                                                                                                              |
| `/usr/bin/kicad-cli`     | KiCad's command line                                                                                                                                                                                      |
| `/usr/bin/python3`       | KiCad's Python 3.12 with `pcbnew`, for KiCad-side workers                                                                                                                                                 |
| `/usr/share/kicad/`      | footprints, symbols and templates (no 3D models)                                                                                                                                                          |
| `/etc/yapnr/kicad-seed/` | the global library tables every user starts from                                                                                                                                                          |
| `/usr/share/doc/yapnr/`  | `LICENSE`, `THIRD_PARTY.md`, `SOURCES` and `licenses/`                                                                                                                                                    |
| `/project`               | the working directory; mount the project here                                                                                                                                                             |

The controller Python and KiCad's Python are separate on purpose: the controller runs Bazel's
exact interpreter and wheel versions (the runtime locks are pinned to `requirements.lock`), and
KiCad-side code runs under KiCad's own Python without torch. (KiCad's Python cannot import
`yapnr` from `/opt/venv` yet; the KiCad-side workers of PR6a get their own import path, with only
yapnr's pure-Python modules on it, and a smoke check.) `/opt/venv/bin` comes before
`/usr/bin` on `PATH`, so a plain `python3` in the image is the controller; yapnr finds KiCad through
these environment variables, which the image sets:

| Variable                                           | Value                                        |
| -------------------------------------------------- | -------------------------------------------- |
| `YAPNR_KICAD_CLI`                                  | `/usr/bin/kicad-cli`                         |
| `YAPNR_KICAD_PYTHON`                               | `/usr/bin/python3`                           |
| `YAPNR_SOURCE_REVISION`                            | the yapnr commit the image was built from    |
| `KICAD10_FOOTPRINT_DIR`, `KICAD10_SYMBOL_DIR`, ... | the stock libraries under `/usr/share/kicad` |

**3D models** are not included (`kicad-packages3d` is 3.3 GB installed). For
`yapnr export kicad --with-3d` or GLB export, mount a host 3D library read-only:
`-v /path/to/3dmodels:/usr/share/kicad/3dmodels:ro`.

Sizes (linux/arm64): the KiCad base is about 1.6 GB unpacked, a 390 MB download; the yapnr image
adds about 0.67 GB (CPython 0.12 GB, the runtime 0.55 GB), 2.3 GB unpacked and a 570 MB download in
all. On linux/amd64 the CPU torch build is larger.

## Verifying an image

```sh
gh attestation verify oci://ghcr.io/studio-fug/yapnr:0.1.0 -R Studio-Fug/yapnr
docker buildx imagetools inspect ghcr.io/studio-fug/yapnr:0.1.0 --format '{{json .SBOM}}'
docker buildx imagetools inspect ghcr.io/studio-fug/yapnr:0.1.0 --format '{{json .Provenance}}'
```

## Licenses and source

yapnr is `AGPL-3.0-or-later`; the image records its exact source (`YAPNR_SOURCE_REVISION`, the
`org.opencontainers.image.revision` label), and release source archives are attached to each GitHub
release. KiCad is `GPL-3.0-or-later` and its libraries are `CC-BY-SA-4.0` with the KiCad library
exception; the image label `org.opencontainers.image.licenses` names the three.
[THIRD_PARTY.md](../THIRD_PARTY.md) lists everything the images redistribute, including the native
libraries inside the numpy and torch wheels and the libraries in the bundled CPython;
`/usr/share/doc/yapnr/SOURCES` in the image says where each part's source is,
`/usr/share/doc/yapnr/licenses/` holds the license texts that are not elsewhere in the image, and
`/usr/share/doc/<package>/copyright` holds the Ubuntu packages' license texts.

The KiCad team's PPA deletes superseded builds, so pointing at it would not keep the corresponding
source available. Every published base tag therefore has a companion image with the complete source
packages of its KiCad build, for linux/amd64 and linux/arm64, which is never deleted. CI publishes
it before the base tag, and a later run publishes it if it is ever missing:

```sh
docker create --name kicad-src ghcr.io/studio-fug/yapnr-kicad:10.0.6-1-src none
docker cp kicad-src:/src ./kicad-src && docker rm kicad-src
```

`SOURCES` also names Launchpad's snapshot of the PPA at the build time and the source files on
Launchpad. The Ubuntu packages come from the archive state recorded at build time in
`/etc/yapnr/ubuntu-snapshot` (a [snapshot.ubuntu.com](https://snapshot.ubuntu.com/) ID, taken right
after `apt-get update`, with every package upgraded to that state); `SOURCES` lists the exact source
package versions, each of which Launchpad keeps at
`https://launchpad.net/ubuntu/+source/<package>/<version>`:

```sh
docker run --rm --entrypoint cat ghcr.io/studio-fug/yapnr-kicad:10.0.6-1 /usr/share/doc/yapnr/SOURCES
```

## Building the images locally

Contributors with Docker and Bazel can build both images for their own architecture, load them into
Docker and smoke-test them:

```sh
tools/image/build_local.sh                         # yapnr-kicad:local, then yapnr:local
tools/image/build_local.sh --base ghcr.io/studio-fug/yapnr-kicad:10.0.6-1   # reuse the base
```

Each image is a few GB; remove them afterwards (`docker image rm yapnr:local yapnr-kicad:local`).
The pieces:

- `docker/yapnr-kicad/Dockerfile`: the base. Its tag is `docker/yapnr-kicad/TAG`; any change to the
  directory needs a new tag. CI refuses to overwrite a published one: every run compares
  `tools/image/base_context.sh` with the published base's `io.github.studio-fug.yapnr.kicad.context`
  label.
- `docker/yapnr/Dockerfile`: the application image. The yapnr wheel comes from Bazel
  (`bazel build //release:wheel.dist --stamp --embed_label=<version>`) through the named build
  context `dist`.
- `docker/yapnr/runtime-{arm64,amd64}.lock`: the hashed runtime locks, regenerated with
  `tools/image/update_runtime_locks.sh` after a runtime pin changes in `requirements.lock`
  (`tests/unit/repo/test_images.py` checks they agree).
- `tools/image/smoke_image.sh`: the smoke test CI runs on both architectures: `doctor --json`
  (Python 3.11, the numpy and torch pins, KiCad configured, the source revision),
  `kicad-cli version`, and `pcbnew` loading a footprint through the seeded library table, saving a
  board and running `kicad-cli pcb drc` on it; as UID 1000, as an arbitrary UID, and with a
  read-only root file system; and the notices, license texts and recorded Ubuntu snapshot.

CI: `.github/workflows/image.yaml` (see [releases](releases.md#what-the-workflows-do)).
