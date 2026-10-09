# Third-party material

yapnr is licensed under `AGPL-3.0-or-later` (see [LICENSE](LICENSE)). This file lists third-party
material that is, or will be, part of what yapnr ships or serves, with its license and how it is
brought in. Update it in the same change that adds, removes or upgrades such material.

## Shipped or served with yapnr

### rules_requirements

- **What:** the authoritative YAML model, validation, verification-set attribution,
  status rollups and trace graph renderer from Studio-Fug/rules_requirements,
  pinned to `aabecadd6e8710a11e99ccb366e63d858d92f6c6`.
- **License:** AGPL-3.0-or-later; its vendored YAML parser retains the upstream
  MIT license. The wheel includes both license texts.
- **How:** Bazel fetches the immutable source archive with a checked SHA-256
  (`@rules_requirements_toolkit`). The pure Python toolkit is included in the
  packaged wheel, so requirements review does not need a runtime download or
  build. The native traceability surface uses its entity payloads and SVG;
  pan/zoom interaction is adapted from the upstream graph view with attribution.

### elkjs

- **What:** [elkjs](https://github.com/kieler/elkjs) 0.9.3 (`lib/elk.bundled.js`), the graph
  layout engine behind the viewer's schematic view.
- **License:** EPL-2.0; the file includes a web-worker shim under Apache-2.0 (Google LLC), with
  its notice in the file.
- **How:** **never vendored.** Bazel fetches the npm tarball at build time, pinned by URL and
  sha256 (`MODULE.bazel`, repository `@elkjs`), and `//yapnr/viewer:dist` copies the file
  unmodified to `elk.bundled.js` in the served directory. The browser loads it as a separate file;
  it is never bundled or minified together with AGPL code. Its license texts are served next to
  it: the EPL-2.0 from the same tarball at `third_party/elkjs/LICENSE.md`, and the Apache-2.0
  (which the tarball lacks) at `third_party/elkjs/Apache-2.0.txt`. That text is the one file
  of elkjs's material kept in this repository: `third_party/licenses/Apache-2.0.txt`, byte for
  byte as published at <https://www.apache.org/licenses/LICENSE-2.0.txt> (sha256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`).

### three.js

- **What:** [three.js](https://threejs.org/) 0.186.1 (r186): `build/three.module.js`,
  `build/three.core.js` and the `OrbitControls`, `GLTFLoader`, `BufferGeometryUtils` and
  `SkeletonUtils` add-ons, for the viewer's 3D view.
- **License:** MIT.
- **How:** fetched like elkjs (repository `@threejs`, the npm tarball pinned by sha256) and copied
  unmodified to `vendor/three/` in the served directory, with its `LICENSE`. Not vendored: the
  build files are larger than the repository's 600 KB file limit.

`tests/unit/viewer/test_dist.py` checks the sha256 of every served third-party file.

### Shapely and GEOS

- **What:** Shapely 2.1.2 supplies polygonal offsets and exact-distance predicates for the
  opt-in energy-track proposal generator. Native KiCad DRC remains the acceptance authority.
- **License:** Shapely is BSD-3-Clause. Its binary wheels bundle GEOS 3.13.1 under LGPL-2.1;
  both notices remain in the installed `shapely-2.1.2.dist-info/licenses/` directory.
- **How:** pinned and hash-checked wheels in the Bazel/controller requirements and the separate
  Python 3.12 KiCad geometry locks. No GEOS source or binary is vendored in the repository.
  The libraries remain dynamically loaded and replaceable. Sources and license information:
  [Shapely 2.1.2](https://github.com/shapely/shapely/tree/2.1.2),
  [bundled GEOS version](https://shapely.readthedocs.io/en/2.1.2/release/2.x.html), and
  [GEOS source releases](https://libgeos.org/usage/download/).

### Google API discovery documents

- **What:** the Batch v1 discovery document (revision 20260723), Google's machine-readable
  description of the Batch REST API, as mirrored by
  [google-api-go-client](https://github.com/googleapis/google-api-go-client) at
  `batch/v1/batch-api.json` (commit `8140ddf12e1a5c4a54748a94b6655818fcc480ed`, 2026-08-02).
- **License:** BSD-3-Clause (the repository's `LICENSE`, kept next to it).
- **How:** vendored unmodified as `third_party/googleapis/batch-v1.json` (sha256
  `8ea2a112c32f1995d8b53660b4c729b62d5dc81ab79b096172a06093d2b89d15`), with
  `third_party/googleapis/LICENSE` (sha256
  `110244b02140866ee37d17fa7449436a377ec3b85a481fbb208f4c87964382de`). `yapnr exp` validates
  the Batch jobs it renders against it, offline; it is data of `//yapnr/exp`, not part of the
  wheel.

### Palace configuration schema

- **What:** the JSON schema of [AWS Palace](https://github.com/awslabs/palace)'s configuration
  files (`scripts/schema/config-schema.json`, schema version 2-1-0) at commit
  `b797ea8060a52241cd9ab1176199f06af8585816` (main, 2026-10-02), the commit the Palace task image
  builds. Palace validates every configuration against the same document at start-up.
- **License:** Apache-2.0 (Palace's `LICENSE` and `NOTICE`, kept next to it).
- **How:** vendored unmodified as `third_party/palace/config-schema.json` (sha256
  `8f8dbd5588fee4cef97c0a83459ba2b721eb371af4356abd970ac301df4e4b7c`), with
  `third_party/palace/LICENSE` (sha256
  `09e8a9bcec8067104652c168685ab0931e7868f9c8284b66f5ae6edae5f1130b`) and
  `third_party/palace/NOTICE` (sha256
  `d4290ed64c2edd0fce1d84e3f9dfb2881240fe534def76b8cd29ed6af683e287`). `yapnr.rf.palace`
  validates the configurations it writes against it, offline
  ([docs/rf-palace.md](docs/rf-palace.md)); it is data of `//yapnr/rf/palace`, not part of the
  wheel. Palace itself is never linked or imported: it runs as a separate program in its own
  task image.

## In the container images

The images `ghcr.io/studio-fug/yapnr-kicad` and `ghcr.io/studio-fug/yapnr`
([docs/containers.md](docs/containers.md)) redistribute the following, unmodified. Inside an image,
`/usr/share/doc/yapnr/SOURCES` says where the source of each part is and lists the exact package
versions, `/usr/share/doc/yapnr/licenses/` holds the license texts that are not elsewhere in the
image, and this file is at `/usr/share/doc/yapnr/THIRD_PARTY.md`.

- **Ubuntu 24.04** (the `ubuntu:24.04` image, pinned by digest, upgraded at build time, plus the
  packages KiCad depends on): various free-software licenses, with each package's license text in
  `/usr/share/doc/<package>/copyright` and the common ones in `/usr/share/common-licenses`. The
  archive state the packages come from is recorded at build time (`/etc/yapnr/ubuntu-snapshot`, a
  [snapshot.ubuntu.com](https://snapshot.ubuntu.com/) ID); their sources are on Launchpad
  (`https://launchpad.net/ubuntu/+source/<package>/<version>`) and in that snapshot.
- **KiCad** 10.0 (`kicad-cli`, `pcbnew` and the rest of the `kicad` package; GPL-3.0-or-later) and
  its **footprint, symbol and template libraries** (CC-BY-SA-4.0 with the KiCad library exception),
  from the KiCad team's PPA (`ppa:kicad/kicad-10.0-releases`). The PPA deletes superseded builds,
  so the complete source packages of every published KiCad build are kept as the image
  `ghcr.io/studio-fug/yapnr-kicad:<tag>-src` (linux/amd64 and linux/arm64), which is never deleted;
  `SOURCES` also points at Launchpad's PPA snapshot of the build date and at the source files on
  Launchpad. No 3D models are included.
- **tini** (MIT): the init process (PID 1), from Ubuntu.
- **CPython 3.11** (PSF-2.0) as built by
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (the release is
  pinned in `docker/yapnr/Dockerfile`), installed by uv under `/opt/python`. It bundles OpenSSL
  (Apache-2.0), SQLite (public domain), Tcl/Tk, ncurses, libedit, libffi, mpdecimal, expat, zlib,
  bzip2, xz (liblzma), libuuid, Berkeley DB and libX11/libxcb, each under its own license; the
  image build copies their license texts from that release's full archive (checksum-pinned) into
  `/usr/share/doc/yapnr/licenses/python-build-standalone/`. uv itself is used during the build only
  and is not in the image.
- **Python packages** from `docker/yapnr/runtime-<arch>.lock` (the runtime subset of
  `requirements.lock`: numpy, torch, PyYAML, Shapely and their dependencies) under `/opt/venv`,
  from PyPI
  and, for torch on linux/amd64, the PyTorch CPU index. Mostly BSD, MIT and Apache-2.0 licensed;
  each package's `.dist-info` directory holds its license files. The numpy, torch and Shapely
  wheels also
  bundle native libraries, listed per architecture in
  `docker/yapnr/native-libraries-<arch>.txt` (appended to `SOURCES`) with the exact source of each
  GCC runtime library, which the wheels' own notices do not give:

  - **OpenBLAS** (BSD-3-Clause): 0.3.23 in numpy, 0.3.25 in torch on linux/arm64;
  - **GCC runtime libraries**: `libgfortran` (GCC 8.3.1) and `libgomp` (GCC 4.8.5 on
    linux/amd64, a conda-forge GCC 13.2.0 build on linux/arm64), GPL-3.0-or-later with the GCC
    runtime library exception, and on linux/amd64 `libquadmath` (GCC 4.8.5, LGPL-2.1-or-later).
    Their GNU build IDs match CentOS 7 packages, whose source RPMs stay on vault.centos.org;
  - **Arm Compute Library** 23.08 (MIT) in torch on linux/arm64;
  - **Intel oneMKL**, linked statically into torch's `libtorch_cpu.so` on linux/amd64, under the
    Intel Simplified Software License.

  The license texts torch's own `LICENSE` lacks (OpenBLAS, Arm Compute Library, Intel's license)
  and the GCC runtime library exception are vendored byte for byte under
  `third_party/image-licenses/` and copied to `/usr/share/doc/yapnr/licenses/`; the GPL-3.0 and
  LGPL-2.1 texts are Ubuntu's, in `/usr/share/common-licenses`.

- **yapnr** itself (AGPL-3.0-or-later; `/usr/share/doc/yapnr/LICENSE`). The image records the
  commit it was built from (`YAPNR_SOURCE_REVISION` and the `org.opencontainers.image.revision`
  label), and every release attaches its source archive.

## Referenced, not included

- **KiCad** (`kicad-cli`, `pcbnew`; GPL-3.0-or-later): outside the container images, used as an
  external tool through the discovered toolchain; not distributed with the source or the wheel.
- **KiCad footprint and 3D model libraries** (CC-BY-SA-4.0 with the KiCad library exception):
  referenced by board files. A future `yapnr export kicad --with-3d` copies the models a board uses
  into the exported project.
- **Python packages** (numpy, torch, PyYAML, Sphinx and its extensions, ...): installed from PyPI
  at build time as pinned in `requirements.lock`; not vendored (the container images include the
  runtime subset, above). Their licenses are various
  open-source licenses, mostly BSD, MIT and Apache-2.0, plus a few others: certifi (MPL-2.0),
  typing_extensions (PSF-2.0), roman-numerals (0BSD or CC0-1.0) and docutils (public domain and
  BSD, with some files under other licenses). Each package's own metadata is authoritative.
- **Documentation site assets:** the furo theme's CSS and JavaScript (MIT) are part of the
  generated site; mermaid 11.4.1 (MIT) is loaded from a CDN.

- **atopile** 0.15.8 (MIT) and its dependencies, among them **atopile-easyeda2kicad**
  (AGPL-3.0): installed from PyPI by `yapnr atopile setup` into the user's cache, as pinned by the
  hashed locks in `yapnr/frontends/atopile/locks/`; not vendored and not in the images yet. The
  locks list the exact versions; each package's own metadata states its license.
- **Part data** (footprints, symbols, 3D models, catalog facts): never in the repository, the
  wheel or the images. It lives in a user's or a server's part cache, with provenance and licence
  metadata per part ([docs/part-cache.md](docs/part-cache.md)).

## In the repository only

- **Synthetic test parts** (`tests/fixtures/atopile/`, `yapnr/frontends/atopile/testing.py`):
  a footprint, symbol and atopile part drawn for yapnr's tests (not a real product), under the
  repository license.
- **IANA root zone list** (`tools/privacy/iana_tlds.txt`): a static copy of IANA's list of
  top-level domains (<https://data.iana.org/TLD/tlds-alpha-by-domain.txt>), public data, with its
  source, retrieval date and version in the file header. The privacy scan reads it; it is not part
  of the wheel or the container images.

## Project assets

The yapnr logo, mark and favicons in `branding/` are project assets created for yapnr (see
[docs/about-the-name.md](docs/about-the-name.md)); they are covered by the repository license.

### Interactive Codex terminal

The application image installs OpenAI Codex 0.161.0 native Linux binaries from
the official npm architecture archives, checked against immutable SHA-512 pins
in `docker/yapnr/codex.lock.json`. Source is
[97901140](https://github.com/openai/codex/tree/979011409de0a60b52f179721948e65531d26144).
Codex is Apache-2.0; its LICENSE/NOTICE and dependency notices are shipped in
`/usr/share/doc/yapnr/licenses/codex/`. The separate ripgrep and bubblewrap binaries
retain their upstream licenses and source links there. Optional voice and bundled
zsh runtimes are excluded. Claude Code is not redistributed. Credentials and
provider accounts are supplied by the operator and are never part of the image.

### Provider-neutral interactive terminal

OpenCode 1.18.35 is installed from official Linux release archives, checked against
SHA-256 pins in `docker/yapnr/opencode.lock.json`. Its source is
[53d1eabb](https://github.com/anomalyco/opencode/tree/53d1eabb61e21162157817bf677da0a4ad3332e3),
licensed under MIT; the license is shipped in `licenses/opencode/`. Provider
credentials are operator-supplied. Official Codex/Claude account authentication
uses the respective official CLI rather than OpenCode's provider adapters.

## Workbench fonts

The bundled Space Grotesk, IBM Plex Sans and IBM Plex Mono fonts are licensed
under the SIL Open Font License 1.1. Original license files are included in
[yapnr/brand/fonts](yapnr/brand/fonts). The application serves WOFF2 conversions
locally; it does not contact a font CDN.
