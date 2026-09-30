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

## Referenced, not included

- **KiCad** (`kicad-cli`, `pcbnew`; GPL-3.0-or-later): used as an external tool through the
  discovered toolchain; not distributed.
- **KiCad footprint and 3D model libraries** (CC-BY-SA-4.0 with the KiCad library exception):
  referenced by board files. A future `yapnr export kicad --with-3d` copies the models a board uses
  into the exported project.
- **Python packages** (numpy, torch, PyYAML, Sphinx and its extensions, ...; various permissive
  licenses): installed from PyPI at build time as pinned in `requirements.lock`; not vendored.
- **Documentation site assets:** the furo theme's CSS and JavaScript (MIT) are part of the
  generated site; mermaid 11.4.1 (MIT) is loaded from a CDN.

## Project assets

The yapnr logo, mark and favicons in `branding/` are project assets created for yapnr (see
[docs/about-the-name.md](docs/about-the-name.md)); they are covered by the repository license.
