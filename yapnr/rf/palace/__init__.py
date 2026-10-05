"""AWS Palace (3D finite elements) models: meshes and configurations from planar documents.

Palace (https://github.com/awslabs/palace, Apache-2.0) is the independent second solver of the
RF sign-offs, next to openEMS (FDTD). It runs as an external program in its own task image; this
package only writes its inputs:

- ``mesh``: the Gmsh OCC builder (msh 2.2, one physical group per material, conductor, port and
  wall) and the mesh record (``mesh.json``);
- ``config``: Driven and BoundaryMode configurations from a document and its mesh record;
- ``schema``: offline validation against Palace's own JSON schema (vendored, third_party/palace);
- ``validation``: the validation models of the Palace plan (lines, patch, TX feed);
- ``cli``: ``python -m yapnr.rf.palace``.

User guide: docs/rf-palace.md.
"""
