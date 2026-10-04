/*
 * The kernels of fdtd.c for one precision: included with R (double or float) and SFX(name)
 * defined. Every expression mirrors one numpy operation of yapnr/rf/fdtd/engine.py, in the
 * same order; the comments name them.
 */

/* A value that is either a vector over j or one scalar for the row. */
#define AT(p, v, j) ((v) ? (p)[j] : (p)[0])

/* H += -(d0 ik0) [- psi0] + (d1 ik1) [+ psi1] on [j0, j1) of a row:
 *   d = diff(src, axis)                     np.subtract(a[hi], a[lo])
 *   out -= d * ik  /  out += d * ik          addmul_(out, d, ik, -sign)
 *   psi = psi * b + cd * d                  axpby_(psi, b, cd, d)
 *   out -= psi  /  out += psi                add_(out, psi, -sign)
 * v*: ik, b, cd are vectors over j (the y term) or scalars; q*: the term's CPML is active. */
ALWAYS void SFX(hseg)(int j0, int j1, R *restrict h, const R *restrict p0, const R *restrict m0,
                      const R *restrict k0, const int v0, R *restrict s0, const R *restrict b0,
                      const R *restrict c0, const int q0, const R *restrict p1,
                      const R *restrict m1, const R *restrict k1, const int v1, R *restrict s1,
                      const R *restrict b1, const R *restrict c1, const int q1) {
  const R K0 = v0 ? 0 : k0[0], K1 = v1 ? 0 : k1[0];
  const R B0 = (q0 && !v0) ? b0[0] : 0, C0 = (q0 && !v0) ? c0[0] : 0;
  const R B1 = (q1 && !v1) ? b1[0] : 0, C1 = (q1 && !v1) ? c1[0] : 0;
  for (int j = j0; j < j1; j++) {
    const R d0 = p0[j] - m0[j];
    R x = h[j] - d0 * (v0 ? k0[j] : K0);
    if (q0) {
      const R t = s0[j] * (v0 ? b0[j] : B0) + (v0 ? c0[j] : C0) * d0;
      s0[j] = t;
      x = x - t;
    }
    const R d1 = p1[j] - m1[j];
    x = x + d1 * (v1 ? k1[j] : K1);
    if (q1) {
      const R t = s1[j] * (v1 ? b1[j] : B1) + (v1 ? c1[j] : C1) * d1;
      s1[j] = t;
      x = x + t;
    }
    h[j] = x;
  }
}

/* E on [j0, j1) of a row:
 *   curl = d0 * ik0                         mul_into(d, ik, curl)
 *   curl += psi0                            (psi as for H)
 *   curl -= d1 * ik1 ; curl -= psi1
 *   E = E * Ca + Cb * curl                  axpby_(E, Ca, Cb, curl)
 * mode 0: Ca, Cb scalars; 1: vectors (a table row); 2: store the curl in out[] instead. */
ALWAYS void SFX(eseg)(int j0, int j1, R *restrict e, const R *restrict p0, const R *restrict m0,
                      const R *restrict k0, const int v0, R *restrict s0, const R *restrict b0,
                      const R *restrict c0, const int q0, const R *restrict p1,
                      const R *restrict m1, const R *restrict k1, const int v1, R *restrict s1,
                      const R *restrict b1, const R *restrict c1, const int q1,
                      const R *restrict ca, const R *restrict cb, R *restrict out,
                      const int mode) {
  const R K0 = v0 ? 0 : k0[0], K1 = v1 ? 0 : k1[0];
  const R B0 = (q0 && !v0) ? b0[0] : 0, C0 = (q0 && !v0) ? c0[0] : 0;
  const R B1 = (q1 && !v1) ? b1[0] : 0, C1 = (q1 && !v1) ? c1[0] : 0;
  const R CA = mode == 0 ? ca[0] : 0, CB = mode == 0 ? cb[0] : 0;
  for (int j = j0; j < j1; j++) {
    const R d0 = p0[j] - m0[j];
    R c = d0 * (v0 ? k0[j] : K0);
    if (q0) {
      const R t = s0[j] * (v0 ? b0[j] : B0) + (v0 ? c0[j] : C0) * d0;
      s0[j] = t;
      c = c + t;
    }
    const R d1 = p1[j] - m1[j];
    c = c - d1 * (v1 ? k1[j] : K1);
    if (q1) {
      const R t = s1[j] * (v1 ? b1[j] : B1) + (v1 ? c1[j] : C1) * d1;
      s1[j] = t;
      c = c - t;
    }
    if (mode == 2)
      out[j] = c;
    else if (mode == 1)
      e[j] = e[j] * ca[j] + cb[j] * c;
    else
      e[j] = e[j] * CA + CB * c;
  }
}

/* Specializations: (v0, v1) per component, (q0, q1) per segment, mode per row. */
#define SFX_HCALL(V0, V1, Q0, Q1)                                                              \
  SFX(hseg)(j0, j1, h, p0, m0, k0, V0, s0, b0, c0, Q0, p1, m1, k1, V1, s1, b1, c1, Q1)
#define SFX_HSW(V0, V1)                                                                         \
  switch (q0 * 2 + q1) {                                                                       \
  case 0: SFX_HCALL(V0, V1, 0, 0); break;                                                      \
  case 1: SFX_HCALL(V0, V1, 0, 1); break;                                                      \
  case 2: SFX_HCALL(V0, V1, 1, 0); break;                                                      \
  default: SFX_HCALL(V0, V1, 1, 1); break;                                                     \
  }

CLONES static void SFX(hseg_any)(int vy, int j0, int j1, R *h, const R *p0, const R *m0, const R *k0,
                          R *s0, const R *b0, const R *c0, int q0, const R *p1, const R *m1,
                          const R *k1, R *s1, const R *b1, const R *c1, int q1) {
  if (vy == 0) {
    SFX_HSW(1, 0)
  } else if (vy == 1) {
    SFX_HSW(0, 1)
  } else {
    SFX_HSW(0, 0)
  }
}

#define SFX_ECALL(V0, V1, Q0, Q1, M)                                                           \
  SFX(eseg)(j0, j1, e, p0, m0, k0, V0, s0, b0, c0, Q0, p1, m1, k1, V1, s1, b1, c1, Q1, ca, cb, \
            out, M)
#define SFX_ESWQ(V0, V1, M)                                                                     \
  switch (q0 * 2 + q1) {                                                                       \
  case 0: SFX_ECALL(V0, V1, 0, 0, M); break;                                                   \
  case 1: SFX_ECALL(V0, V1, 0, 1, M); break;                                                   \
  case 2: SFX_ECALL(V0, V1, 1, 0, M); break;                                                   \
  default: SFX_ECALL(V0, V1, 1, 1, M); break;                                                  \
  }
#define SFX_ESW(V0, V1)                                                                         \
  if (mode == 0) {                                                                             \
    SFX_ESWQ(V0, V1, 0)                                                                        \
  } else if (mode == 1) {                                                                      \
    SFX_ESWQ(V0, V1, 1)                                                                        \
  } else {                                                                                     \
    SFX_ESWQ(V0, V1, 2)                                                                        \
  }

CLONES static void SFX(eseg_any)(int vy, int mode, int j0, int j1, R *e, const R *p0, const R *m0,
                          const R *k0, R *s0, const R *b0, const R *c0, int q0, const R *p1,
                          const R *m1, const R *k1, R *s1, const R *b1, const R *c1, int q1,
                          const R *ca, const R *cb, R *out) {
  if (vy == 0) {
    SFX_ESW(1, 0)
  } else if (vy == 1) {
    SFX_ESW(0, 1)
  } else {
    SFX_ESW(0, 0)
  }
}

/* Offset of the +1 neighbour along an axis. */
ALWAYS size_t SFX(delta)(const yf_sim *s, int axis) {
  return axis == 0 ? (size_t)s->jp : axis == 1 ? (size_t)1 : (size_t)s->ni * (size_t)s->jp;
}

/* Row-constant data of a term on row (k, i): ik (pointer to the scalar, or the vector), the
 * CPML slot (-1: none) and the psi row (y: the row of slots). */
typedef struct {
  const R *ik, *b, *cd;
  R *psi;
  int act; /* x/z: the row is in the CPML; y: the row has psi slots */
} SFX(trow);

ALWAYS SFX(trow) SFX(term_row)(const yf_sim *s, const yf_term *T, int k, int i) {
  SFX(trow) r;
  const R *ik = (const R *)T->ik;
  const R *b = (const R *)T->b, *cd = (const R *)T->cd;
  R *psi = (R *)T->psi;
  r.b = r.cd = NULL;
  r.psi = NULL;
  r.act = 0;
  if (T->axis == 1) {
    r.ik = ik;
    if (T->nslot > 0) {
      r.act = 1;
      r.b = b;
      r.cd = cd;
      r.psi = psi + ((size_t)k * s->ni + i) * (size_t)T->nslot;
    }
    return r;
  }
  const int u = T->axis == 0 ? i : k;
  r.ik = ik + u;
  const int sl = T->slot[u];
  if (sl >= 0) {
    r.act = 1;
    r.b = b + sl;
    r.cd = cd + sl;
    if (T->axis == 0)
      r.psi = psi + ((size_t)k * T->nslot + sl) * (size_t)s->jp;
    else
      r.psi = psi + ((size_t)sl * s->ni + i) * (size_t)s->jp;
  }
  return r;
}

/* The mu entry of component c on plane k, or -1. */
ALWAYS int SFX(mu_entry)(const yf_sim *s, int c, int k) {
  for (int e = 0; e < s->nmu; e++)
    if (s->mu_comp[e] == c && k >= s->mu_k0[e] && k < s->mu_k1[e]) return e;
  return -1;
}

ALWAYS int SFX(in_region)(const yf_comp *C, int k, int i) {
  return k >= C->k0 && k < C->k1 && i >= C->i0 && i < C->i1;
}

/* Update one row of H component c (3..5); with `old`, keep the row before the update there
 * (copy_into(H, old) of a mu plane). */
static void SFX(h_row)(const yf_sim *s, int c, int k, int i, R *old) {
  const yf_comp *C = &s->c[c];
  const size_t o = ((size_t)k * s->ni + i) * (size_t)s->jp;
  R *h = (R *)s->f[c] + o;
  const yf_term *T0 = &C->t[0], *T1 = &C->t[1];
  const R *a0 = (const R *)s->f[T0->src] + o, *a1 = (const R *)s->f[T1->src] + o;
  const R *p0 = a0 + SFX(delta)(s, T0->axis), *p1 = a1 + SFX(delta)(s, T1->axis);
  const SFX(trow) r0 = SFX(term_row)(s, T0, k, i), r1 = SFX(term_row)(s, T1, k, i);
  const int vy = T0->axis == 1 ? 0 : T1->axis == 1 ? 1 : 2;
  if (old) {
    const int jlo = C->seg[0].j0, jhi = C->seg[C->nseg - 1].j1;
    memcpy(old + jlo, h + jlo, sizeof(R) * (size_t)(jhi - jlo));
  }
  for (int g = 0; g < C->nseg; g++) {
    const yf_seg *S = &C->seg[g];
    const int j0 = S->j0, j1 = S->j1;
    R *s0 = r0.psi, *s1 = r1.psi;
    const R *b0 = r0.b, *c0 = r0.cd, *b1 = r1.b, *c1 = r1.cd;
    int q0 = r0.act, q1 = r1.act;
    if (vy == 0) {
      q0 = S->yact && r0.act;
      if (q0) {
        s0 += S->slot0 - j0;
        b0 += S->slot0 - j0;
        c0 += S->slot0 - j0;
      }
    } else if (vy == 1) {
      q1 = S->yact && r1.act;
      if (q1) {
        s1 += S->slot0 - j0;
        b1 += S->slot0 - j0;
        c1 += S->slot0 - j0;
      }
    }
    SFX(hseg_any)(vy, j0, j1, h, p0, a0, r0.ik, s0, b0, c0, q0, p1, a1, r1.ik, s1, b1, c1, q1);
  }
}

/* blend_(H, old, m) on a row of mu entry e: H -= old; H *= m; H += old. */
static void SFX(mu_row)(const yf_sim *s, int e, int k, int i, const R *old) {
  const int c = s->mu_comp[e];
  const yf_comp *C = &s->c[c];
  R *h = (R *)s->f[c] + ((size_t)k * s->ni + i) * (size_t)s->jp;
  const R *m = (const R *)s->mu_m[e] + ((size_t)(k - s->mu_k0[e]) * s->ni + i) * (size_t)s->jp;
  const int jlo = C->seg[0].j0, jhi = C->seg[C->nseg - 1].j1;
  for (int j = jlo; j < jhi; j++) h[j] = (h[j] - old[j]) * m[j] + old[j];
}

/* Update one row of E component c (0..2). On the copper plane with the inductive sheet:
 * sheetmode 1 advances the sheet's branch currents in the row (no E source acts), 2 keeps E^n
 * in esum (they advance after the sources, `sheet_row`). `scratch`: one row. */
static void SFX(e_row)(const yf_sim *s, int c, int k, int i, R *scratch, int sheetmode) {
  const yf_comp *C = &s->c[c];
  const size_t o = ((size_t)k * s->ni + i) * (size_t)s->jp;
  R *e = (R *)s->f[c] + o;
  const yf_term *T0 = &C->t[0], *T1 = &C->t[1];
  const R *p0 = (const R *)s->f[T0->src] + o, *p1 = (const R *)s->f[T1->src] + o;
  const R *m0 = p0 - SFX(delta)(s, T0->axis), *m1 = p1 - SFX(delta)(s, T1->axis);
  const SFX(trow) r0 = SFX(term_row)(s, T0, k, i), r1 = SFX(term_row)(s, T1, k, i);
  const int vy = T0->axis == 1 ? 0 : T1->axis == 1 ? 1 : 2;
  const R *ca, *cb;
  int mode;
  if (s->ca_t[c][k]) {
    ca = (const R *)s->ca_t[c][k] + (size_t)i * s->jp;
    cb = (const R *)s->cb_t[c][k] + (size_t)i * s->jp;
    mode = 1;
  } else {
    ca = (const R *)s->ca_s[c] + k;
    cb = (const R *)s->cb_s[c] + k;
    mode = 0;
  }
  const int sheet = s->sheet && c < 2 && k == s->kc;
  if (sheet) mode = 2;
  for (int g = 0; g < C->nseg; g++) {
    const yf_seg *S = &C->seg[g];
    const int j0 = S->j0, j1 = S->j1;
    R *s0 = r0.psi, *s1 = r1.psi;
    const R *b0 = r0.b, *c0 = r0.cd, *b1 = r1.b, *c1 = r1.cd;
    int q0 = r0.act, q1 = r1.act;
    if (vy == 0) {
      q0 = S->yact && r0.act;
      if (q0) {
        s0 += S->slot0 - j0;
        b0 += S->slot0 - j0;
        c0 += S->slot0 - j0;
      }
    } else if (vy == 1) {
      q1 = S->yact && r1.act;
      if (q1) {
        s1 += S->slot0 - j0;
        b1 += S->slot0 - j0;
        c1 += S->slot0 - j0;
      }
    }
    SFX(eseg_any)(vy, mode, j0, j1, e, p0, m0, r0.ik, s0, b0, c0, q0, p1, m1, r1.ik, s1, b1, c1,
                  q1, ca, cb, scratch);
  }
  if (!sheet) return;
  /* The inductive sheet (engine._step_e, _step_sheet), on the copper plane's row:
   *   buf = jlo * c1lo ; buf += jhi * c1hi ; curl -= buf ; esum = E ; E = E Ca + Cb curl
   *   esum += E ; jlo = jlo klo + bhlo esum ; jhi = jhi khi + bhhi esum */
  const size_t so = (size_t)i * s->jp;
  void *const *A = s->sh[c];
  const R *c1lo = (const R *)A[0] + so, *c1hi = (const R *)A[1] + so;
  const R *klo = (const R *)A[2] + so, *khi = (const R *)A[3] + so;
  const R *bhlo = (const R *)A[4] + so, *bhhi = (const R *)A[5] + so;
  R *jlo = (R *)A[6] + so, *jhi = (R *)A[7] + so, *esum = (R *)A[8] + so;
  const int ja = C->seg[0].j0, jb = C->seg[C->nseg - 1].j1;
  const int tab = s->ca_t[c][k] != NULL;
  const R CA = tab ? 0 : ca[0], CB = tab ? 0 : cb[0];
  for (int j = ja; j < jb; j++) {
    R buf = jlo[j] * c1lo[j];
    buf = buf + jhi[j] * c1hi[j];
    const R cu = scratch[j] - buf;
    const R eo = e[j];
    const R en = eo * (tab ? ca[j] : CA) + (tab ? cb[j] : CB) * cu;
    e[j] = en;
    if (sheetmode == 1) {
      const R es = eo + en;
      jlo[j] = jlo[j] * klo[j] + bhlo[j] * es;
      jhi[j] = jhi[j] * khi[j] + bhhi[j] * es;
    } else {
      esum[j] = eo;
    }
  }
}

/* The sheet's branch currents on row i of ex (c = 0) or ey (1) after the E sources. */
static void SFX(sheet_row)(const yf_sim *s, int c, int i) {
  const yf_comp *C = &s->c[c];
  if (i < C->i0 || i >= C->i1) return;
  const size_t so = (size_t)i * s->jp;
  const R *e = (const R *)s->f[c] + ((size_t)s->kc * s->ni + i) * (size_t)s->jp;
  void *const *A = s->sh[c];
  const R *klo = (const R *)A[2] + so, *khi = (const R *)A[3] + so;
  const R *bhlo = (const R *)A[4] + so, *bhhi = (const R *)A[5] + so;
  R *jlo = (R *)A[6] + so, *jhi = (R *)A[7] + so, *esum = (R *)A[8] + so;
  const int ja = C->seg[0].j0, jb = C->seg[C->nseg - 1].j1;
  for (int j = ja; j < jb; j++) {
    const R es = esum[j] + e[j]; /* add_(esum, E, 1) */
    esum[j] = es;
    jlo[j] = jlo[j] * klo[j] + bhlo[j] * es; /* axpby_(jlo, k, bh, esum) */
    jhi[j] = jhi[j] * khi[j] + bhhi[j] * es;
  }
}

/* This step's values of a chunk of a source into val[slot]: sum_k amp[k][p] w[b][k] in k
 * order (`sources.combine`), or the table row. */
static void SFX(src_eval)(const yf_src *q, const yf_item *it, int64_t b, int slot) {
  const int P = q->count, p0 = it->p0, p1 = it->p1;
  double *v = q->val + (size_t)slot * P;
  if (q->k == 0) {
    const double *w = q->w + (size_t)b * P;
    for (int p = p0; p < p1; p++) v[p] = w[p];
    return;
  }
  const double *w = q->w + (size_t)b * q->k;
  const double *a = q->amp;
  const double w0 = w[0];
  for (int p = p0; p < p1; p++) v[p] = a[p] * w0;
  for (int t = 1; t < q->k; t++) {
    const double wt = w[t];
    const double *at = a + (size_t)t * P;
    for (int p = p0; p < p1; p++) v[p] = v[p] + at[p] * wt;
  }
}

/* sub_at(F, idx, scale * v): F[idx] -= (real)(scale * v), sources in list order. */
static void SFX(src_apply)(const yf_sim *s, const yf_run *r, int64_t b, int magnetic) {
  for (int q = 0; q < r->nsrc; q++) {
    const yf_src *S = &r->src[q];
    if ((S->comp >= 3) != magnetic || !S->act[b]) continue;
    R *f = (R *)s->f[S->comp];
    const int64_t *off = S->off;
    const double *sc = S->scale, *v = S->val;
    for (int p = 0; p < S->count; p++) f[off[p]] = f[off[p]] - (R)(sc[p] * v[p]);
  }
}

/* acc.re += outer(cw, v), acc.im += outer(sw, v) on a chunk of a probe. */
static void SFX(probe_item)(const yf_sim *s, const yf_run *r, const yf_item *it,
                            const double *cw, const double *sw, double *v) {
  const yf_probe *Q = &r->probe[it->index];
  const R *f = (const R *)s->f[Q->comp];
  const int P = Q->count, p0 = it->p0, p1 = it->p1;
  for (int p = p0; p < p1; p++) v[p - p0] = (double)f[Q->off[p]];
  for (int m = 0; m < r->m; m++) {
    const double c = cw[m], sn = sw[m];
    double *re = Q->re + (size_t)m * P, *im = Q->im + (size_t)m * P;
    for (int p = p0; p < p1; p++) {
      re[p] = re[p] + c * v[p - p0];
      im[p] = im[p] + sn * v[p - p0];
    }
  }
}

#define SFX_PHASE(N, ...)                                                                       \
  do {                                                                                         \
    const int64_t n_items_ = (N);                                                              \
    for (;;) {                                                                                 \
      const int64_t it = claim(P, base);                                                       \
      if (it >= n_items_) break;                                                               \
      __VA_ARGS__;                                                                             \
    }                                                                                          \
    base += n_items_ + P->n;                                                                   \
    barrier(P, &sense);                                                                        \
  } while (0)

/* ---- sweeps: one step at a time, H then E over the whole box ------------------------------ */

static void SFX(h_item)(const yf_sim *s, int64_t item, int rows, R *scratch, int mumode) {
  const int64_t nrows = (int64_t)s->np * s->ni;
  const int64_t r0 = item * rows, r1 = r0 + rows < nrows ? r0 + rows : nrows;
  for (int64_t r = r0; r < r1; r++) {
    const int k = (int)(r / s->ni), i = (int)(r % s->ni);
    for (int c = 3; c < 6; c++) {
      if (!SFX(in_region)(&s->c[c], k, i)) continue;
      const int e = s->nmu ? SFX(mu_entry)(s, c, k) : -1;
      if (e < 0) {
        SFX(h_row)(s, c, k, i, NULL);
      } else if (mumode == 1) {
        /* no H source this step: blend in place */
        SFX(h_row)(s, c, k, i, scratch);
        SFX(mu_row)(s, e, k, i, scratch);
      } else {
        R *old = (R *)s->mu_old[e] + ((size_t)(k - s->mu_k0[e]) * s->ni + i) * (size_t)s->jp;
        SFX(h_row)(s, c, k, i, old);
      }
    }
  }
}

static void SFX(e_item)(const yf_sim *s, int64_t item, int rows, R *scratch, int sheetmode) {
  const int64_t nrows = (int64_t)s->np * s->ni;
  const int64_t r0 = item * rows, r1 = r0 + rows < nrows ? r0 + rows : nrows;
  for (int64_t r = r0; r < r1; r++) {
    const int k = (int)(r / s->ni), i = (int)(r % s->ni);
    for (int c = 0; c < 3; c++)
      if (SFX(in_region)(&s->c[c], k, i)) SFX(e_row)(s, c, k, i, scratch, sheetmode);
  }
}

/* The mu blend after the sources: item = one row of one entry's planes. */
static void SFX(mu_item)(const yf_sim *s, int64_t item) {
  for (int e = 0; e < s->nmu; e++) {
    const int64_t rows = (int64_t)(s->mu_k1[e] - s->mu_k0[e]) * s->ni;
    if (item >= rows) {
      item -= rows;
      continue;
    }
    const int c = s->mu_comp[e];
    const int k = s->mu_k0[e] + (int)(item / s->ni), i = (int)(item % s->ni);
    if (!SFX(in_region)(&s->c[c], k, i)) return;
    SFX(mu_row)(s, e, k, i, (const R *)s->mu_old[e] + (size_t)item * s->jp);
    return;
  }
}

static void SFX(job_sweep)(yf_pool *P, int tid, const yf_sim *s, const yf_run *r) {
  int64_t base = 0;
  int sense = 0;
  R *scratch = (R *)P->scr[tid];
  double *pv = (double *)((char *)P->scr[tid] + scratch_rows_bytes(s));
  const int rows = r->rows > 0 ? r->rows : 1;
  const int64_t nrows = (int64_t)s->np * s->ni;
  const int64_t nitems = (nrows + rows - 1) / rows;
  int64_t mu_rows = 0;
  for (int e = 0; e < s->nmu; e++) mu_rows += (int64_t)(s->mu_k1[e] - s->mu_k0[e]) * s->ni;
  int64_t hs = 0, es = 0; /* sample counters */
  for (int64_t n = r->n0; n < r->n1; n++) {
    const int64_t b = n - r->n0;
    int hsrc = 0, esrc = 0;
    for (int q = 0; q < r->nsrc; q++)
      if (r->src[q].act[b]) {
        if (r->src[q].comp >= 3)
          hsrc = 1;
        else
          esrc = 1;
      }
    /* H sweep (with the mu blend when no H source acts) */
    SFX_PHASE(nitems, SFX(h_item)(s, it, rows, scratch, hsrc ? 2 : 1));
    if (hsrc) {
      SFX_PHASE(r->nhsitem, {
        const yf_item *x = &r->hsitem[it];
        if (r->src[x->index].act[b]) SFX(src_eval)(&r->src[x->index], x, b, 0);
      });
      if (tid == 0) SFX(src_apply)(s, r, b, 1);
      barrier(P, &sense);
      if (s->nmu) SFX_PHASE(mu_rows, SFX(mu_item)(s, it));
    }
    if (n % r->dec == 0) {
      const double *cw = r->hcos + (size_t)hs * r->m, *sw = r->hsin + (size_t)hs * r->m;
      if (r->nhitem) SFX_PHASE(r->nhitem, SFX(probe_item)(s, r, &r->hitem[it], cw, sw, pv));
      hs++;
    }
    /* E sweep (with the sheet's branches when no E source acts) */
    SFX_PHASE(nitems, SFX(e_item)(s, it, rows, scratch, esrc ? 2 : 1));
    if (esrc) {
      SFX_PHASE(r->nesitem, {
        const yf_item *x = &r->esitem[it];
        if (r->src[x->index].act[b]) SFX(src_eval)(&r->src[x->index], x, b, 0);
      });
      if (tid == 0) SFX(src_apply)(s, r, b, 0);
      barrier(P, &sense);
      if (s->sheet) SFX_PHASE(2 * (int64_t)s->ni, {
          const int c = it < s->ni ? 0 : 1;
          SFX(sheet_row)(s, c, (int)(c == 0 ? it : it - s->ni));
        });
    }
    if ((n + 1) % r->dec == 0) {
      const double *cw = r->ecos + (size_t)es * r->m, *sw = r->esin + (size_t)es * r->m;
      if (r->neitem) SFX_PHASE(r->neitem, SFX(probe_item)(s, r, &r->eitem[it], cw, sw, pv));
      es++;
    }
  }
}

/* ---- wavefront: `tblock` steps per pass over the planes ------------------------------------
 *
 * Phase p of a pass updates, for every step s of the pass, H on plane p - 3s and E on plane
 * p - 1 - 3s, rows handed out dynamically. H(k) reads E(k), E(k + 1) and E(k) reads H(k),
 * H(k - 1), so every value is read at the time level the sweeps would read it: a plane of E is
 * updated after the H of both planes that read its old value, and before the next step's H
 * of those planes (three phases later); the planes touched in one phase are distinct. Each
 * row then gets its sources, mu blend, sheet branches and probe samples right after its
 * update, which is when the sweeps' later passes would reach that row's values. A pass keeps
 * about 3 tblock + 1 planes in cache instead of streaming the box twice per step. */

/* Apply the source entries of one row: F[off] -= (real)(scale * v), in list order. */
ALWAYS void SFX(row_sources)(const yf_sim *s, const yf_run *r, const int64_t *ptr,
                             const yf_item *ent, size_t row, int64_t b, int slot) {
  for (int64_t t = ptr[row]; t < ptr[row + 1]; t++) {
    const yf_src *S = &r->src[ent[t].index];
    if (!S->act[b]) continue;
    const int p = ent[t].p0;
    R *f = (R *)s->f[S->comp] + S->off[p];
    *f = *f - (R)(S->scale[p] * S->val[(size_t)slot * S->count + p]);
  }
}

/* Accumulate the probe entries of one row: re += cw v, im += sw v. */
ALWAYS void SFX(row_probes)(const yf_sim *s, const yf_run *r, const int64_t *ptr,
                            const yf_item *ent, size_t row, const double *cw, const double *sw) {
  for (int64_t t = ptr[row]; t < ptr[row + 1]; t++) {
    const yf_probe *Q = &r->probe[ent[t].index];
    const int p = ent[t].p0, P = Q->count;
    const double v = (double)((const R *)s->f[Q->comp])[Q->off[p]];
    for (int m = 0; m < r->m; m++) {
      double *re = Q->re + (size_t)m * P + p, *im = Q->im + (size_t)m * P + p;
      *re = *re + cw[m] * v;
      *im = *im + sw[m] * v;
    }
  }
}

/* Row (k, i) of H at step n (b = n - n0, slot = its step in the pass), with sources, mu blend
 * and probes. `scratch`: three rows. */
static void SFX(h_full)(const yf_sim *s, const yf_run *r, int k, int i, int64_t n, int slot,
                        R *scratch) {
  const int64_t b = n - r->n0;
  int mu[3] = {-1, -1, -1};
  for (int c = 3; c < 6; c++) {
    if (!SFX(in_region)(&s->c[c], k, i)) continue;
    mu[c - 3] = s->nmu ? SFX(mu_entry)(s, c, k) : -1;
    SFX(h_row)(s, c, k, i, mu[c - 3] >= 0 ? scratch + (size_t)(c - 3) * s->jp : NULL);
  }
  const size_t row = (size_t)k * s->ni + i;
  if (r->hs_ptr) SFX(row_sources)(s, r, r->hs_ptr, r->hs_ent, row, b, slot);
  for (int c = 0; c < 3; c++)
    if (mu[c] >= 0) SFX(mu_row)(s, mu[c], k, i, scratch + (size_t)c * s->jp);
  if (r->hp_ptr && n % r->dec == 0) {
    const int64_t hs = (n + r->dec - 1) / r->dec - (r->n0 + r->dec - 1) / r->dec;
    SFX(row_probes)(s, r, r->hp_ptr, r->hp_ent, row, r->hcos + (size_t)hs * r->m,
                    r->hsin + (size_t)hs * r->m);
  }
}

static void SFX(e_full)(const yf_sim *s, const yf_run *r, int k, int i, int64_t n, int slot,
                        R *scratch) {
  const int64_t b = n - r->n0;
  for (int c = 0; c < 3; c++)
    if (SFX(in_region)(&s->c[c], k, i)) SFX(e_row)(s, c, k, i, scratch, 2);
  const size_t row = (size_t)k * s->ni + i;
  if (r->es_ptr) SFX(row_sources)(s, r, r->es_ptr, r->es_ent, row, b, slot);
  if (s->sheet && k == s->kc) {
    SFX(sheet_row)(s, 0, i);
    SFX(sheet_row)(s, 1, i);
  }
  if (r->ep_ptr && (n + 1) % r->dec == 0) {
    const int64_t es = (n + r->dec) / r->dec - (r->n0 + r->dec) / r->dec;
    SFX(row_probes)(s, r, r->ep_ptr, r->ep_ent, row, r->ecos + (size_t)es * r->m,
                    r->esin + (size_t)es * r->m);
  }
}

static void SFX(job_wave)(yf_pool *P, int tid, const yf_sim *s, const yf_run *r) {
  int64_t base = 0;
  int sense = 0;
  R *scratch = (R *)P->scr[tid];
  const int T = r->tblock;
  const int np = s->np, ni = s->ni;
  const int rows = r->rows > 0 ? r->rows : 1;
  const int nchunk = (ni + rows - 1) / rows;
  for (int64_t n0 = r->n0; n0 < r->n1; n0 += T) {
    const int S = (int)(r->n1 - n0 < T ? r->n1 - n0 : T);
    /* this pass's source values */
    int any = 0;
    for (int q = 0; q < r->nsrc && !any; q++)
      for (int t = 0; t < S; t++)
        if (r->src[q].act[n0 + t - r->n0]) any = 1;
    if (any)
      SFX_PHASE((int64_t)r->nsitem * S, {
        const yf_item *x = &r->sitem[it % r->nsitem];
        const int t = (int)(it / r->nsitem);
        const int64_t b = n0 + t - r->n0;
        if (r->src[x->index].act[b]) SFX(src_eval)(&r->src[x->index], x, b, t);
      });
    const int nphase = np + 3 * (S - 1) + 1;
    for (int p = 0; p < nphase; p++) {
      /* tasks of this phase: (step t, H or E, plane) */
      int tk[2 * YF_MAX_TBLOCK], tt[2 * YF_MAX_TBLOCK], te[2 * YF_MAX_TBLOCK], ntask = 0;
      for (int t = 0; t < S; t++) {
        const int kh = p - 3 * t, ke = p - 1 - 3 * t;
        if (kh >= 0 && kh < np) {
          tk[ntask] = kh;
          tt[ntask] = t;
          te[ntask++] = 0;
        }
        if (ke >= 0 && ke < np) {
          tk[ntask] = ke;
          tt[ntask] = t;
          te[ntask++] = 1;
        }
      }
      SFX_PHASE((int64_t)ntask * nchunk, {
        const int task = (int)(it / nchunk), ch = (int)(it % nchunk);
        const int k = tk[task], t = tt[task];
        const int i1 = (ch + 1) * rows < ni ? (ch + 1) * rows : ni;
        for (int i = ch * rows; i < i1; i++) {
          if (te[task])
            SFX(e_full)(s, r, k, i, n0 + t, t, scratch);
          else
            SFX(h_full)(s, r, k, i, n0 + t, t, scratch);
        }
      });
    }
  }
}

static void SFX(job)(yf_pool *P, int tid, const yf_sim *s, const yf_run *r) {
  if (r->tblock >= 1)
    SFX(job_wave)(P, tid, s, r);
  else
    SFX(job_sweep)(P, tid, s, r);
}

#undef SFX_HCALL
#undef SFX_HSW
#undef SFX_ECALL
#undef SFX_ESWQ
#undef SFX_ESW
#undef SFX_PHASE
