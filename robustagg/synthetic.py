"""Synthetic ground-truth panel generator.

Everything here is fabricated.  The generator exists so the estimator can be
judged against a known truth: units differ genuinely (heterogeneity), a chosen
share of *claimed weight* is corrupted, corruptions can be coordinated, and a
unit can inflate its effective count without moving its mean.

Each scenario names the claimed bad weight share, so the demo can sit well
inside the alpha guarantee, at its edge, or beyond it.  The analysis applies
the cap first; the assumption ledger reports the share actually used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .estimators import SplitMix64, Summary


PRIMARY_METRIC = "指标值"
SECONDARY_METRIC = "伴随指标"


@dataclass
class UnitTruth:
    unit_id: str
    group: str
    true_mean: float
    true_std: float
    true_n: int
    batch_id: str
    is_bad: bool = False
    bad_kind: str | None = None  # "mean_shift" | "count_inflation" | "both"
    corruption: dict = field(default_factory=dict)
    true_means: dict = field(default_factory=dict)
    secondary_mean: float = 0.0
    secondary_std: float = 0.0


@dataclass
class GeneratedPanel:
    name: str
    seed: int
    metric: str
    period: str
    truth: float
    units: list[UnitTruth]
    # ``summaries`` holds the primary component only: the estimators and the
    # simulation consume one component at a time.  ``all_summaries`` carries
    # the whole mean vector and is what the demo loader stores.
    summaries: list[Summary]
    metadata: dict
    metrics: list[str] = field(default_factory=list)
    truth_by_metric: dict = field(default_factory=dict)
    all_summaries: list[Summary] = field(default_factory=list)


SCENARIOS = {
    "baseline": dict(
        label="基线：单位异质、无污染",
        description="真实单位均值本身就有差异；验证稳健估计不把合法异质性当坏数据。",
        n_units=24, bad_weight_share=0.0, bad_kind="mean_shift", spread=4.0,
        shift_multiplier=0.0, concentration=1.0, bad_units=0,
    ),
    "independent": dict(
        label="独立损坏摘要",
        description="少数单位的均值被拉向两端，总权重约占上限的一半；检验截尾收益。",
        n_units=24, bad_weight_share=0.05, bad_kind="mean_shift", spread=4.0,
        shift_multiplier=10.0, concentration=1.0, bad_units=3,
    ),
    "coordinate": dict(
        label="协同偏移 + 虚增数量",
        description="多个单位同向上偏并放大有效数量，声称权重约等于污染上限；检验封顶与截尾。",
        n_units=28, bad_weight_share=0.085, bad_kind="both", spread=4.0,
        shift_multiplier=9.0, concentration=2.5, bad_units=4,
    ),
    "inflate_n": dict(
        label="虚增有效数量",
        description="一个单位的均值几乎不动，却把有效数量放大 30 倍；直接按 n 加权会被它带入。",
        n_units=24, bad_weight_share=0.22, bad_kind="count_inflation", spread=4.0,
        shift_multiplier=1.6, concentration=1.0, bad_units=1,
    ),
    "over_ceiling": dict(
        label="超出污染上限（失去保证）",
        description="损坏权重份额约为 α 的三倍；展示保证被撤销、误差界失效。",
        n_units=24, bad_weight_share=0.35, bad_kind="both", spread=4.0,
        shift_multiplier=9.0, concentration=2.0, bad_units=6,
    ),
    "few_bad": dict(
        label="少量极端值（误伤边界）",
        description="只有两个坏单位但偏移很大，同时合法单位之间的异质性也很大。",
        n_units=20, bad_weight_share=0.04, bad_kind="mean_shift", spread=9.0,
        shift_multiplier=6.0, concentration=1.0, bad_units=2,
    ),
}


def generate(
    scenario: str = "independent",
    *,
    seed: int = 20261124,
    n_units: int | None = None,
    value_scale: float = 100.0,
    spread: float | None = None,
    batches: int = 4,
    bad_weight_share: float | None = None,
    bad_kind: str | None = None,
    shift_multiplier: float | None = None,
    concentration: float | None = None,
) -> GeneratedPanel:
    """Build a panel of per-unit summaries with known truth.

    ``bad_weight_share`` is the share of *claimed* effective count held by
    corrupt summaries.  After the cap it is typically lower -- that is the
    quantity the guarantee is graded against.
    """
    spec = dict(SCENARIOS["baseline"])
    if scenario in SCENARIOS:
        spec.update(SCENARIOS[scenario])
    elif scenario != "custom":
        raise ValueError(f"unknown scenario {scenario!r}")
    for key, val in {
        "n_units": n_units, "spread": spread, "bad_weight_share": bad_weight_share,
        "bad_kind": bad_kind, "shift_multiplier": shift_multiplier, "concentration": concentration,
    }.items():
        if val is not None:
            spec[key] = val

    rng = SplitMix64(int(seed) | 1)
    n_units = max(6, int(spec["n_units"]))
    spread = float(spec["spread"])
    metric = PRIMARY_METRIC
    period = "2026-W46"
    base_center = value_scale
    groups = ["甲", "乙", "丙", "丁"][: max(1, min(4, batches))]

    units: list[UnitTruth] = []
    for i in range(n_units):
        group = groups[i * len(groups) // n_units]
        gid = groups.index(group)
        true_mean = base_center + spread * rng.gauss(0.0, 1.0) + gid * spread * 0.25
        true_mean = max(1.0, true_mean)
        true_n = rng.randrange(120) + 60
        true_std = true_mean * (0.25 + 0.20 * rng.random())
        # Second component of the mean vector: genuinely correlated with the
        # first (units differ as wholes).  Drawn from a separate stream so the
        # primary component's tuning is unchanged by its presence.
        sec = SplitMix64((int(seed) ^ 0xA11CE) + i * 7919)
        secondary_mean = 70.0 + 0.18 * (true_mean - base_center) + 6.0 * sec.gauss(0.0, 1.0)
        secondary_mean = min(99.0, max(1.0, secondary_mean))
        secondary_std = secondary_mean * (0.12 + 0.10 * sec.random())
        units.append(
            UnitTruth(
                f"单位-{i+1:02d}", group, true_mean, true_std, true_n, f"批次-{group}-01",
                true_means={metric: true_mean, SECONDARY_METRIC: secondary_mean},
                secondary_mean=secondary_mean, secondary_std=secondary_std,
            )
        )

    good_weight = sum(u.true_n for u in units)
    target = float(spec["bad_weight_share"])
    bad_indices: list[int] = []
    if target > 0:
        n_bad = int(spec.get("bad_units") or 0) or max(1, round(target * n_units))
        n_bad = max(1, min(n_units - 2, n_bad))
        order = list(range(n_units))
        sh = SplitMix64(int(seed) ^ 0x5EED)
        for i in range(n_units - 1, 0, -1):
            j = sh.randrange(i + 1)
            order[i], order[j] = order[j], order[i]
        bad_indices = sorted(order[:n_bad])

        target_bad_weight = target / (1.0 - target) * good_weight
        conc = max(0.0, float(spec["concentration"]))
        if conc <= 0 or len(bad_indices) == 1:
            shares = [1.0 / len(bad_indices)] * len(bad_indices)
        else:
            raw = [conc ** i for i in range(len(bad_indices))]
            raw.reverse()
            s = sum(raw)
            shares = [r / s for r in raw]
        for k, idx in enumerate(bad_indices):
            units[idx].is_bad = True
            units[idx].bad_kind = spec["bad_kind"]
            units[idx].corruption["claimed_weight"] = target_bad_weight * shares[k]

    summaries: list[Summary] = []
    vector_summaries: list[Summary] = []
    for u in units:
        mean = u.true_mean
        n_eff = float(u.true_n)
        reported_std = u.true_std
        if u.is_bad:
            kind = u.bad_kind
            claimed = u.corruption["claimed_weight"]
            shifted = False
            if kind in ("mean_shift", "both") and spec["shift_multiplier"] > 0:
                direction = 1.0 if kind == "both" else (1.0 if rng.random() < 0.5 else -1.0)
                mean = u.true_mean + direction * spec["shift_multiplier"] * spread * (0.7 + 0.6 * rng.random())
                reported_std = max(0.01, u.true_std * (0.15 + 0.15 * rng.random()))
                shifted = True
            n_eff = max(1.0, claimed)
            u.corruption.update(
                {"kind": kind, "delta": mean - u.true_mean, "n_multiplier": n_eff / u.true_n,
                 "true_n": u.true_n, "shifted": shifted}
            )
        if mean <= 0 or not math.isfinite(mean):
            mean = max(0.01, abs(mean) + 1.0)
        lo = min(mean, u.true_mean) - reported_std * 4.0
        hi = max(mean, u.true_mean) + reported_std * 4.0
        primary = Summary(
            summary_id=f"{u.unit_id}-{period}",
            unit_id=u.unit_id,
            batch_id=u.batch_id,
            metric=metric,
            period=period,
            mean=round(mean, 4),
            std=round(max(reported_std, 0.01), 4),
            n_effective=round(n_eff, 2),
            range_low=round(lo, 4),
            range_high=round(hi, 4),
        )
        summaries.append(primary)
        vector_summaries.append(primary)
        # The unit reports its whole mean vector in one receipt batch.  Only
        # the primary component carries the injected mean shift; both carry
        # the (possibly inflated) count, as one unit's count must.
        smean = u.secondary_mean
        sstd = max(0.01, u.secondary_std)
        vector_summaries.append(
            Summary(
                summary_id=f"{u.unit_id}-{period}-{SECONDARY_METRIC}",
                unit_id=u.unit_id,
                batch_id=u.batch_id,
                metric=SECONDARY_METRIC,
                period=period,
                mean=round(smean, 4),
                std=round(sstd, 4),
                n_effective=round(n_eff, 2),
                range_low=round(smean - sstd * 4.0, 4),
                range_high=round(smean + sstd * 4.0, 4),
            )
        )

    total_n = sum(u.true_n for u in units)
    truth = sum(u.true_mean * u.true_n for u in units) / total_n
    secondary_truth = sum(u.secondary_mean * u.true_n for u in units) / total_n

    n_bad_units = sum(1 for u in units if u.is_bad)
    claimed_bad = sum(s.n_effective for s, u in zip(summaries, units) if u.is_bad)
    claimed_total = sum(s.n_effective for s in summaries)
    return GeneratedPanel(
        name=scenario,
        seed=int(seed),
        metric=metric,
        period=period,
        truth=round(truth, 6),
        units=units,
        summaries=summaries,
        metrics=[metric, SECONDARY_METRIC],
        truth_by_metric={metric: round(truth, 6), SECONDARY_METRIC: round(secondary_truth, 6)},
        all_summaries=vector_summaries,
        metadata={
            "label": spec["label"],
            "description": spec["description"],
            "bad_units": n_bad_units,
            "n_units": n_units,
            "bad_unit_fraction": n_bad_units / n_units,
            "claimed_bad_weight_share": claimed_bad / claimed_total if claimed_total else 0.0,
            "target_bad_weight_share": target,
            "spread": spread,
            "value_scale": value_scale,
            "batches": groups,
            "scenario": scenario,
            "seed": int(seed),
            "metrics": [metric, SECONDARY_METRIC],
            "truth_by_metric": {metric: round(truth, 6), SECONDARY_METRIC: round(secondary_truth, 6)},
        },
    )
