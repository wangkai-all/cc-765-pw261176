"""Error bounds for the robust centre.

Two statements are produced, and the UI must never blur them:

* **Worst-case bound** (``trimmed_bound``) -- holds for any placement of the
  corrupted mass, given the assumptions in its ledger.  Reported as an
  interval around the trimmed mean, with status
  ``guaranteed`` / ``conditional`` / ``void``.
* **Simulation band** (``band_from_errors``) -- Monte-Carlo quantiles under an
  explicit generative model.  It checks the analytic bound; it is not a
  substitute for it.

Derivation and assumption list: ``docs/theory.md``.

Short derivation (trimmed mean ``T``, alpha trimmed each tail, value span
``s``, bad mass ``b <= alpha``, per-unit cap ``c``)::

    |T(observed) - clean mean|
        <= (b + alpha) * s / (1 - 2*alpha)          # bad mass + trim of clean tails
           + max(0, c - alpha) * s / (1 - 2*alpha)  # heavyweight unit surviving trim

Setting ``b = alpha`` gives the interval used here.  ``alpha`` is the
estimator's breakdown point: if the actual bad share exceeds it, the bound is
void rather than merely loose.
"""

from __future__ import annotations

import math
from typing import Sequence

from .estimators import weighted_median


def _trimmed_mean(pairs, alpha):
    items = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in items)
    lo, hi = alpha * total, total - alpha * total
    num = den = cum = 0.0
    for v, w in items:
        start, end = cum, cum + w
        cum = end
        ov = max(0.0, min(end, hi) - max(start, lo))
        if ov > 0:
            num += ov * v
            den += ov
    return num / den if den else math.nan


def trimmed_bound(
    pairs: Sequence[tuple[float, float]],
    alpha: float,
    *,
    max_share: float,
    n_units: int,
    known_bad_share: float | None = None,
    min_units: int = 3,
    cap_feasible: bool | None = None,
    cap_requested: float | None = None,
) -> dict:
    """Worst-case half-width for the alpha-trimmed mean.

    ``pairs`` are (value, weight).  ``known_bad_share`` exists only for
    generated data; when supplied and above ``alpha`` the guarantee is void.
    ``cap_feasible`` is ``False`` when the requested per-unit ceiling cannot be
    attained on this panel (too few units for the requested share); the
    guarantee is then void rather than silently uncapped.  ``max_share`` is the
    ceiling actually attained, and ``cap_requested`` (when given) is the lower
    one the caller asked for, so the ledger can say which of the two failed.
    """
    total = sum(w for _, w in pairs)
    values = [v for v, _ in pairs]
    span = (max(values) - min(values)) if len(values) >= 2 else 0.0
    kept = 1.0 - 2.0 * alpha

    assumptions = [
        {
            "id": "alpha_range",
            "status": "met" if 0.0 < alpha < 0.5 else "unmet",
            "text": f"污染上限 α={alpha:.0%} 必须落在 (0, 50%)",
        },
        {
            "id": "contamination_ceiling",
            "status": (
                "met" if known_bad_share is not None and known_bad_share <= alpha + 1e-12
                else "unmet" if known_bad_share is not None
                else "unverifiable"
            ),
            "text": (
                f"实际损坏摘要的权重份额 ≤ α={alpha:.0%}"
                + (f"（本次已知 {known_bad_share:.1%}）" if known_bad_share is not None
                   else "（接收路径无法核验，只能按申报值假设）")
            ),
        },
        {
            "id": "cap",
            "status": "unmet" if cap_feasible is False else "met",
            "text": (
                # When the cap is feasible the bound uses the share actually
                # attained, and that is the number the widening term names; only
                # an infeasible ceiling is described by the requested value.
                f"单单位权重上限 {(cap_requested if cap_feasible is False and cap_requested is not None else max_share):.0%}"
                + (f"：本面板 {n_units} 个单位，任何正上限都无法把每个单位压到该份额以下"
                   if cap_feasible is False
                   else " ≤ α：一个坏单位可被完全截掉" if max_share <= alpha + 1e-9
                   else f" > α：界额外放宽 (上限−α)={max_share - alpha:.0%} 项")
            ),
        },
        {
            "id": "units",
            "status": "met" if n_units >= min_units else "unmet",
            "text": f"至少 {min_units} 个单位参与（当前 {n_units}）",
        },
        {
            "id": "aggregate_only",
            "status": "unverifiable",
            "text": "摘要只含聚合量；服务端未接收原始记录，无法复核其内部一致性",
        },
    ]

    def finish(offset, trimmed_mean, extra=""):
        unmet = [a for a in assumptions if a["status"] == "unmet"]
        unver = [a for a in assumptions if a["status"] == "unverifiable"]
        if unmet or not math.isfinite(trimmed_mean) or not math.isfinite(offset):
            status = "void"
        elif unver:
            status = "conditional"
        else:
            status = "guaranteed"
        interval = ([trimmed_mean - offset, trimmed_mean + offset]
                    if status != "void" and math.isfinite(trimmed_mean) else None)
        if status == "void":
            extra += " 前提未满足或无法成立，故不提供保证区间。"
        elif status == "conditional":
            extra += " 保证以『实际污染不超过 α』为前提；该前提只能申报，服务端无法核验。"
        return {
            "offset": offset,
            "interval": interval,
            "status": status,
            "guaranteed": status == "guaranteed",
            "assumptions": assumptions,
            "rationale": extra.strip(),
            "span": span,
            "kept_mass": kept,
            "trim_mean": trimmed_mean,
            "median": weighted_median([(v, w) for v, w in pairs]) if pairs else math.nan,
            "alpha": alpha,
            "known_bad_share": known_bad_share,
        }

    if not pairs or total <= 0 or len(values) < 2:
        assumptions[3]["status"] = "unmet"
        return finish(math.inf, math.nan, "没有足够的单位摘要。")
    if kept <= 0:
        return finish(math.inf, math.nan, "α ≥ 50%，截尾后无剩余质量，稳健性不成立。")

    beta = alpha
    offset = span * (beta + alpha + max(0.0, max_share - alpha)) / kept
    trimmed_mean = _trimmed_mean(pairs, alpha)
    rationale = (
        f"截尾均值与干净总体中心之差 ≤ (β+α+max(0,上限-α))·跨度/(1-2α) = "
        f"({beta:.2f}+{alpha:.2f}+{max(0.0, max_share - alpha):.2f})·{span:.2f}/{kept:.2f} "
        f"= {offset:.2f}。该界对损坏值的任意放置成立；α 同时是截尾均值的崩溃点。"
    )
    return finish(offset, trimmed_mean, rationale)


def band_from_errors(errors: Sequence[float], level: float = 0.9) -> dict:
    """Empirical band from simulated |estimate - truth|.

    A statement about the generative model: at ``level``=0.9, ninety percent of
    simulated placements land within the half-width.
    """
    vals = sorted(e for e in errors if math.isfinite(e))
    if not vals:
        return {"level": level, "half_width": math.inf, "n": 0}
    idx = min(len(vals) - 1, max(0, math.ceil(level * len(vals)) - 1))
    return {
        "level": level,
        "half_width": vals[idx],
        "n": len(vals),
        "median": vals[len(vals) // 2],
        "max": vals[-1],
    }
