/*
 * nodend_check: does a library's ParMETIS_V3_NodeND do what the ParMETIS 4.0 manual says
 * (section 4.2.4, "Format of the Ordering and Separator Sizes Arrays", and section 5.4)?
 *
 * Written from that manual only, for the Palace task image (docker/palace), which links Scotch's
 * ParMETIS-compatible library (libptscotchparmetisv3, on PT-Scotch) where Palace's superbuild
 * linked ParMETIS. SuperLU_DIST relies on what is checked here: its parallel symbolic
 * factorization reads the separator tree from `sizes`. The test needs only the header and the
 * library under test and MPI.
 *
 *   mpirun -np P nodend_check NX NY NZ [--numflag 0|1] [--skew] [--order-only]
 *
 * The graph is the 7-point grid NX x NY x NZ (vertex x + NX*(y + NY*z)), distributed in
 * contiguous blocks (with --skew, rank 0 gets three times the share of the others). Checks, for
 * P a power of two:
 *   - the call returns METIS_OK;
 *   - `order` is a permutation of the vertices (numbered from numflag);
 *   - `sizes` is the same on every rank: P sub-domain sizes, then the P-1 separator sizes, the
 *     top one last, levels left to right; every entry is positive and they add up to the
 *     number of vertices;
 *   - the new numbering is the postorder of that separator tree (each subtree's vertices are
 *     contiguous: left subtree, right subtree, then its separator), and every separator
 *     separates: no edge joins two vertices whose tree nodes are not on one root path;
 *   - a second call in the same process passes the same checks.
 * It prints a checksum of the first call's `order` and `sizes`: the same command must print the
 * same checksum on every run (run.sh runs some twice; Scotch here is built deterministic). Two
 * calls in one process need not agree: the library's random generator moves on between them.
 * With --order-only (any P; the manual's p' levels for P not a power of two are not checked)
 * only the permutation is checked and the return value is reported.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later (yapnr)
 */
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "parmetis.h"

#ifndef NODEND_IDX
#define NODEND_IDX int /* idx_t: 32-bit, as SCOTCH_Num and SuperLU_DIST's int_t in this build */
#endif
typedef NODEND_IDX idx;

static int rank, nprocs;

static void fail(const char *what) {
  fprintf(stderr, "nodend_check: FAIL (rank %d of %d): %s\n", rank, nprocs, what);
  MPI_Abort(MPI_COMM_WORLD, 1);
}

static void *xmalloc(size_t n) {
  void *p = malloc(n ? n : 1);
  if (!p) fail("out of memory");
  return p;
}

/* neighbours of grid vertex v; returns their number */
static int neighbours(long nx, long ny, long nz, long v, long out[6]) {
  long x = v % nx, y = (v / nx) % ny, z = v / (nx * ny), k = 0;
  if (x > 0) out[k++] = v - 1;
  if (x < nx - 1) out[k++] = v + 1;
  if (y > 0) out[k++] = v - nx;
  if (y < ny - 1) out[k++] = v + nx;
  if (z > 0) out[k++] = v - nx * ny;
  if (z < nz - 1) out[k++] = v + nx * ny;
  return (int)k;
}

/* tree node h (heap numbering: root 1, children 2h and 2h+1, left first) at level L and
 * position j from the left -> its entry in `sizes` (section 4.2.4) */
static long size_index(long p, int level, long j) { return 2 * p - (2L << level) + j; }

static int level_of(long h) {
  int l = 0;
  while (h > 1) h >>= 1, l++;
  return l;
}

/* fill total[h] (vertices in the subtree of h) and the first new number of each subtree */
static void subtree_totals(const idx *sizes, long p, int levels, long h, long *total) {
  int l = level_of(h);
  long own = sizes[size_index(p, l, h - (1L << l))];
  if (l == levels) {
    total[h] = own;
    return;
  }
  subtree_totals(sizes, p, levels, 2 * h, total);
  subtree_totals(sizes, p, levels, 2 * h + 1, total);
  total[h] = total[2 * h] + total[2 * h + 1] + own;
}

/* owner[k] = tree node of new number k: subtree h starts at `first` */
static void assign(const long *total, long p, int levels, long h, long first, long *owner) {
  if (level_of(h) == levels) {
    for (long k = first; k < first + total[h]; k++) owner[k] = h;
    return;
  }
  assign(total, p, levels, 2 * h, first, owner);
  assign(total, p, levels, 2 * h + 1, first + total[2 * h], owner);
  for (long k = first + total[2 * h] + total[2 * h + 1]; k < first + total[h]; k++) owner[k] = h;
}

/* a is h itself or an ancestor of h */
static int on_root_path(long a, long h) {
  while (h > a) h >>= 1;
  return h == a;
}

/* rank 0: the gathered ordering `all` and the `sizes` of one call */
static void check(const idx *all, const idx *sizes, long n, long p, int levels, int numflag,
                  int order_only, long nx, long ny, long nz, const char *which) {
  char msg[256];
  char *seen = calloc(n, 1);
  if (!seen) fail("out of memory");
  for (long v = 0; v < n; v++) {
    long k = all[v] - numflag;
    if (k < 0 || k >= n || seen[k]) {
      snprintf(msg, sizeof msg, "%s: order is not a permutation of the vertices", which);
      fail(msg);
    }
    seen[k] = 1;
  }
  free(seen);
  if (order_only) return;
  long ns = 2 * p - 1, sum = 0;
  for (long i = 0; i < ns; i++) {
    if (sizes[i] <= 0) {
      snprintf(msg, sizeof msg, "%s: sizes[%ld] = %ld, an empty sub-domain or separator", which,
               i, (long)sizes[i]);
      fail(msg);
    }
    sum += sizes[i];
  }
  if (sum != n) fail("sizes do not add up to the number of vertices");
  /* postorder: the tree node owning each new number */
  long *total = xmalloc(2 * p * sizeof(long)), *owner = xmalloc(n * sizeof(long));
  subtree_totals(sizes, p, levels, 1, total);
  assign(total, p, levels, 1, 0, owner);
  /* separators separate: walk every edge of the grid */
  long nb[6];
  for (long v = 0; v < n; v++) {
    int k = neighbours(nx, ny, nz, v, nb);
    for (int j = 0; j < k; j++) {
      long a = owner[all[v] - numflag], b = owner[all[nb[j]] - numflag];
      if (!on_root_path(a, b) && !on_root_path(b, a)) {
        snprintf(msg, sizeof msg, "%s: edge %ld-%ld joins tree nodes %ld and %ld (different subtrees)",
                 which, v, nb[j], a, b);
        fail(msg);
      }
    }
  }
  printf("nodend_check:   %s: sizes", which);
  for (long i = 0; i < ns; i++) printf(" %ld", (long)sizes[i]);
  printf("; postorder separator tree of %d levels; separators separate\n", levels);
  free(total);
  free(owner);
}

int main(int argc, char **argv) {
  MPI_Init(&argc, &argv);
  MPI_Comm_rank(MPI_COMM_WORLD, &rank);
  MPI_Comm_size(MPI_COMM_WORLD, &nprocs);
  if (argc < 4) fail("usage: nodend_check NX NY NZ [--numflag 0|1] [--skew] [--order-only]");
  long nx = atol(argv[1]), ny = atol(argv[2]), nz = atol(argv[3]);
  int numflag = 0, skew = 0, order_only = 0;
  for (int i = 4; i < argc; i++) {
    if (!strcmp(argv[i], "--numflag") && i + 1 < argc)
      numflag = atoi(argv[++i]);
    else if (!strcmp(argv[i], "--skew"))
      skew = 1;
    else if (!strcmp(argv[i], "--order-only"))
      order_only = 1;
    else
      fail("unknown argument");
  }
  long n = nx * ny * nz;
  long p = nprocs;
  int levels = 0;
  while ((1L << (levels + 1)) <= p) levels++;
  if (!order_only && (1L << levels) != p) fail("P must be a power of two (or --order-only)");

  /* vtxdist: contiguous blocks, numbered from numflag */
  idx *vtxdist = xmalloc((p + 1) * sizeof(idx));
  long shares = skew ? p + 2 : p, base = n / shares, extra = n % shares, at = 0;
  for (long r = 0; r < p; r++) {
    vtxdist[r] = (idx)(at + numflag);
    at += (skew && r == 0) ? 3 * base + extra : base + (!skew && r < extra);
  }
  vtxdist[p] = (idx)(n + numflag);
  if (at != n) fail("vtxdist does not cover the graph");
  long first = vtxdist[rank] - numflag, nloc = vtxdist[rank + 1] - vtxdist[rank];

  idx *xadj = xmalloc((nloc + 1) * sizeof(idx));
  idx *adjncy = xmalloc((6 * nloc) * sizeof(idx));
  long nb[6], e = 0;
  xadj[0] = (idx)numflag;
  for (long i = 0; i < nloc; i++) {
    int k = neighbours(nx, ny, nz, first + i, nb);
    for (int j = 0; j < k; j++) adjncy[e++] = (idx)(nb[j] + numflag);
    xadj[i + 1] = (idx)(e + numflag);
  }

  idx options[4] = {0, 0, 0, 0}; /* options[0] = 0: the defaults */
  idx *order = xmalloc(nloc * sizeof(idx)), *order2 = xmalloc(nloc * sizeof(idx));
  idx *sizes = xmalloc(2 * p * sizeof(idx)), *sizes2 = xmalloc(2 * p * sizeof(idx));
  memset(sizes, 0, 2 * p * sizeof(idx));
  memset(sizes2, 0, 2 * p * sizeof(idx));
  MPI_Comm comm = MPI_COMM_WORLD;
  idx nf = (idx)numflag;
  double t0 = MPI_Wtime();
  int status = ParMETIS_V3_NodeND(vtxdist, xadj, adjncy, &nf, options, order, sizes, &comm);
  double seconds = MPI_Wtime() - t0;
  int status2 = ParMETIS_V3_NodeND(vtxdist, xadj, adjncy, &nf, options, order2, sizes2, &comm);

  /* gather the ordering (and the sizes of every rank) on rank 0 */
  int *counts = xmalloc(p * sizeof(int)), *displs = xmalloc(p * sizeof(int));
  for (long r = 0; r < p; r++) {
    counts[r] = (int)(vtxdist[r + 1] - vtxdist[r]);
    displs[r] = (int)(vtxdist[r] - numflag);
  }
  MPI_Datatype t = sizeof(idx) == 8 ? MPI_INT64_T : MPI_INT;
  idx *all = rank == 0 ? xmalloc(n * sizeof(idx)) : NULL;
  idx *all2 = rank == 0 ? xmalloc(n * sizeof(idx)) : NULL;
  MPI_Gatherv(order, (int)nloc, t, all, counts, displs, t, 0, comm);
  MPI_Gatherv(order2, (int)nloc, t, all2, counts, displs, t, 0, comm);
  idx *allsizes = rank == 0 ? xmalloc(2 * p * p * sizeof(idx)) : NULL;
  MPI_Gather(sizes, (int)(2 * p), t, allsizes, (int)(2 * p), t, 0, comm);
  int statuses[2] = {status, status2}, *allstatus = rank == 0 ? xmalloc(2 * p * sizeof(int)) : 0;
  MPI_Gather(statuses, 2, MPI_INT, allstatus, 2, MPI_INT, 0, comm);

  int ok = 1;
  if (rank == 0) {
    for (int r = 0; r < p; r++)
      if (!order_only && (allstatus[2 * r] != METIS_OK || allstatus[2 * r + 1] != METIS_OK))
        fail("ParMETIS_V3_NodeND did not return METIS_OK");
    for (long r = 1; r < p && !order_only; r++)
      if (memcmp(allsizes, allsizes + 2 * p * r, (2 * p - 1) * sizeof(idx)))
        fail("sizes differ between ranks");
    check(all, sizes, n, p, levels, numflag, order_only, nx, ny, nz, "first call");
    check(all2, sizes2, n, p, levels, numflag, order_only, nx, ny, nz, "second call");
    unsigned long long h = 1469598103934665603ULL; /* FNV-1a over order and sizes */
    for (long v = 0; v < n; v++) h = (h ^ (unsigned long long)(long long)all[v]) * 1099511628211ULL;
    for (long i = 0; i < 2 * p - 1 && !order_only; i++)
      h = (h ^ (unsigned long long)(long long)sizes[i]) * 1099511628211ULL;
    printf("nodend_check: P=%ld grid %ldx%ldx%ld (n=%ld) numflag=%d%s: returned %d, %.3f s, "
           "checksum %016llx: %s\n",
           p, nx, ny, nz, n, numflag, skew ? " skew" : "", status, seconds, h,
           order_only ? "order is a permutation" : "PASS");
  }
  MPI_Bcast(&ok, 1, MPI_INT, 0, MPI_COMM_WORLD);
  MPI_Finalize();
  return ok ? 0 : 1;
}
