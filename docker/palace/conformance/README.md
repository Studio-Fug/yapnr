# ParMETIS_V3_NodeND conformance test

`nodend_check.c` checks a library's `ParMETIS_V3_NodeND` against the ParMETIS 4.0 manual
(section 4.2.4, the format of the ordering and separator-sizes arrays, and section 5.4, the
routine's parameters). The Palace image (`../Dockerfile`, stage `solvers`) runs it against Scotch's
ParMETIS-compatible library, which SuperLU_DIST calls for its parallel ordering
(`ColumnOrdering: "ParMETIS"`): SuperLU_DIST's parallel symbolic factorization reads the separator
tree from `sizes`, so the tree has to be complete and in the manual's layout. It was written from
the manual alone, without ParMETIS's source; it needs only the header and the library under test
and MPI, so it also checks any other implementation of the routine.

```sh
sh run.sh /opt/palace                       # parmetis.h and Scotch's libraries under PREFIX
sh run.sh PREFIX LIB...                     # another implementation: its libraries in link order
mpirun -np 8 nodend_check 40 30 20 [--numflag 1] [--skew] [--order-only]
```

The graph is the 7-point grid `NX x NY x NZ`, in contiguous blocks over the ranks (`--skew`: rank
0 holds three times the share of the others). For a power-of-two number of ranks P it checks, for
two calls:

- the return value is `METIS_OK`;
- `order` is a permutation of the vertices, numbered from `numflag`;
- `sizes` is the same on every rank: the P sub-domain sizes, then the P-1 separator sizes, level
  by level from the bottom, left to right, the top separator last; every entry is positive and
  they add up to the number of vertices;
- the new numbering is the postorder of that tree (left subtree, right subtree, then the
  separator, each a contiguous range), and the separators separate: no edge of the grid joins two
  vertices whose tree nodes are not on one path to the root.

It prints a checksum of the first call's `order` and `sizes`; `run.sh` runs some cases twice and
requires the same checksum (Scotch here is built deterministic). Two calls in one process may
differ, as the library's random generator moves on between them.

`run.sh` enforces 1, 2 and 4 ranks and 8 ranks on graphs of 960 to 24,000 vertices, then prints
two informational cases: 8 ranks on 216 vertices, and 6 ranks with `--order-only`. ParMETIS
orders on the largest power of two below P and fills `sizes` for it; Scotch's version returns
`METIS_ERROR` with a valid ordering and no `sizes` there, which SuperLU_DIST never meets, as it
calls the routine on a power-of-two subset of its ranks.
