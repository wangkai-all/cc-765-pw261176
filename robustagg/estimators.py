"""Robust centre estimators and the shared summary/contribution types.

The unit of analysis is a *summary*: a mean (or a vector of metric means), the
effective count, the supported range and the batch it arrived in.  A corrupt
summary may carry an arbitrary mean and an inflated count, so nothing here
rewrites a summary; estimators return the exact support set they used, with
per-fragment weights, so the UI can trace any number back to receipts.

Only ``math`` and the standard library are used.  ``SplitMix64`` makes seeded
simulations reproducible across Python releases.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

ESTIMATORS = ("naive", "naive_uncapped", "trimmed", "huber", "median")
ESTIMATOR_LABELS = {
    "naive": "封顶加权",
    "naive_uncapped": "直接按上报量加权",
    "trimmed": "截尾汇总",
    "huber": "Huber M 估计",
    "median": "加权中位数",
}
ESTIMATOR_SHORT = {
    "naive": "封顶加权",
    "naive_uncapped": "直接加权",
    "trimmed": "截尾",
    "huber": "Huber",
    "median": "中位数",
}


class SplitMix64(random.Random):
    """Deterministic RNG, stable across Python releases."""

    def __init__(self, seed: int):
        super().__init__()
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def _next64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def random(self) -> float:
        return (self._next64() >> 11) * (2.0**-53)

    def getrandbits(self, k: int) -> int:
        return self._next64() >> (64 - k)

    def randrange(self, n: int) -> int:
        if n <= 0:
            raise ValueError("empty range")
        return self._next64() % n


@dataclass(frozen=True)
class Summary:
    """One aggregate report for one metric and period.

    ``summary_id`` identifies the fragment; a correction creates a new row that
    names the fragment it replaces in ``revision_of``.  The store resolves the
    active fragment set; this type only describes one receipt.
    """

    summary_id: str
    unit_id: str
    batch_id: str
    metric: str
    period: str
    mean: float
    std: float
    n_effective: float
    range_low: float
    range_high: float
    reported_total: float | None = None
    revision_of: str | None = None

    @property
    def variance(self) -> float:
        if self.std <= 0 or self.n_effective <= 0:
            return 0.0
        return (self.std * self.std) / self.n_effective

    def as_dict(self):
        return {
            "summary_id": self.summary_id,
            "unit_id": self.unit_id,
            "batch_id": self.batch_id,
            "metric": self.metric,
            "period": self.period,
            "mean": self.mean,
            "std": self.std,
            "n_effective": self.n_effective,
            "range_low": self.range_low,
            "range_high": self.range_high,
            "reported_total": self.reported_total,
            "revision_of": self.revision_of,
        }


@dataclass
class Contribution:
    """One active fragment as seen by the estimator."""

    summary: Summary
    unit_id: str
    weight: float
    claimed_weight: float
    unit_claim: float
    unit_weight: float
    capped: bool
    split: bool
    isolated: bool = False
    isolation_reasons: list[str] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return self.summary.mean


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float:
    items = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in items)
    if total <= 0:
        return math.nan
    half = total / 2.0
    cum = 0.0
    for v, w in items:
        cum += w
        if cum >= half:
            return v
    return items[-1][0]


def madn(values: Sequence[float], weights: Sequence[float] | None = None) -> float:
    if not values:
        return 0.0
    pairs = list(zip(values, weights)) if weights else [(v, 1.0) for v in values]
    med = weighted_median(pairs)
    dev = [abs(v - med) for v in values]
    devp = list(zip(dev, weights)) if weights else [(d, 1.0) for d in dev]
    return 1.4826 * weighted_median(devp)


def naive_center(pairs: Sequence[tuple[int, float, float, float]]) -> dict:
    total = sum(w for _, w, _, _ in pairs)
    if total <= 0:
        return {"center": math.nan, "support": []}
    center = sum(w * v for _, w, v, _ in pairs) / total
    return {"center": center, "support": [(i, w, v, False) for i, w, v, _ in pairs]}


def trimmed_center(pairs: Sequence[tuple[int, float, float, float]], alpha: float) -> dict:
    """Symmetric alpha-trimmed weighted mean.

    A mass ``alpha * W`` is removed from each tail of the value order.  A unit
    straddling the trim boundary keeps only its surviving mass (the support
    records ``partial``).  Extreme summaries of either sign lose influence, and
    legitimate units that happen to sit near the centre keep all of theirs.
    """
    alpha = min(max(alpha, 0.0), 0.49)
    items = sorted(pairs, key=lambda t: t[2])
    total = sum(w for _, w, _, _ in items)
    if total <= 0:
        return {"center": math.nan, "support": []}
    lo, hi = alpha * total, total - alpha * total
    support = []
    cum = num = den = 0.0
    for idx, w, v, _se in items:
        start, end = cum, cum + w
        cum = end
        overlap = max(0.0, min(end, hi) - max(start, lo))
        if overlap <= 0:
            continue
        num += overlap * v
        den += overlap
        support.append((idx, overlap, v, overlap < w - 1e-12))
    return {"center": (num / den if den > 0 else math.nan), "support": support}


def huber_center(pairs: Sequence[tuple[int, float, float, float]], *, efficiency: float = 0.95,
                 max_iter: int = 200, tol: float = 1e-11) -> dict:
    """Huber M-estimator, iteratively reweighted, weights = effective weight.

    Scale is the weighted MAD of unit means blended with each summary's own
    standard error, so a summary claiming zero spread cannot collapse it.
    """
    if not pairs:
        return {"center": math.nan, "support": [], "k": 1.345, "scale": 0.0}
    med = weighted_median([(v, w) for _, w, v, _ in pairs])
    s = madn([v for _, _, v, _ in pairs], [w for _, w, _, _ in pairs])
    k = 1.345 if efficiency >= 0.95 else (1.645 if efficiency >= 0.90 else 2.0)
    floor = 1e-9 * (1.0 + abs(med))
    s = max(s, floor)
    center = med
    for _ in range(max_iter):
        num = den = 0.0
        for _, w, v, se in pairs:
            local = math.sqrt(max(se * se, floor * floor) + s * s)
            u = (v - center) / local
            psi = max(-k, min(k, u))
            num += w * psi * local
            den += w
        if den <= 0:
            break
        nxt = center + num / den
        if abs(nxt - center) <= tol * (1.0 + abs(center)):
            center = nxt
            break
        center = nxt
    return {
        "center": center,
        "support": [(i, w, v, False) for i, w, v, _ in pairs],
        "k": k,
        "scale": s,
    }


def median_center(pairs: Sequence[tuple[int, float, float, float]]) -> dict:
    center = weighted_median([(v, w) for _, w, v, _ in pairs])
    return {"center": center, "support": [(i, w, v, False) for i, w, v, _ in pairs]}


def run_estimator(name: str, pairs, *, alpha: float, efficiency: float = 0.95) -> dict:
    if name in ("naive", "naive_uncapped"):
        return naive_center(pairs)
    if name == "trimmed":
        return trimmed_center(pairs, alpha)
    if name == "huber":
        return huber_center(pairs, efficiency=efficiency)
    if name == "median":
        return median_center(pairs)
    raise ValueError(f"unknown estimator {name!r}")
