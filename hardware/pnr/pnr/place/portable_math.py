"""Platform-independent exp/log and Adam for global placement (pnr.place.model).

PyTorch's ``exp``/``log`` (and ``softmax``, ``sigmoid``, ``logsumexp``'s backward, which
call them) use each platform's vector math library: the Linux arm64 wheel and the macOS
arm64 wheel of the same PyTorch version return different last bits for the same float32
input. Global placement runs hundreds of Adam steps on a loss built from these, and the
iteration amplifies a one-ulp difference into a different placement (14 of 20 parts
moved, by up to 3 mm, and two rotations on ``08-chaser-20-plane`` seed 0), so one seed
gave one board on the Mac and another on the nightly CI's Linux runner, where that rung
failed.

The functions here evaluate exp and log from IEEE-754 basic operations only (add,
multiply, divide, round, comparisons and bit assembly of powers of two), each a separate
correctly rounded tensor kernel, in float64, then round once to the input's dtype. The
same input therefore gives the same bits on every platform whose kernels are IEEE-754
(no fused multiply-add can form across two kernel calls) and whose sums reduce in the same
order: every arm64 platform the engine's torch wheels cover (darwin-arm64, linux-aarch64;
tests/test_portable_math.py holds their shared golden bits). The polynomials are only as
long as float32 needs: relative error below 1e-11 (float32 resolution is 6e-8), so the
result is torch's to the last float32 bit almost always, and the same everywhere always.

Gradients are analytic (``d exp = exp``, ``d log = 1/x``) and use the same kernels.
"""

from __future__ import annotations

import math

import torch

_LN2_HI = 6.93147180369123816490e-01  # fdlibm split of ln 2: k * _LN2_HI is exact
_LN2_LO = 1.90821492927058770002e-10
_INV_LN2 = 1.0 / math.log(2.0)
_SQRT2 = math.sqrt(2.0)
_EXP_MIN = -708.0  # 2**k stays a normal float64 for k >= -1021
_EXP_MAX = 709.0
# Taylor coefficients 1/k! for |r| <= ln2/2: degree 9 leaves < 1e-11 relative error.
_EXP_COEFFS = [1.0 / math.factorial(k) for k in range(10)]
# log(m) = 2 atanh(f), f = (m - 1)/(m + 1), |f| <= 0.1716: odd terms 1/(2k+1) to f**13
# (< 1e-12 absolute).
_LOG_COEFFS = [1.0 / (2 * k + 1) for k in range(7)]
_MANTISSA = (1 << 52) - 1
_ONE_BITS = 1023 << 52


def _pow2(k: torch.Tensor) -> torch.Tensor:
    """2**k (int64 k in [-1022, 1023]) assembled from its bits: exact everywhere."""
    return ((k + 1023) << 52).view(torch.float64)


def _exp64(x: torch.Tensor) -> torch.Tensor:
    x = torch.clamp(x, _EXP_MIN, _EXP_MAX)
    k = torch.round(x * _INV_LN2)
    r = (x - k * _LN2_HI) - k * _LN2_LO
    poly = torch.full_like(r, _EXP_COEFFS[-1])
    for c in reversed(_EXP_COEFFS[:-1]):
        poly = poly * r + c
    # Two steps keep 2**k normal at the clamp's ends (k in [-1022, 1023]).
    k = k.to(torch.int64)
    half = torch.div(k, 2, rounding_mode="floor")
    return poly * _pow2(half) * _pow2(k - half)


def _log64(x: torch.Tensor) -> torch.Tensor:
    """log of positive normal float64 values (zero, subnormals: -inf / unsupported)."""
    bits = x.contiguous().view(torch.int64)
    e = (bits >> 52) - 1023
    m = ((bits & _MANTISSA) | _ONE_BITS).view(torch.float64)  # in [1, 2)
    big = m > _SQRT2
    m = torch.where(big, m * 0.5, m)
    e = e + big.to(torch.int64)
    f = (m - 1.0) / (m + 1.0)
    f2 = f * f
    poly = torch.full_like(f, _LOG_COEFFS[-1])
    for c in reversed(_LOG_COEFFS[:-1]):
        poly = poly * f2 + c
    ef = e.to(torch.float64)
    out = (2.0 * f * poly + ef * _LN2_LO) + ef * _LN2_HI
    out = torch.where(x > 0.0, out, torch.full_like(out, -math.inf))
    return torch.where(torch.isinf(x) & (x > 0), x, out)


class _Exp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        y = _exp64(x.double()).to(x.dtype)
        ctx.save_for_backward(y)
        return y

    @staticmethod
    def backward(ctx, grad):
        (y,) = ctx.saved_tensors
        return grad * y


class _Log(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.save_for_backward(x)
        return _log64(x.double()).to(x.dtype)

    @staticmethod
    def backward(ctx, grad):
        (x,) = ctx.saved_tensors
        return grad / x


def exp(x: torch.Tensor) -> torch.Tensor:
    """``torch.exp`` with the same bits on every platform."""
    return _Exp.apply(x)


def log(x: torch.Tensor) -> torch.Tensor:
    """``torch.log`` (positive inputs) with the same bits on every platform."""
    return _Log.apply(x)


def logsumexp(x: torch.Tensor, dim: int) -> torch.Tensor:
    """``torch.logsumexp(x, dim)``; the gradient is the softmax, as torch's."""
    peak = torch.amax(x, dim=dim, keepdim=True).detach()
    peak = torch.where(torch.isfinite(peak), peak, torch.zeros_like(peak))
    return log(exp(x - peak).sum(dim=dim, keepdim=True)).squeeze(dim) + peak.squeeze(dim)


def softmax(x: torch.Tensor, dim: int) -> torch.Tensor:
    """``torch.softmax(x, dim)``."""
    e = exp(x - torch.amax(x, dim=dim, keepdim=True).detach())
    return e / e.sum(dim=dim, keepdim=True)


def sigmoid(x: torch.Tensor) -> torch.Tensor:
    """``torch.sigmoid(x)``; saturated (zero gradient) beyond ``|x| = 80``, where float32
    sigmoid is already 0 or 1."""
    return 1.0 / (1.0 + exp(-torch.clamp(x, -80.0, 80.0)))


# cos/sin of the four placement angles, exact (torch.cos(deg2rad(90)) is -4.4e-8 in
# float32, and its last bits are the platform's).
QUARTER_TURNS = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}


def quarter_turn(angle: float):
    """(cos, sin) of a multiple of 90 degrees, exactly."""
    key = int(round(float(angle))) % 360
    if key not in QUARTER_TURNS or abs(float(angle) - round(float(angle))) > 1e-9:
        raise ValueError("not a quarter turn: %r" % (angle,))
    return QUARTER_TURNS[key]


class Adam(torch.optim.Optimizer):
    """``torch.optim.Adam`` (no weight decay, no amsgrad) from one-operation kernels.

    torch's Adam updates its moments with ``lerp_`` and ``addcmul_`` and the parameter
    with ``addcdiv_``: compound kernels that a platform's compiler may contract into a
    fused multiply-add or not, so the Linux and macOS wheels drift apart from the
    second step on. Here every kernel is a single IEEE-754 operation (multiply, add,
    divide, square root), the same update in the same order on every platform."""

    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8):
        super().__init__(params, dict(lr=lr, betas=betas, eps=eps))

    @torch.no_grad()
    def step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        for group in self.param_groups:
            beta1, beta2 = group["betas"]
            for p in group["params"]:
                if p.grad is None:
                    continue
                g = p.grad
                state = self.state[p]
                if not state:
                    state["step"] = 0
                    state["m"] = torch.zeros_like(p)
                    state["v"] = torch.zeros_like(p)
                state["step"] += 1
                t = state["step"]
                m = torch.add(torch.mul(state["m"], beta1), torch.mul(g, 1.0 - beta1))
                v = torch.add(torch.mul(state["v"], beta2), torch.mul(torch.mul(g, g), 1.0 - beta2))
                state["m"], state["v"] = m, v
                step_size = group["lr"] / (1.0 - beta1**t)
                bc2_sqrt = math.sqrt(1.0 - beta2**t)
                denom = torch.add(torch.div(torch.sqrt(v), bc2_sqrt), group["eps"])
                p.sub_(torch.mul(torch.div(m, denom), step_size))
        return loss
