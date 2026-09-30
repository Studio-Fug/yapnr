"""The part cache: derived part data kept outside every repository (docs/part-cache.md).

yapnr commits no third-party part data. Parts (atopile part directories: ``.ato``, footprint,
symbol, 3D model) and catalog entries (the facts the picker chooses from) live in a cache that a
user runs locally or that a server shares:

- ``model``: records, content ids, validation;
- ``store``: the on-disk layout (content-addressed blobs, manifests, catalog, takedowns);
- ``server``: the HTTP service, with token-protected writes and takedowns;
- ``client``: local and HTTP clients, and verified materialization into a project;
- ``importer``: filling a cache from existing part directories and catalogs.

Everything here is stdlib only, so the server runs in a minimal container.
"""
