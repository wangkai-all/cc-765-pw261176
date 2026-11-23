"""Weighted quantiles and trimmed means."""

from __future__ import annotations

import math
from typing import Sequence


def weighted_quantile(pairs: Sequence[tuple[float, float]], q: float) -> float:
    """Weighted quantile, ``q`` in [0,1]; first value whose cumulative reaches q."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0,1]")
    items = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in items)
    if total <= 0:
        return math.nan
    target = q * total
    cum = 0.0
    for v, w in items:
        cum += w
        if cum >= target:
            return v
    return items[-1][0]


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float:
    return weighted_quantile(pairs, 0.5)


def weighted_trimmed_mean(pairs: Sequence[tuple[float, float]], trim: float) -> float:
    """Mean after removing ``trim * total`` mass from each tail."""
    items = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in items)
    if total <= 0:
        return math.nan
    lo, hi = trim * total, total - trim * total
    num = den = cum = 0.0
    for v, w in items:
        start, end = cum, cum + w
        cum = end
        ov = max(0.0, min(end, hi) - max(start, lo))
        if ov > 0:
            num += ov * v
            den += ov
    return num / den if den > 0 else math.nan
