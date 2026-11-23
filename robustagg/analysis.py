"""One analysis run: weights, estimates, bounds, split detection, influence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .bounds import trimmed_bound
from .quantiles import weighted_quantile
from .estimators import (
    Contribution,
    ESTIMATORS,
    ESTIMATOR_LABELS,
    Summary,
    run_estimator,
)


@dataclass
class AnalysisConfig:
    metric: str
    period: str
    contamination_rate: float = 0.10  # alpha: max share of weight assumed bad
    max_share: float = 0.25  # weight cap per unit as a share of total
    estimator: str = "trimmed"
    efficiency: float = 0.95

    def as_dict(self):
        return {
            "metric": self.metric,
            "period": self.period,
            "contamination_rate": self.contamination_rate,
            "max_share": self.max_share,
            "estimator": self.estimator,
            "efficiency": self.efficiency,
        }

    def validated(self) -> "AnalysisConfig":
        if not 0.0 < self.contamination_rate < 0.5:
            raise ValueError("污染比例 α 必须在 (0, 0.5) 之间")
        if not 0.0 < self.max_share <= 0.5:
            raise ValueError("单单位权重上限必须在 (0, 0.5] 之间")
        if self.estimator not in ESTIMATORS:
            raise ValueError(f"未知估计量 {self.estimator!r}")
        return self


def cap_feasible(claims: Sequence[float], max_share: float) -> bool:
    """Can any positive cap satisfy ``c = max_share * sum(min(w_i, c))``?

    Writing ``h(c) = max_share * sum(min(w_i, c)) / c`` (non-increasing), a
    positive solution exists iff ``h(0+) >= 1 >= h(max)``, i.e.
    ``max_share * k >= 1`` and ``max(claims) >= max_share * sum(claims)``.
    With five units, a 10% cap is arithmetically impossible; saying so is
    better than capping everyone to nothing.
    """
    positive = [w for w in claims if w > 0]
    if not positive:
        return False
    total = sum(positive)
    # At c = max(claims) no cap binds, so the reachable maximum share is
    # max/total; a share ceiling above it cannot be exceeded and needs no cap.
    if max(positive) <= max_share * total + 1e-12:
        return True
    # Below that, capping binds and the split-proof equation has a root iff
    # the share ceiling can be met at all, i.e. k * max_share >= 1.
    return len(positive) * max_share >= 1.0 - 1e-12


def resolve_cap(claims: Sequence[float], max_share: float) -> float:
    """Largest ``c`` with ``c = max_share * sum(min(w_i, c))``.

    The cap is expressed as a share of the *final* weight total, so the
    guarantee ("no unit holds more than ``max_share`` of the weight actually
    used") holds exactly, however much the other units' claims are capped.

    When no positive cap can satisfy the equation the unit share ceiling is
    unreachable for this panel; the function returns the largest claim (no
    cap applied) and ``cap_feasible`` reports the failure so the bound can
    void its guarantee instead of silently emitting near-zero weights.
    """
    positive = [w for w in claims if w > 0]
    if not positive:
        return 0.0
    if not cap_feasible(positive, max_share):
        return max(positive)
    lo, hi = 0.0, max(positive)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if mid - max_share * sum(min(w, mid) for w in positive) > 0:
            hi = mid          # cap smaller than its own share of the total
        else:
            lo = mid          # cap equal to / larger than its share
    return (lo + hi) / 2.0


def build_contributions(
    summaries: Sequence[Summary],
    *,
    config: AnalysisConfig,
    isolated_units: set[str] | None = None,
    isolated_batches: set[str] | None = None,
) -> list[Contribution]:
    """Apply the split-proof weight cap.

    The cap is resolved at the *unit* level against the final weight total:
    unit claims are pooled, a cap ``c`` satisfying ``c = max_share * sum(min(w,c))``
    is found by bisection, and each unit receives ``min(claim, c)``.  The capped
    unit weight is then split across its fragments in proportion to their
    claimed counts, so splitting a report across batches cannot buy more weight
    than a single report would have.  (A correction adds no fragment at all:
    the store removes the superseded row from the active set.)
    """
    isolated_units = isolated_units or set()
    isolated_batches = isolated_batches or set()

    by_unit: dict[str, list[Summary]] = {}
    for s in summaries:
        by_unit.setdefault(s.unit_id, []).append(s)

    units = list(by_unit)
    claims = {u: sum(f.n_effective for f in by_unit[u]) for u in units}
    cap = resolve_cap(list(claims.values()), config.max_share)

    out: list[Contribution] = []
    for unit in units:
        fragments = by_unit[unit]
        claimed = claims[unit]
        unit_weight = min(cap, claimed) if claimed > 0 else 0.0
        capped = claimed > unit_weight + 1e-12
        split = len(fragments) > 1 or len({f.batch_id for f in fragments}) > 1
        for f in fragments:
            share = (f.n_effective / claimed) if claimed > 0 else 1.0 / len(fragments)
            reasons = []
            if unit in isolated_units:
                reasons.append("单位被隔离")
            if f.batch_id in isolated_batches:
                reasons.append("批次被隔离")
            out.append(
                Contribution(
                    summary=f,
                    unit_id=unit,
                    weight=unit_weight * share,
                    claimed_weight=f.n_effective,
                    unit_claim=claimed,
                    unit_weight=unit_weight,
                    capped=capped,
                    split=split,
                    isolated=bool(reasons),
                    isolation_reasons=reasons,
                )
            )
    return out


def bad_weight_share(
    summaries: Sequence[Summary],
    *,
    config: AnalysisConfig,
    bad_units: set[str],
    isolated_units: set[str] | None = None,
) -> float:
    """Share of estimator weight held by known-bad units (generated data only)."""
    contribs = build_contributions(summaries, config=config, isolated_units=isolated_units)
    used = [c for c in contribs if not c.isolated]
    total = sum(c.weight for c in used)
    if total <= 0:
        return 0.0
    return sum(c.weight for c in used if c.unit_id in bad_units) / total


def _support(used: Sequence[Contribution], raw: dict) -> list[dict]:
    out = []
    for (i, w, v, partial) in raw["support"]:
        if i >= len(used):
            continue
        c = used[i]
        out.append(
            {
                "summary_id": c.summary.summary_id,
                "unit_id": c.unit_id,
                "batch_id": c.summary.batch_id,
                "period": c.summary.period,
                "weight": w,
                "claimed_weight": c.claimed_weight,
                "value": v,
                "partial": bool(partial),
                "capped": c.capped,
                "split": c.split,
            }
        )
    return out


def analyze(
    summaries: Sequence[Summary],
    *,
    config: AnalysisConfig,
    isolated_units: set[str] | None = None,
    isolated_batches: set[str] | None = None,
    truth: float | None = None,
    known_bad_share: float | None = None,
    bad_units: set[str] | None = None,
) -> dict:
    """Run every estimator on the active fragments.

    ``truth`` exists only for generated data; without it the UI shows no error
    column and no "correct answer" line.
    """
    config.validated()
    isolated_units = set(isolated_units or set())
    isolated_batches = set(isolated_batches or set())
    if bad_units and known_bad_share is None:
        known_bad_share = bad_weight_share(
            summaries, config=config, bad_units=bad_units, isolated_units=isolated_units
        )

    contribs = build_contributions(
        summaries, config=config, isolated_units=isolated_units, isolated_batches=isolated_batches
    )
    used = [c for c in contribs if not c.isolated]
    total_w = sum(c.weight for c in used)
    total_claimed_used = sum(c.claimed_weight for c in used)

    pairs = [(c.summary.mean, c.weight) for c in used]
    triples = [(i, c.weight, c.summary.mean, math.sqrt(c.summary.variance)) for i, c in enumerate(used)]
    triples_uncapped = [
        (i, c.claimed_weight, c.summary.mean, math.sqrt(c.summary.variance)) for i, c in enumerate(used)
    ]

    estimates = {}
    for est in ESTIMATORS:
        src = triples_uncapped if est == "naive_uncapped" else triples
        raw = run_estimator(est, src, alpha=config.contamination_rate, efficiency=config.efficiency)
        support = _support(used, raw)
        estimates[est] = {
            "center": raw["center"],
            "label": ESTIMATOR_LABELS[est],
            "support": support,
            "partial_trim": any(s["partial"] for s in support),
        }

    max_unit_weight = max((c.unit_weight for c in used), default=0.0)
    actual_max_share = (max_unit_weight / total_w) if total_w > 0 else 0.0
    feasible = cap_feasible(
        [c.unit_claim for c in used], config.max_share
    )
    bound = trimmed_bound(
        pairs,
        config.contamination_rate,
        max_share=actual_max_share,
        n_units=len({c.unit_id for c in used}),
        known_bad_share=known_bad_share,
        cap_feasible=feasible,
        cap_requested=config.max_share,
    )
    bound["cap_share_actual"] = actual_max_share
    bound["cap_feasible"] = feasible

    units = sorted({c.unit_id for c in used})
    capped_units = sorted({c.unit_id for c in used if c.capped})
    split_units = sorted({c.unit_id for c in used if c.split})

    return {
        "config": config.as_dict(),
        "estimates": estimates,
        "chosen": config.estimator,
        "truth": truth,
        "errors": {
            est: (abs(v["center"] - truth) if truth is not None and math.isfinite(v["center"]) else None)
            for est, v in estimates.items()
        },
        "weighting": {
            "units_reporting": len(units),
            "fragments": len(used),
            "fragments_ignored": len(contribs) - len(used),
            "total_weight": total_w,
            "total_claimed": total_claimed_used,
            "cap": max((c.unit_weight for c in used), default=0.0),
            "cap_share": config.max_share,
            "cap_share_actual": (max((c.unit_weight for c in used), default=0.0) / total_w) if total_w else 0.0,
            "removed_by_cap": total_claimed_used - total_w,
            "cap_binding": bool(capped_units),
            "capped_units": capped_units,
            "split_units": split_units,
            "isolated_units": sorted(isolated_units),
            "isolated_batches": sorted(isolated_batches),
        },
        "bound": {**bound, "center": estimates["trimmed"]["center"]},
        "contributions": [
            {
                "summary_id": c.summary.summary_id,
                "unit_id": c.unit_id,
                "batch_id": c.summary.batch_id,
                "mean": c.summary.mean,
                "std": c.summary.std,
                "n_effective": c.summary.n_effective,
                "range_low": c.summary.range_low,
                "range_high": c.summary.range_high,
                "claimed_weight": c.claimed_weight,
                "weight": c.weight,
                "capped": c.capped,
                "split": c.split,
                "isolated": c.isolated,
                "isolation_reasons": c.isolation_reasons,
                "known_bad": bool(bad_units and c.unit_id in bad_units),
            }
            for c in contribs
        ],
    }


def unit_influence(
    summaries: Sequence[Summary],
    *,
    config: AnalysisConfig,
    isolated_units: set[str],
    unit_id: str,
    isolated_batches: set[str] | None = None,
    truth: float | None = None,
    bad_units: set[str] | None = None,
) -> dict:
    """What changes if this unit is isolated (or restored)?"""
    base = analyze(summaries, config=config, isolated_units=isolated_units,
                   isolated_batches=isolated_batches, truth=truth, bad_units=bad_units)
    dropped = analyze(summaries, config=config, isolated_units=set(isolated_units) | {unit_id},
                      isolated_batches=isolated_batches, truth=truth, bad_units=bad_units)
    est = config.estimator
    delta = None
    if math.isfinite(base["estimates"][est]["center"]) and math.isfinite(dropped["estimates"][est]["center"]):
        delta = dropped["estimates"][est]["center"] - base["estimates"][est]["center"]
    gap_with = base["estimates"]["naive"]["center"] - base["estimates"]["trimmed"]["center"]
    gap_without = dropped["estimates"]["naive"]["center"] - dropped["estimates"]["trimmed"]["center"]
    fragments = [c for c in base["contributions"] if c["unit_id"] == unit_id]
    weight = sum(f["weight"] for f in fragments)
    total = base["weighting"]["total_weight"]
    offset = base["bound"]["offset"]
    # Same rule as ``compare``: a void bound supplies no scale for "did this
    # move more than the assumption allows", so the judgement is withheld.
    judgeable = delta is not None and math.isfinite(offset) and base["bound"]["status"] != "void"
    return {
        "unit_id": unit_id,
        "estimator": est,
        "center_with": base["estimates"][est]["center"],
        "center_without": dropped["estimates"][est]["center"],
        "delta": delta,
        "weight": weight,
        "claimed_weight": sum(f["claimed_weight"] for f in fragments),
        "weight_share": (weight / total) if total else None,
        "at_cap": bool(fragments) and all(f["capped"] for f in fragments),
        "fragments": fragments,
        "gap_with": gap_with,
        "gap_without": gap_without,
        "gap_delta": (gap_without - gap_with) if math.isfinite(gap_with) and math.isfinite(gap_without) else None,
        "within_bound": (abs(delta) <= offset) if judgeable else None,
        "bound_with": base["bound"],
        "bound_without": dropped["bound"],
        "is_isolated": unit_id in isolated_units,
    }


def compare(
    summaries: Sequence[Summary],
    *,
    config: AnalysisConfig,
    isolated_units: set[str] | None = None,
    isolated_batches: set[str] | None = None,
    truth: float | None = None,
    bad_units: set[str] | None = None,
) -> dict:
    """Contrast naive with robust and say whether the gap is explainable."""
    res = analyze(summaries, config=config, isolated_units=isolated_units,
                  isolated_batches=isolated_batches, truth=truth, bad_units=bad_units)
    e = res["estimates"]
    naive, trimmed = e["naive"]["center"], e["trimmed"]["center"]
    gap = (naive - trimmed) if math.isfinite(naive) and math.isfinite(trimmed) else None
    bound = res["bound"]
    offset = bound["offset"]
    # A void bound still has a numeric half-width, but it carries no guarantee.
    # Comparing the gap against it would read as "explained by the assumption"
    # on the very screen that says the guarantee lapsed, so the comparison is
    # unavailable rather than favourable.
    explainable = gap is not None and math.isfinite(offset) and bound["status"] != "void"
    return {
        "naive": naive,
        "naive_uncapped": e["naive_uncapped"]["center"],
        "trimmed": trimmed,
        "median": e["median"]["center"],
        "huber": e["huber"]["center"],
        "gap": gap,
        "offset": offset,
        "explained_by_assumption": (
            (abs(gap) <= offset) if explainable else None
        ),
        "truth": truth,
        "errors": res["errors"],
        "support_naive": e["naive"]["support"],
        "support_trimmed": e["trimmed"]["support"],
        "bound_status": res["bound"]["status"],
        "weighting": res["weighting"],
    }


def quantile_curve(summaries: Sequence[Summary], *, config: AnalysisConfig,
                   isolated_units: set[str] | None = None):
    """Empirical CDF of values, weighted by unit-capped weight."""
    contribs = build_contributions(summaries, config=config, isolated_units=isolated_units)
    used = sorted((c for c in contribs if not c.isolated), key=lambda c: c.summary.mean)
    total = sum(c.weight for c in used) or 1.0
    curve = []
    cum = 0.0
    for c in used:
        cum += c.weight
        curve.append(
            {
                "unit_id": c.unit_id,
                "summary_id": c.summary.summary_id,
                "value": c.summary.mean,
                "batch_id": c.summary.batch_id,
                "weight": c.weight,
                "claimed_weight": c.claimed_weight,
                "cum_share": cum / total,
                "capped": c.capped,
            }
        )
    alpha = config.contamination_rate
    pairs = [(c.summary.mean, c.weight) for c in used]
    marks = [
        {"q": q, "value": weighted_quantile(pairs, q), "label": lab}
        for q, lab in ((alpha, "下截尾线"), (0.5, "中位数"), (1 - alpha, "上截尾线"))
    ]
    return {"curve": curve, "marks": marks, "alpha": alpha, "total_weight": total}
