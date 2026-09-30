# Third-party material

yapnr is licensed under `AGPL-3.0-or-later` (see [LICENSE](LICENSE)). This file lists third-party
material that is, or will be, part of what yapnr ships or serves, with its license and how it is
brought in. Update it in the same change that adds, removes or upgrades such material.

## Shipped or served with yapnr

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
  `requirements.lock`: numpy, torch, PyYAML and their dependencies) under `/opt/venv`, from PyPI
  and, for torch on linux/amd64, the PyTorch CPU index. Mostly BSD, MIT and Apache-2.0 licensed;
  each package's `.dist-info` directory holds its license files. The numpy and torch wheels also
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

## In the repository only

- **IANA root zone list** (`tools/privacy/iana_tlds.txt`): a static copy of IANA's list of
  top-level domains (<https://data.iana.org/TLD/tlds-alpha-by-domain.txt>), public data, with its
  source, retrieval date and version in the file header. The privacy scan reads it; it is not part
  of the wheel or the container images.

## Project assets

The yapnr logo, mark and favicons in `branding/` are project assets created for yapnr (see
[docs/about-the-name.md](docs/about-the-name.md)); they are covered by the repository license.
