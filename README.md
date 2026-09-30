<p align="center">
  <img src="branding/yapnr-logo-256.png" alt="yapnr" width="256">
</p>

# yapnr

**Yet another place and route** engine for printed circuit boards.

yapnr places components and routes copper for KiCad boards. It uses a mechanical,
Monte-Carlo-driven search: hierarchical block synthesis, power-first placement, native KiCad
routing and DRC in the loop, and electrical contracts (current, pairs, plane access) declared
next to the design.

The engine is being migrated here from the [Splanc](https://github.com/fughilli/splanc) repository,
where it was developed to lay out the Splanc Mini board. The migration plan and progress are in
`docs/`.

## License

Copyright (C) 2026 Kevin Balke. Licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`); see [LICENSE](LICENSE).
