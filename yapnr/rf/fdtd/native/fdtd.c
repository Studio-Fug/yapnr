/*
 * Native stepper of yapnr.rf's FDTD engine (yapnr.rf.fdtd.native_kernel, backend "native").
 *
 * Runs blocks of Yee steps of `engine.Simulation` in C: the H and E sweeps with their CPML
 * auxiliary terms, the Crank-Nicolson coefficients, the copper-edge mu planes, the inductive
 * sheet's branch currents, the sources and the DTFT probes, on a pool of threads. Every value
 * is computed with exactly the operations of the numpy reference, in the same order, and the
 * library is built with -ffp-contract=off (no fused multiply-add) and without fast-math, so a
 * float64 run is bit-identical to the numpy backend on every instruction set and for any
 * thread count (each output element is written by one thread, from inputs that the previous
 * phase finished). float32 is the same code with float fields, as numpy float32.
 *
 * Layout: every field component lives in one padded box of (nz + 1) planes of (nx + 1) rows
 * of `jp` values (j, the y index, contiguous; rows 64-byte aligned): element (i, j, k) of a
 * component is at ((k * ni) + i) * jp + j with ni = nx + 1. Python views these buffers as the
 * engine's (x, y, z) arrays without copying.
 *
 * Two schedules, the same values. Wavefront (run->tblock >= 1, fdtd_kernels.h `job_wave`):
 * passes of tblock steps over the planes, H of plane k and E of plane k - 1 of a step in one
 * phase, the next step three planes behind, each row with its sources, mu blend, sheet and
 * probes right after its update; the planes of a pass stay in cache. Sweeps (tblock 0, for
 * boxes whose planes do not fit in cache):
 * one step (n -> n + 1), each phase separated by a barrier:
 *   H sweep (curl E, CPML psi; the mu planes blend in the sweep when no H source is active)
 *   [H sources; mu blend]            when an H source is active this step
 *   [H probes]                       when n is a sample step
 *   E sweep (curl H, CPML psi, the sheet's explicit current, Ca/Cb; the sheet's branch
 *            currents advance in the sweep when no E source is active)
 *   [E sources; sheet branches]      when an E source is active this step
 *   [E probes]                       when n + 1 is a sample step
 * Work within a phase is handed out dynamically (rows, source chunks, probe chunks).
 */

#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define YF_ABI 2

#define ALWAYS static inline __attribute__((always_inline))

/* x86-64 Linux: one library with AVX-512, AVX2 and baseline variants of the sweeps, chosen at
 * load time (ifunc). The variants differ in vector width only: no contraction, so the same
 * bits. */
#if defined(__x86_64__) && defined(__linux__) && !defined(YF_NO_CLONES)
#define CLONES __attribute__((target_clones("avx512f", "avx2", "default")))
#else
#define CLONES
#endif

/* ---- data shared with Python (ctypes mirrors these in native_kernel.py) ---------------- */

typedef struct {
  int32_t src;   /* source component (0..5: ex ey ez hx hy hz) */
  int32_t axis;  /* derivative axis (0 x, 1 y, 2 z) */
  int32_t nslot; /* CPML slots along the axis (y: padded row stride of psi) */
  int32_t pad_;
  const void *ik;      /* real[len + 1]: 1/(kappa * length) (times dt/mu0 for H), by coordinate */
  const int32_t *slot; /* [len + 1]: CPML slot of each coordinate, -1 outside */
  const void *b;       /* real[nslot]: psi recursion factor */
  const void *cd;      /* real[nslot]: psi input factor */
  void *psi;           /* real: x [np][nslot][jp], z [nslot][ni][jp], y [np][ni][nslot] */
} yf_term;

typedef struct {
  int32_t j0, j1; /* [j0, j1) of a row */
  int32_t yact;   /* the y term's CPML is active on the segment */
  int32_t slot0;  /* its slot at j0 */
} yf_seg;

typedef struct {
  int32_t k0, k1, i0, i1; /* rows updated: k in [k0, k1), i in [i0, i1) */
  int32_t nseg, pad_;
  const yf_seg *seg; /* the j range of every row, split where the y term's CPML starts/ends */
  yf_term t[2];      /* curl terms: E = +t0 - t1, H = -t0 + t1 (see engine._E_TERMS) */
} yf_comp;

typedef struct {
  int32_t nx, ny, nz; /* cells */
  int32_t ni, jp, np; /* rows per plane (nx + 1), row stride, planes (nz + 1) */
  int32_t dsize;      /* 8: float64, 4: float32 */
  int32_t kc;         /* the copper plane */
  int32_t sheet;      /* the inductive sheet is on */
  int32_t nmu;        /* mu planes entries */
  void *f[6];
  yf_comp c[6];
  const void *ca_s[3], *cb_s[3];          /* real[np]: Ca, Cb of uniform planes */
  const void *const *ca_t[3], *const *cb_t[3]; /* [np]: [ni][jp] table of a plane, or NULL */
  const int32_t *mu_comp, *mu_k0, *mu_k1; /* [nmu] */
  const void *const *mu_m;                /* [nmu]: real[(k1 - k0)][ni][jp] 1/mu_r */
  void *const *mu_old;                    /* [nmu]: the same shape, H before the sweep */
  /* the sheet, ex (0) and ey (1), each [ni][jp] on the plane k_c:
     c1lo c1hi klo khi bhlo bhhi jlo jhi esum */
  void *sh[2][9];
} yf_sim;

typedef struct {
  int32_t comp, count; /* component, edges */
  int32_t k, pad_;     /* k > 0: separable, sum_k amp[k][p] w[b][k]; k == 0: table w[b][p] */
  const int64_t *off;  /* [count] offsets into the component's box */
  const double *scale; /* [count] */
  const double *amp;   /* [k][count] */
  const double *w;     /* this block: [B][k], or [B][count] */
  const uint8_t *act;  /* [B]: the source is on at step n0 + b */
  double *val;         /* [tblock or 1][count] scratch: the values of the pass's steps */
} yf_src;

typedef struct {
  int32_t comp, count;
  const int64_t *off; /* [count] */
  double *re, *im;    /* [m][count], accumulated in place */
} yf_probe;

typedef struct {
  int32_t index, p0, p1, pad_; /* a chunk [p0, p1) of a probe or a source */
} yf_item;

typedef struct {
  int64_t n0, n1; /* steps of the block */
  int32_t dec, m; /* decimation, frequencies */
  int32_t nsrc, nprobe;
  int32_t nhitem, neitem;   /* probe chunks */
  int32_t nhsitem, nesitem; /* source chunks */
  int32_t tblock;           /* steps per wavefront pass; 0: sweeps */
  int32_t nsitem;           /* chunks of all sources */
  const yf_src *src;
  const yf_probe *probe;
  const yf_item *hitem, *eitem;   /* probe chunks of the H and E probes */
  const yf_item *hsitem, *esitem; /* source chunks of the H and E sources */
  const yf_item *sitem;           /* source chunks of all sources */
  const double *hcos, *hsin;      /* [H samples of the block][m]: dec dt cos(w t) */
  const double *ecos, *esin;      /* [E samples][m] */
  /* Wavefront: per row (k ni + i) of the box, the source and probe edges on it (CSR: ptr
   * [rows + 1], entries {index, p}), H and E apart, sources in list order. */
  const int64_t *hs_ptr, *es_ptr, *hp_ptr, *ep_ptr;
  const yf_item *hs_ent, *es_ent, *hp_ent, *ep_ent;
} yf_run;

/* ---- thread pool: one job (a block of steps) at a time, run by every thread ------------ */

typedef struct yf_pool yf_pool;
typedef void (*yf_job)(yf_pool *, int tid, const yf_sim *, const yf_run *);

struct yf_pool {
  int n;
  pthread_t *th;
  pthread_mutex_t mu;
  pthread_cond_t cv;
  int gen, quit;
  yf_job job;
  const yf_sim *sim;
  const yf_run *run;
  atomic_int remaining;
  atomic_int bar_count;
  atomic_int bar_sense;
  _Atomic int64_t ticket;
};

typedef struct {
  yf_pool *pool;
  int tid;
} yf_worker_arg;

static inline void cpu_relax(void) {
#if defined(__aarch64__)
  __asm__ __volatile__("yield");
#elif defined(__x86_64__)
  __asm__ __volatile__("pause");
#endif
}

#define SPINS 4000

static void barrier(yf_pool *P, int *sense) {
  const int s = !*sense;
  *sense = s;
  if (atomic_fetch_add_explicit(&P->bar_count, 1, memory_order_acq_rel) == P->n - 1) {
    atomic_store_explicit(&P->bar_count, 0, memory_order_relaxed);
    atomic_store_explicit(&P->bar_sense, s, memory_order_release);
  } else {
    int spins = 0;
    while (atomic_load_explicit(&P->bar_sense, memory_order_acquire) != s) {
      if (++spins < SPINS)
        cpu_relax();
      else
        sched_yield();
    }
  }
}

/* Dynamic hand-out without resets: in every phase each thread makes exactly one failing
 * claim, so all threads agree on the ticket that starts the next phase. */
static inline int64_t claim(yf_pool *P, int64_t base) {
  return atomic_fetch_add_explicit(&P->ticket, 1, memory_order_relaxed) - base;
}

static void *worker_main(void *arg) {
  yf_worker_arg *a = (yf_worker_arg *)arg;
  yf_pool *P = a->pool;
  const int tid = a->tid;
  free(a);
  int seen = 0;
  for (;;) {
    pthread_mutex_lock(&P->mu);
    while (P->gen == seen && !P->quit) pthread_cond_wait(&P->cv, &P->mu);
    if (P->quit) {
      pthread_mutex_unlock(&P->mu);
      return NULL;
    }
    seen = P->gen;
    yf_job job = P->job;
    const yf_sim *sim = P->sim;
    const yf_run *run = P->run;
    pthread_mutex_unlock(&P->mu);
    job(P, tid, sim, run);
    atomic_fetch_sub_explicit(&P->remaining, 1, memory_order_acq_rel);
  }
}

/* ---- the kernels, once per precision ---------------------------------------------------- */

#define R double
#define SFX(name) name##_f64
#include "fdtd_kernels.h"
#undef R
#undef SFX

#define R float
#define SFX(name) name##_f32
#include "fdtd_kernels.h"
#undef R
#undef SFX

/* ---- API --------------------------------------------------------------------------------- */

int yf_abi(void) { return YF_ABI; }

/* Sizes of the shared structures, so Python can check its mirrors. */
void yf_sizes(int64_t *out) {
  out[0] = (int64_t)sizeof(yf_term);
  out[1] = (int64_t)sizeof(yf_seg);
  out[2] = (int64_t)sizeof(yf_comp);
  out[3] = (int64_t)sizeof(yf_sim);
  out[4] = (int64_t)sizeof(yf_src);
  out[5] = (int64_t)sizeof(yf_probe);
  out[6] = (int64_t)sizeof(yf_item);
  out[7] = (int64_t)sizeof(yf_run);
}

/* The instruction-set variant the sweeps run with (provenance). */
const char *yf_isa(void) {
#if defined(__x86_64__) && defined(__linux__) && !defined(YF_NO_CLONES)
  __builtin_cpu_init();
  if (__builtin_cpu_supports("avx512f")) return "x86-64 avx512f";
  if (__builtin_cpu_supports("avx2")) return "x86-64 avx2";
  return "x86-64";
#elif defined(__x86_64__)
  return "x86-64";
#elif defined(__aarch64__)
  return "arm64 neon";
#else
  return "generic";
#endif
}

void yf_pool_free(void *handle) {
  yf_pool *P = (yf_pool *)handle;
  if (!P) return;
  pthread_mutex_lock(&P->mu);
  P->quit = 1;
  pthread_cond_broadcast(&P->cv);
  pthread_mutex_unlock(&P->mu);
  for (int t = 1; t < P->n; t++) pthread_join(P->th[t], NULL);
  pthread_mutex_destroy(&P->mu);
  pthread_cond_destroy(&P->cv);
  free(P->th);
  free(P);
}

void *yf_pool_new(int nthreads) {
  if (nthreads < 1) nthreads = 1;
  if (nthreads > 256) nthreads = 256;
  yf_pool *P = (yf_pool *)calloc(1, sizeof(yf_pool));
  if (!P) return NULL;
  P->n = nthreads;
  P->th = (pthread_t *)calloc((size_t)nthreads, sizeof(pthread_t));
  pthread_mutex_init(&P->mu, NULL);
  pthread_cond_init(&P->cv, NULL);
  for (int t = 1; t < nthreads; t++) {
    yf_worker_arg *a = (yf_worker_arg *)malloc(sizeof(yf_worker_arg));
    a->pool = P;
    a->tid = t;
    if (pthread_create(&P->th[t], NULL, worker_main, a) != 0) {
      free(a);
      P->n = t;
      break;
    }
  }
  return P;
}

int yf_pool_threads(void *handle) { return ((yf_pool *)handle)->n; }

/* Run steps run->n0 .. run->n1 - 1. Returns 0, or a negative error. */
int yf_run_block(void *handle, const yf_sim *sim, const yf_run *run) {
  yf_pool *P = (yf_pool *)handle;
  if (!P || !sim || !run) return -1;
  if (run->n1 <= run->n0) return 0;
  if (run->dec < 1) return -2;
  yf_job job;
  if (sim->dsize == 8)
    job = job_f64;
  else if (sim->dsize == 4)
    job = job_f32;
  else
    return -3;
  atomic_store(&P->ticket, 0);
  atomic_store(&P->bar_count, 0);
  atomic_store(&P->bar_sense, 0);
  atomic_store(&P->remaining, P->n - 1);
  pthread_mutex_lock(&P->mu);
  P->job = job;
  P->sim = sim;
  P->run = run;
  P->gen++;
  pthread_cond_broadcast(&P->cv);
  pthread_mutex_unlock(&P->mu);
  job(P, 0, sim, run);
  int spins = 0;
  while (atomic_load_explicit(&P->remaining, memory_order_acquire) > 0) {
    if (++spins < SPINS)
      cpu_relax();
    else
      sched_yield();
  }
  return 0;
}
