"""Simulation: does the robust centre beat the naive one under stated assumptions?

Two separate experiments, because they answer different questions:

``simulate_scenario``  -- changed data (a fresh generated panel per run):
    quantifies the *upside* of robustness across contamination placements.

``null_calibration``   -- unchanged data, estimator pairs computed under
    different trust ceilings on the *same* values.  Quantifies the *downside*
    ("误伤"): how much a legitimate, uncontaminated panel moves when alpha or
    the weight cap changes.  A robust estimator that moved a clean panel by
    more than the simulation band would be misfiring.

The returned bands are simulation bands (see ``bounds.band_from_errors``), not
guarantees; the generator that produced them is named in the result.
"""

from __future__ import annotations

import math

from .analysis import AnalysisConfig, analyze
from .bounds import band_from_errors
from .estimators import Summary
from .synthetic import SCENARIOS, generate


def _run_once(panel, *, alpha: float, max_share: float, isolated_units: set[str] | None = None):
    cfg = AnalysisConfig(
        metric=panel.metric, period=panel.period,
        contamination_rate=alpha, max_share=max_share,
    )
    bad = {u.unit_id for u in panel.units if u.is_bad}
    return analyze(panel.summaries, config=cfg, truth=panel.truth, bad_units=bad,
                   isolated_units=isolated_units)


def simulate_scenario(
    scenario: str,
    *,
    runs: int = 200,
    seed: int = 7,
    n_units: int | None = None,
    contamination_rate: float = 0.15,
    max_share: float = 0.10,
) -> dict:
    """Monte-Carlo over generated panels: absolute error per estimator."""
    errs = {k: [] for k in ("naive", "naive_uncapped", "trimmed", "huber", "median")}
    coverage = {"graded": 0, "covered": 0, "void": 0}
    meta = None
    for r in range(runs):
        panel = generate(scenario, seed=seed * 100003 + r, n_units=n_units)
        if meta is None:
            meta = panel.metadata
        res = _run_once(panel, alpha=contamination_rate, max_share=max_share)
        for est in errs:
            c = res["estimates"][est]["center"]
            errs[est].append(abs(c - panel.truth) if math.isfinite(c) else math.inf)
        b = res["bound"]
        if b["status"] == "void":
            coverage["void"] += 1
        else:
            coverage["graded"] += 1
            if b["interval"] and b["interval"][0] <= panel.truth <= b["interval"][1]:
                coverage["covered"] += 1
    results = {}
    for est, vals in errs.items():
        results[est] = {
            "median_abs_err": band_from_errors(vals, 0.5)["half_width"],
            "p90_abs_err": band_from_errors(vals, 0.9)["half_width"],
            "max_abs_err": band_from_errors(vals, 1.0)["half_width"],
            "band": band_from_errors(vals, 0.9),
        }
    gain = None
    if results["trimmed"]["p90_abs_err"] > 0:
        gain = results["naive_uncapped"]["p90_abs_err"] / results["trimmed"]["p90_abs_err"]
    return {
        "scenario": scenario,
        "runs": runs,
        "contamination_rate": contamination_rate,
        "max_share": max_share,
        "label": (meta or {}).get("label"),
        "description": (meta or {}).get("description"),
        "results": results,
        "bound_coverage": {
            **coverage,
            "rate": (coverage["covered"] / coverage["graded"]) if coverage["graded"] else None,
        },
        "gain_uncapped_vs_trimmed_p90": gain,
        "model": "generate(scenario, seed=seed*100003+r); independent draws per run",
    }


def null_calibration(
    *,
    runs: int = 120,
    seed: int = 11,
    alphas: tuple[float, ...] = (0.05, 0.10, 0.20),
    cap_shares: tuple[float, ...] = (0.25, 0.10),
    n_units: int = 24,
) -> dict:
    """How far does the robust centre move a *clean* panel when alpha changes?

    For each generated clean panel the estimators are computed under several
    trust ceilings.  A well-behaved robust estimator moves the centre by less
    than the reported bound on panels with no corrupt units -- this is the
    "误伤" measurement the brief asks for.
    """
    out = {}
    for alpha in alphas:
        for cap_share in cap_shares:
            deltas = {k: [] for k in ("trimmed", "huber", "median", "naive")}
            for r in range(runs):
                panel = generate("baseline", seed=seed * 100003 + r, n_units=n_units)
                base = _run_once(panel, alpha=0.10, max_share=0.25)
                alt = _run_once(panel, alpha=alpha, max_share=cap_share)
                for est in deltas:
                    a = base["estimates"][est]["center"]
                    b = alt["estimates"][est]["center"]
                    if math.isfinite(a) and math.isfinite(b):
                        deltas[est].append(abs(b - a))
            key = f"alpha={alpha:.2f}|cap={cap_share:.2f}"
            out[key] = {
                "alpha": alpha,
                "max_share": cap_share,
                "results": {
                    est: {
                        "median_shift": band_from_errors(v, 0.5)["half_width"],
                        "p90_shift": band_from_errors(v, 0.9)["half_width"],
                        "max_shift": band_from_errors(v, 1.0)["half_width"],
                    }
                    for est, v in deltas.items()
                },
            }
    return {
        "runs": runs,
        "n_units": n_units,
        "baseline_setting": {"alpha": 0.10, "max_share": 0.25},
        "by_setting": out,
        "model": "baseline panels (no corrupt units); estimators re-run on the same values",
    }


def simulate_all(*, runs: int = 120, seed: int = 7, contamination_rate: float = 0.15,
                 max_share: float = 0.10) -> dict:
    return {
        name: simulate_scenario(
            name, runs=runs, seed=seed,
            contamination_rate=contamination_rate, max_share=max_share,
        )
        for name in SCENARIOS
    }
