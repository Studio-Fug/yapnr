# The part cache

yapnr commits no third-party part data. The atopile parts a board uses (footprints, symbols, 3D
models) and the facts its part picker chooses from live in a **part cache**: a directory on the
user's machine, or a server that several machines share. A project pins the parts it uses by
content id in a parts lock, and every build writes them into its work copy after checking each
file ([the atopile toolchain](frontends/atopile.md#parts)).

```sh
yapnr part-cache init                                   # ~/.local/share/yapnr/part-cache
yapnr part-cache import-parts --imported-from "board X" path/to/elec/src/parts
yapnr part-cache find -q TPS552882
yapnr part-cache show C15127                            # newest part for an LCSC id
yapnr part-cache materialize C15127 --into elec/src/parts
yapnr part-cache serve --root ~/.local/share/yapnr/part-cache     # http://127.0.0.1:8780
```

The location is `--cache` (a directory or an `http(s)://` URL), else `$YAPNR_PART_CACHE`, else
`$XDG_DATA_HOME/yapnr/part-cache` (`~/.local/share/yapnr/part-cache`). A server token comes from
`$YAPNR_PART_CACHE_TOKEN` and is never printed. Keep a cache directory outside every repository.

## What is stored

**Parts.** A part is one atopile part directory: the `.ato` file with its atomic-part trait, the
footprint (`.kicad_mod`), the symbol (`.kicad_sym`), 3D models (`.step`, `.stp`, `.wrl`) and notes
(`.md`, `.txt`). No other file types, no subdirectories, at most 32 files, each at most 64 MB (the
server's `--max-file-mb`). Each file is stored once, named by its sha256; the part's **id** is
the sha256 of its name and its sorted list of (file name, sha256, size). The same files always
give the same id, and any change gives a new one; older versions stay available by id.

The manifest of a part also records what the `.ato` file says about it (the LCSC id, manufacturer
and part number) and two required fields that are not part of the id:

- `provenance`: `source` (required; `easyeda:C15127` when atopile's `is_auto_generated` trait says
  so, `self`, `kicad-library`, ...), and optionally `generator`, `created`, `imported_from` and
  `notes`;
- `licence`: `spdx` (required: an SPDX identifier, or `NOASSERTION` when unknown), `notes` and
  `terms_url`.

The uploader and time are recorded by the cache (`uploaded`: the token's label, never a person's
details). A part whose `.ato` names a 3D model that is not uploaded is kept with a `warnings`
entry; a missing footprint or symbol is refused.

**Catalog entries.** The facts the picker needs about one LCSC id, in the
[`yapnr-picker-catalog-v1`](frontends/atopile.md#the-offline-picker) part format, each with its
own provenance. A newer upload replaces an entry. `yapnr part-cache catalog` exports them as a
catalog file.

**Not stored:** atopile's raw EasyEDA cache (`build/cache/parts/easyeda/`: it also holds the
uploader's personal data), datasheets, or anything obtained through a vendor API whose terms
forbid redistribution. A repository check (`tools/check_part_data.py`) keeps generated part data
out of yapnr itself.

## Layout on disk

```text
<root>/yapnr-part-cache.json        {"schema": "yapnr-part-cache-v1"}
<root>/blobs/sha256/ab/<sha256>     file contents, immutable
<root>/parts/ab/<id>.json           part manifests, immutable
<root>/catalog/C<digits>.json       the current catalog entry of each LCSC id
<root>/takedowns.jsonl              append-only log of deleted parts and entries
<root>/index.sqlite                 an index, rebuilt from the files by `reindex`
<root>/tmp/                         staging for atomic writes
```

The files are the truth; the index can always be rebuilt. Writes go through a temporary file and
an atomic rename. `yapnr part-cache verify` re-hashes every file and re-checks every manifest;
`gc` removes files that no part uses.

## The HTTP API

`yapnr part-cache serve` binds `127.0.0.1:8780` unless `--public` allows another address.

| Request                                                                         | Scope | Answer                                                                                 |
| ------------------------------------------------------------------------------- | ----- | -------------------------------------------------------------------------------------- |
| `GET /v1/health`                                                                | none  | status and counts (always public)                                                      |
| `GET /v1/parts?lcsc=&manufacturer=&mpn=&name=&q=&all_versions=1&limit=&offset=` | read  | part summaries, newest first                                                           |
| `GET /v1/parts/<id>`                                                            | read  | the manifest                                                                           |
| `GET /v1/lcsc/<C123>`                                                           | read  | the newest manifest for an LCSC id                                                     |
| `GET /v1/blobs/<sha256>`                                                        | read  | the file (`application/octet-stream`, `attachment`, `nosniff`, immutable)              |
| `GET /v1/catalog[?lcsc=C1,C2]`                                                  | read  | a catalog document                                                                     |
| `GET /v1/catalog/<C123>`                                                        | read  | one catalog entry with its provenance                                                  |
| `GET /v0/...`, `POST /v0/query...`                                              | read  | atopile's components API, from the catalog                                             |
| `PUT /v1/blobs/<sha256>`                                                        | write | upload a file; the body must hash to the name                                          |
| `POST /v1/parts`                                                                | write | `{name, files: [{path, sha256, size}], provenance, licence}`; 201, or 200 if it exists |
| `PUT /v1/catalog/<C123>`                                                        | write | `{part, provenance}`                                                                   |
| `DELETE /v1/parts/<id>`                                                         | admin | `{"reason": "..."}`: a takedown                                                        |
| `DELETE /v1/catalog/<C123>`                                                     | admin | `{"reason": "..."}`                                                                    |

Reads are public unless the server runs with `--private-reads`. Writes need
`Authorization: Bearer <token>` with the `write` scope; takedowns the `admin` scope. Errors are
JSON `{"detail": "..."}` with 400, 401, 403, 404, 410 (taken down) or 413.

Tokens: `yapnr part-cache token --scope write --label ci` prints a new token (give it to the
client as `YAPNR_PART_CACHE_TOKEN`) and the line for the server's token file,
`<sha256 of the token> <scope> <label>`. The server stores only hashes and compares them in
constant time.

A client never trusts a server: `materialize` checks every file against the manifest's sha256 and
size and the manifest's id against its files, and writes nothing on a mismatch. Downloaded files
are kept in a local content-addressed directory (`$XDG_CACHE_HOME/yapnr/part-cache-blobs`).

## Running a public instance

The image `docker/yapnr-part-cache` holds Ubuntu 24.04's Python and yapnr's stdlib-only cache and
picker modules (no KiCad, no atopile, no numerical stack). It runs as an unprivileged user and
keeps everything in the volume `/data`:

```sh
bazel build //release:wheel_for_test           # or the stamped //release:wheel.dist
docker buildx build -f docker/yapnr-part-cache/Dockerfile \
    --build-context dist=<dir with the yapnr wheel> -t yapnr-part-cache:local .
docker run -d --name part-cache -p 127.0.0.1:8780:8780 -v part-cache-data:/data \
    -v "$PWD/tokens:/run/secrets/part-cache-tokens:ro" yapnr-part-cache:local
tools/image/smoke_part_cache.sh                # build, start, upload, read, take down, remove
```

Settings: `YAPNR_PART_CACHE_TOKENS` (the token file; default
`/run/secrets/part-cache-tokens`; without one the instance is read-only),
`YAPNR_PART_CACHE_TOKEN_HASHES` (the file's lines, for platforms that pass settings as
variables), `YAPNR_PART_CACHE_PRIVATE_READS=1`, `YAPNR_PART_CACHE_MAX_FILE_MB`.

The server speaks plain HTTP with a thread per request. For a public instance:

- put a TLS-terminating reverse proxy in front (Caddy, nginx), with request-rate and connection
  limits and a body limit matching `--max-file-mb`;
- give each uploader their own `write` token, and keep `admin` tokens for takedowns;
- back up `/data` (the files are the truth; `index.sqlite` is rebuilt by `reindex`);
- publish terms and a contact for takedown requests. Stored files are user-generated content and
  are always served as attachments, never rendered.

**Takedowns.** `yapnr part-cache delete <id> --reason "..."` (with an `admin` token against a
server, or directly on the directory) removes the manifest and every file no other part uses,
appends the reason to `takedowns.jsonl`, and refuses the same id from then on (410). Catalog
entries are taken down the same way over HTTP.

## Importing parts

A project that commits atopile part directories can move them into a cache and keep only a lock:

```sh
yapnr part-cache import-parts --imported-from "project@<commit>:elec/src/parts" elec/src/parts
yapnr atopile lock-parts . --cache ~/.local/share/yapnr/part-cache     # writes yapnr-parts.lock.json
yapnr part-cache import-catalog --splanc tools/picker_catalog.json     # a Splanc-layout catalog
```

Provenance comes from the files: a part with atopile's `is_auto_generated` trait keeps its source
(`easyeda:C<id>`) and date; one without it is recorded as hand-authored or hand-edited. The
licence is recorded as `NOASSERTION`, with a note that points at the source's terms for generated
parts; `--licence-note` adds to it. Import refuses parts it cannot check (no atomic-part trait, a
missing footprint or symbol) and imports the rest.

Splanc's boards were moved this way into a local cache on the development machine: 276 part
directories of four boards (`splanc`, `splanc_dev`, `splanc_max`, `splanc_eol_tester`), 248
distinct versions of 163 parts, 90 MB, and the 103 entries of its picker catalog. The locks
written for its boards materialize every file byte-identically, and Splanc Mini builds from the
locked parts with the same input id as from its committed parts.
