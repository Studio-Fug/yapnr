# Third-party material

yapnr is licensed under `AGPL-3.0-or-later` (see [LICENSE](LICENSE)). This file lists third-party
material that is, or will be, part of what yapnr ships or serves, with its license and how it is
brought in. Update it in the same change that adds, removes or upgrades such material.

## Shipped or served with yapnr

### three.js (planned, PR4d)

- **What:** [three.js](https://threejs.org/) r180 and its `GLTFLoader`, for the viewer's 3D view.
- **License:** MIT.
- **How:** vendored under `third_party/three/` together with its license text.

### elkjs (planned, PR4b)

- **What:** [elkjs](https://github.com/kieler/elkjs) 0.9.3, the graph layout engine behind the
  viewer's schematic view.
- **License:** EPL-2.0; it includes a web-worker shim under Apache-2.0.
- **How:** **never vendored.** It is fetched at build time and pinned by sha256, then served to the
  browser as a separate, unmodified file, never bundled or minified together with AGPL code. Its
  license text is kept under `third_party/elkjs/`.

## In the container images

The images `ghcr.io/studio-fug/yapnr-kicad` and `ghcr.io/studio-fug/yapnr`
([docs/containers.md](docs/containers.md)) redistribute the following, unmodified. Inside an image,
`/usr/share/doc/yapnr/SOURCES` says where the source of each part is and lists the exact package
versions; this file is at `/usr/share/doc/yapnr/THIRD_PARTY.md`.

- **Ubuntu 24.04** (the `ubuntu:24.04` image, pinned by digest, plus the packages KiCad depends
  on): various free-software licenses, with each package's license text in
  `/usr/share/doc/<package>/copyright`. Their sources are available from the Ubuntu snapshot
  service at the time the base was built (image label `io.github.studio-fug.yapnr.ubuntu.snapshot`).
- **KiCad** 10.0 (`kicad-cli`, `pcbnew` and the rest of the `kicad` package; GPL-3.0-or-later) and
  its **footprint, symbol and template libraries** (CC-BY-SA-4.0 with the KiCad library exception),
  from the KiCad team's PPA (`ppa:kicad/kicad-10.0-releases`). The PPA deletes superseded builds,
  so the complete source packages of every published KiCad build are kept as the image
  `ghcr.io/studio-fug/yapnr-kicad:<tag>-src`, which is never deleted. No 3D models are included.
- **tini** (MIT): the init process (PID 1), from Ubuntu.
- **CPython 3.11** (PSF-2.0, with the licenses of the libraries it bundles) as built by
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone), installed by uv
  under `/opt/python`. uv itself is used during the build only and is not in the image.
- **Python packages** from `docker/yapnr/runtime-<arch>.lock` (the runtime subset of
  `requirements.lock`: numpy, torch, PyYAML and their dependencies) under `/opt/venv`, from PyPI
  and, for torch on linux/amd64, the PyTorch CPU index. Mostly BSD, MIT and Apache-2.0 licensed;
  the numpy and torch wheels bundle native libraries under their own licenses. Each package's
  `.dist-info` directory holds its license files and is authoritative.
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

## Project assets

The yapnr logo, mark and favicons in `branding/` are project assets created for yapnr (see
[docs/about-the-name.md](docs/about-the-name.md)); they are covered by the repository license.
