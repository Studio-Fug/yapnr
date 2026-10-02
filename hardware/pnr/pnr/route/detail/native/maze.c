/*
 * Native search loop of yapnr's packed maze kernel (pnr.route.detail.native_maze).
 *
 * The same A* as pnr/route/detail/packed_maze.py `_search` over a dense field
 * (pnr/route/detail/dense_maze.py): orthogonal moves, 45-degree moves with both
 * corner cells passable, and through-via moves to every other layer; the
 * octile heuristic; a binary heap ordered on (f, tie) with a strictly
 * increasing tie, so pops happen in the same order as Python's heapq; the
 * first pop of a cell closes it; relaxation on a strictly smaller distance,
 * closed or not. Every double is computed in the same order as the Python
 * kernels; build with -ffp-contract=off (no fused multiply-add).
 *
 * Drill spacing against the vias of the path being searched is evaluated with
 * the field's offset stencil; each via relaxation appends one node to a
 * persistent list, so every cell keeps the drill list of the path that last
 * improved it, exactly like the Python kernels' per-cell tuples.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PNR_MAZE_ABI 1

typedef struct {
  int32_t nx, ny, nl, diagonal;
  const double *price;     /* [nl * ny * nx] cell price */
  const double *via_price; /* [ny * nx] through-via price */
  const uint8_t *ok;       /* [nl * ny * nx] passable and not blocked */
  const uint8_t *col;      /* [ny * nx] a via may drop here */
  const uint8_t *plated;   /* [ny * nx] own-net plated pad: no new drill */
  const uint8_t *hole;     /* [ny * nx] drill clears static drills and tree vias */
  const uint8_t *stencil;  /* [(2r+1)^2] drill-to-drill conflicts by offset */
  int32_t stencil_radius;
  double via_cost, sqrt2, octile;
} pnr_field;

typedef struct {
  double f;
  int64_t tie;
  int32_t key;
} entry;

typedef struct {
  int32_t size;
  uint32_t gen;
  uint32_t *seen, *closed, *hseen, *target;
  double *g, *h;
  int32_t *came, *drill;
  entry *heap;
  int64_t heap_len, heap_cap;
  int32_t *dcol, *dprev;
  int64_t dlen, dcap;
  int32_t *txy;
  int64_t tcap;
} context;

int32_t pnr_maze_abi(void) { return PNR_MAZE_ABI; }

void pnr_maze_free(void *pointer) {
  context *c = (context *)pointer;
  if (!c) return;
  free(c->seen);
  free(c->closed);
  free(c->hseen);
  free(c->target);
  free(c->g);
  free(c->h);
  free(c->came);
  free(c->drill);
  free(c->heap);
  free(c->dcol);
  free(c->dprev);
  free(c->txy);
  free(c);
}

void *pnr_maze_new(int32_t size) {
  context *c = (context *)calloc(1, sizeof(context));
  if (!c || size <= 0) {
    free(c);
    return NULL;
  }
  c->size = size;
  c->gen = 0;
  c->seen = (uint32_t *)calloc((size_t)size, sizeof(uint32_t));
  c->closed = (uint32_t *)calloc((size_t)size, sizeof(uint32_t));
  c->hseen = (uint32_t *)calloc((size_t)size, sizeof(uint32_t));
  c->target = (uint32_t *)calloc((size_t)size, sizeof(uint32_t));
  c->g = (double *)malloc((size_t)size * sizeof(double));
  c->h = (double *)malloc((size_t)size * sizeof(double));
  c->came = (int32_t *)malloc((size_t)size * sizeof(int32_t));
  c->drill = (int32_t *)malloc((size_t)size * sizeof(int32_t));
  if (!c->seen || !c->closed || !c->hseen || !c->target || !c->g || !c->h || !c->came ||
      !c->drill) {
    pnr_maze_free(c);
    return NULL;
  }
  return c;
}

static void next_generation(context *c) {
  c->gen++;
  if (c->gen == 0) { /* wrapped: forget every stamp */
    size_t bytes = (size_t)c->size * sizeof(uint32_t);
    memset(c->seen, 0, bytes);
    memset(c->closed, 0, bytes);
    memset(c->hseen, 0, bytes);
    memset(c->target, 0, bytes);
    c->gen = 1;
  }
}

static int before(const entry *a, const entry *b) {
  return a->f < b->f || (a->f == b->f && a->tie < b->tie);
}

static int push(context *c, double f, int64_t tie, int32_t key) {
  if (c->heap_len == c->heap_cap) {
    int64_t cap = c->heap_cap ? 2 * c->heap_cap : 4096;
    entry *grown = (entry *)realloc(c->heap, (size_t)cap * sizeof(entry));
    if (!grown) return -1;
    c->heap = grown;
    c->heap_cap = cap;
  }
  entry e = {f, tie, key};
  int64_t at = c->heap_len++;
  while (at > 0) {
    int64_t parent = (at - 1) / 2;
    if (!before(&e, &c->heap[parent])) break;
    c->heap[at] = c->heap[parent];
    at = parent;
  }
  c->heap[at] = e;
  return 0;
}

static entry pop(context *c) {
  entry top = c->heap[0];
  entry last = c->heap[--c->heap_len];
  int64_t at = 0, n = c->heap_len;
  for (;;) {
    int64_t child = 2 * at + 1;
    if (child >= n) break;
    if (child + 1 < n && before(&c->heap[child + 1], &c->heap[child])) child++;
    if (!before(&c->heap[child], &last)) break;
    c->heap[at] = c->heap[child];
    at = child;
  }
  if (n > 0) c->heap[at] = last;
  return top;
}

static int32_t add_drill(context *c, int32_t column, int32_t previous) {
  if (c->dlen == c->dcap) {
    int64_t cap = c->dcap ? 2 * c->dcap : 1024;
    int32_t *col = (int32_t *)realloc(c->dcol, (size_t)cap * sizeof(int32_t));
    if (!col) return -2;
    c->dcol = col;
    int32_t *prev = (int32_t *)realloc(c->dprev, (size_t)cap * sizeof(int32_t));
    if (!prev) return -2;
    c->dprev = prev;
    c->dcap = cap;
  }
  c->dcol[c->dlen] = column;
  c->dprev[c->dlen] = previous;
  return (int32_t)c->dlen++;
}

static double heuristic(context *c, const pnr_field *f, int32_t key, int32_t targets) {
  if (c->hseen[key] == c->gen) return c->h[key];
  int32_t plane = f->nx * f->ny;
  int32_t ij = key % plane;
  int32_t j = ij / f->nx, i = ij - j * f->nx;
  double best = 0.0;
  int first = 1;
  for (int32_t t = 0; t < targets; t++) {
    int32_t dx = abs(i - c->txy[2 * t]), dy = abs(j - c->txy[2 * t + 1]);
    int32_t low = dx < dy ? dx : dy;
    double product = f->octile * (double)low;
    double distance = (double)(dx + dy) - product;
    if (first || distance < best) {
      best = distance;
      first = 0;
    }
  }
  if (best == 0.0) best = 0.0; /* Python's `best or 0.0` */
  c->hseen[key] = c->gen;
  c->h[key] = best;
  return best;
}

/* Returns the path length (sources first) in `path`, 0 when no path exists,
 * -1 when `capacity` is too small, -2 on allocation failure, -3 on bad input. */
int32_t pnr_maze_search(void *pointer, const pnr_field *f, const int32_t *starts, int32_t nstarts,
                        const int32_t *ends, int32_t nends, int32_t *path, int32_t capacity) {
  context *c = (context *)pointer;
  const int32_t nx = f->nx, ny = f->ny, nl = f->nl;
  const int32_t plane = nx * ny;
  if (!c || nx <= 0 || ny <= 0 || nl <= 0 || (int64_t)plane * nl > c->size) return -3;
  next_generation(c);
  const uint32_t gen = c->gen;
  c->heap_len = 0;
  c->dlen = 0;
  if ((int64_t)nends * 2 > c->tcap) {
    int32_t *txy = (int32_t *)realloc(c->txy, (size_t)nends * 2 * sizeof(int32_t));
    if (!txy) return -2;
    c->txy = txy;
    c->tcap = (int64_t)nends * 2;
  }
  int32_t targets = 0;
  for (int32_t t = 0; t < nends; t++) {
    int32_t key = ends[t];
    if (key < 0 || key >= plane * nl) return -3;
    c->target[key] = gen;
    int32_t ij = key % plane;
    c->txy[2 * targets] = ij % nx;
    c->txy[2 * targets + 1] = ij / nx;
    targets++;
  }
  const double *price = f->price, *via_price = f->via_price;
  const uint8_t *ok = f->ok, *col = f->col, *plated = f->plated, *hole = f->hole;
  const uint8_t *stencil = f->stencil;
  const int32_t radius = f->stencil_radius, width = 2 * f->stencil_radius + 1;
  static const int32_t orthogonal[4][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
  static const int32_t diagonal[4][2] = {{1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
  int64_t tie = 0;
  for (int32_t s = 0; s < nstarts; s++) {
    int32_t key = starts[s];
    if (key < 0 || key >= plane * nl) return -3;
    c->g[key] = 0.0;
    c->seen[key] = gen;
    c->came[key] = -1;
    c->drill[key] = -1;
    if (push(c, heuristic(c, f, key, targets), tie++, key)) return -2;
  }

#define RELAX(NEXT, DISTANCE, DRILL)                                                     \
  do {                                                                                 \
    if (c->seen[NEXT] != gen || (DISTANCE) < c->g[NEXT]) {                             \
      int32_t drill_ = (DRILL);                                                        \
      if (drill_ < -1) return -2;                                                      \
      c->seen[NEXT] = gen;                                                             \
      c->g[NEXT] = (DISTANCE);                                                         \
      c->came[NEXT] = current;                                                         \
      c->drill[NEXT] = drill_;                                                         \
      if (push(c, (DISTANCE) + heuristic(c, f, NEXT, targets), tie++, NEXT)) return -2; \
    }                                                                                  \
  } while (0)

  while (c->heap_len > 0) {
    entry top = pop(c);
    int32_t current = top.key;
    if (c->closed[current] == gen) continue;
    c->closed[current] = gen;
    if (c->target[current] == gen) {
      int32_t length = 0;
      for (int32_t k = current;; k = c->came[k]) {
        length++;
        if (c->came[k] < 0) break;
      }
      if (length > capacity) return -1;
      int32_t at = length;
      for (int32_t k = current;; k = c->came[k]) {
        path[--at] = k;
        if (c->came[k] < 0) break;
      }
      return length;
    }
    const int32_t layer = current / plane;
    const int32_t ij = current - layer * plane;
    const int32_t j = ij / nx, i = ij - j * nx;
    const double base = c->g[current];
    const int32_t layer_base = layer * plane;
    const int32_t drills = c->drill[current];
    for (int m = 0; m < 4; m++) {
      int32_t ni = i + orthogonal[m][0], nj = j + orthogonal[m][1];
      if (ni < 0 || ni >= nx || nj < 0 || nj >= ny) continue;
      int32_t next = layer_base + nj * nx + ni;
      if (!ok[next]) continue;
      double distance = base + price[next];
      RELAX(next, distance, drills);
    }
    if (f->diagonal) {
      for (int m = 0; m < 4; m++) {
        int32_t ni = i + diagonal[m][0], nj = j + diagonal[m][1];
        if (ni < 0 || ni >= nx || nj < 0 || nj >= ny) continue;
        int32_t next = layer_base + nj * nx + ni;
        if (!ok[next] || !ok[layer_base + j * nx + ni] || !ok[layer_base + nj * nx + i]) continue;
        double step = f->sqrt2 * price[next];
        double distance = base + step;
        RELAX(next, distance, drills);
      }
    }
    if (!col[ij]) continue;
    const int drop = !plated[ij];
    if (drop) {
      if (!hole[ij]) continue;
      int clear = 1;
      for (int32_t q = drills; q >= 0; q = c->dprev[q]) {
        int32_t site = c->dcol[q];
        int32_t sj = site / nx, si = site - sj * nx;
        int32_t di = i - si, dj = j - sj;
        if (di >= -radius && di <= radius && dj >= -radius && dj <= radius &&
            stencil[(dj + radius) * width + (di + radius)]) {
          clear = 0;
          break;
        }
      }
      if (!clear) continue;
    }
    for (int32_t target_layer = 0; target_layer < nl; target_layer++) {
      if (target_layer == layer) continue;
      int32_t next = target_layer * plane + ij;
      if (!ok[next]) continue;
      double distance = base + (drop ? via_price[ij] : price[next]);
      distance = distance + (drop ? f->via_cost : 0.0);
      RELAX(next, distance, drop ? add_drill(c, ij, drills) : drills);
    }
  }
#undef RELAX
  return 0;
}
