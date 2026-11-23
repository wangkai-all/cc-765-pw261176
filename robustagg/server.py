"""HTTP layer: aggregate-only intake, analysis, isolation, traceability, simulation.

Zero third-party dependencies: standard library only, so the project runs with
whatever Python the reviewer has.  The API is a thin JSON layer over ``store``
and ``analysis``; the browser client lives in ``static/``.

Intake policy
-------------
The server accepts *aggregate summaries only*.  The allow-list below is
enforced on every receipt; unknown fields (including anything resembling a
row-level record: ``records``, ``rows``, ``samples``, ``values``) are rejected
with 422 and the request is not stored.  There is no endpoint that accepts raw
person-level data.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import store
from .analysis import AnalysisConfig, analyze, compare, quantile_curve, unit_influence
from .estimators import ESTIMATORS, ESTIMATOR_LABELS, Summary
from .simulation import null_calibration, simulate_all, simulate_scenario
from .synthetic import SCENARIOS, generate

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

DATASET_DB = os.environ.get("ROBUSTAGG_DB", str(ROOT.parent / "data" / "robustagg.db"))

ALLOWED_RECEIPT_FIELDS = {
    "summary_id", "unit_id", "batch_id", "metric", "period",
    "mean", "std", "n_effective", "range_low", "range_high",
    "reported_total", "revision_of",
    # Compact mean-vector form: an aggregate summary may report several
    # components at once.  Expanded server-side into one receipt per metric.
    "mean_vector", "std_vector", "ranges",
}
# Anything here is a hard rejection, not an ignored key: these names indicate
# the sender is trying to upload individual records.
FORBIDDEN_KEYS = {
    "records", "rows", "samples", "values", "individuals", "persons", "microdata",
    "person_id", "record_id", "dob", "birth_date", "name", "address", "phone",
    "email", "id_number", "ssn",
}

# Re-entrant: _dispatch takes the lock for every request, and handlers that do
# a read-then-write sequence (intake conflict checks, for example) take it again
# to make the sequence atomic.  A plain Lock deadlocked the second acquire.
_lock = threading.RLock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = store.connect(DATASET_DB)
    return _conn


def _num(value, field, *, positive=False, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise store.Conflict(f"{field} 必须是数值")
    v = float(value)
    if not math.isfinite(v):
        raise store.Conflict(f"{field} 必须是有限数值")
    if positive and v <= 0:
        raise store.Conflict(f"{field} 必须为正数")
    return v


def expand_vector_receipt(raw: dict) -> list[dict]:
    """Expand a compact mean-vector receipt into one receipt per metric.

    A unit's report is a *vector* of summary means.  The compact form is still
    purely aggregate::

        {"unit_id": "单位-07", "batch_id": "批次-甲-01", "period": "2026-W46",
         "n_effective": 140,
         "mean_vector": {"指标值": 98.2, "伴随指标": 70.4},
         "std_vector": {"指标值": 11.4, "伴随指标": 6.1},
         "ranges": {"指标值": [60, 140], "伴随指标": [50, 90]}}

    Each component becomes its own receipt (same unit, batch and count) so the
    store keeps one row per (unit, metric, period) and every estimate can be
    traced per component.  Unknown or non-numeric components are rejected by
    the ordinary receipt validator afterwards.
    """
    if "mean_vector" not in raw:
        return [raw]
    if not isinstance(raw, dict):
        raise store.Conflict("每条摘要必须是一个 JSON 对象")
    mean_vec = raw.get("mean_vector")
    if not isinstance(mean_vec, dict) or not mean_vec:
        raise store.Conflict("mean_vector 必须是非空对象（指标 → 均值）")
    if len(mean_vec) > 12:
        raise store.Conflict("mean_vector 最多 12 个分量")
    if "mean" in raw:
        raise store.Conflict("mean 与 mean_vector 只能二选一")
    if raw.get("revision_of"):
        raise store.Conflict("均值向量的更正请按分量分别提交 revision_of")
    std_vec = raw.get("std_vector") or {}
    if not isinstance(std_vec, dict):
        raise store.Conflict("std_vector 必须是对象（指标 → 标准差）")
    ranges = raw.get("ranges") or {}
    if not isinstance(ranges, dict):
        raise store.Conflict("ranges 必须是对象（指标 → [下限, 上限]）")
    base_id = raw.get("summary_id")
    if base_id is not None and not isinstance(base_id, str):
        raise store.Conflict("summary_id 必须是字符串")
    unit_id = raw.get("unit_id")
    period = raw.get("period")
    if not base_id and isinstance(unit_id, str) and unit_id.strip() and isinstance(period, str) and period.strip():
        # Deterministic id so a retransmitted vector collides (422) instead of
        # silently doubling a unit's weight.
        base_id = f"{unit_id.strip()}-{period.strip()}"
    n_components = len(mean_vec)
    out = []
    for metric, mean in mean_vec.items():
        if not isinstance(metric, str) or not metric.strip():
            raise store.Conflict("mean_vector 的键必须是非空指标名")
        component = dict(raw)
        component.pop("mean_vector", None)
        component.pop("std_vector", None)
        component.pop("ranges", None)
        component["metric"] = metric.strip()
        component["mean"] = mean
        std = std_vec.get(metric, raw.get("std"))
        if std is None:
            raise store.Conflict(f"分量 {metric!r} 缺少 std_vector 或 std")
        component["std"] = std
        rng = ranges.get(metric)
        if rng is not None:
            if not isinstance(rng, (list, tuple)) or len(rng) != 2:
                raise store.Conflict(f"分量 {metric!r} 的 ranges 必须是 [下限, 上限]")
            component["range_low"], component["range_high"] = rng
        else:
            lo, hi = _component_range(mean, std)
            component["range_low"], component["range_high"] = lo, hi
        component.pop("std_vector", None)
        if base_id is not None:
            component["summary_id"] = (
                base_id if n_components == 1 else f"{base_id}-{component['metric']}"
            )
        out.append(component)
    return out


def _component_range(mean, std) -> tuple[float, float]:
    """Derive a structural range for a component when the sender omits one."""
    m = _num(mean, "mean_vector 分量")
    s = _num(std, "std_vector 分量")
    if s < 0:
        raise store.Conflict("std_vector 不能为负")
    return (m - 4.0 * s, m + 4.0 * s)


def validate_receipt(raw: dict) -> Summary:
    if not isinstance(raw, dict):
        raise store.Conflict("每条摘要必须是一个 JSON 对象")
    forbidden = FORBIDDEN_KEYS & set(raw)
    if forbidden:
        raise store.Conflict(
            "服务端不接收原始个人记录；检测到逐记录字段: " + ", ".join(sorted(forbidden))
        )
    unknown = set(raw) - ALLOWED_RECEIPT_FIELDS
    if unknown:
        raise store.Conflict("不接受的字段: " + ", ".join(sorted(unknown)))
    for key in ("summary_id", "unit_id", "batch_id", "metric", "period"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise store.Conflict(f"{key} 必须是非空字符串")
        if len(raw[key]) > 120:
            raise store.Conflict(f"{key} 过长")
    mean = _num(raw.get("mean"), "mean")
    std = _num(raw.get("std"), "std")
    n = _num(raw.get("n_effective"), "n_effective", positive=True)
    lo = _num(raw.get("range_low"), "range_low")
    hi = _num(raw.get("range_high"), "range_high")
    if std < 0:
        raise store.Conflict("std 不能为负")
    if hi < lo:
        raise store.Conflict("range_high 必须不小于 range_low")
    if not (lo <= mean <= hi):
        raise store.Conflict("mean 必须落在 [range_low, range_high] 内（结构一致性检查）")
    total = _num(raw.get("reported_total"), "reported_total", allow_none=True)
    if total is not None and total < 0:
        raise store.Conflict("reported_total 不能为负")
    rev = raw.get("revision_of")
    if rev is not None and (not isinstance(rev, str) or not rev.strip()):
        raise store.Conflict("revision_of 必须是非空字符串")
    return Summary(
        summary_id=raw["summary_id"].strip(), unit_id=raw["unit_id"].strip(),
        batch_id=raw["batch_id"].strip(), metric=raw["metric"].strip(),
        period=raw["period"].strip(), mean=mean, std=std, n_effective=n,
        range_low=lo, range_high=hi, reported_total=total,
        revision_of=rev.strip() if rev else None,
    )


def _dataset_or_404(ds_id: str) -> dict:
    ds = store.get_dataset(conn(), ds_id)
    if not ds:
        raise KeyError(ds_id)
    return ds


def _dataset_metrics(ds: dict) -> list[str]:
    meta = ds.get("metadata") or {}
    listed = meta.get("metrics")
    if listed:
        return list(listed)
    rows = conn().execute(
        "SELECT DISTINCT metric FROM summaries WHERE dataset_id = ? ORDER BY metric", (ds["id"],)
    ).fetchall()
    metrics = [r["metric"] for r in rows]
    return metrics or ["指标值"]


def _config(ds: dict, q: dict) -> AnalysisConfig:
    meta = ds.get("metadata") or {}
    alpha = float(q.get("alpha", 0.10))
    cap = float(q.get("max_share", 0.10))
    est = q.get("estimator", "trimmed")
    if est not in ESTIMATORS:
        raise store.Conflict(f"未知估计量 {est!r}")
    metrics = _dataset_metrics(ds)
    metric = q.get("metric") or meta.get("metric") or metrics[0]
    if metric not in metrics:
        raise store.Conflict(f"数据集没有指标 {metric!r}；可选 {', '.join(metrics)}")
    periods = [r["period"] for r in conn().execute(
        "SELECT DISTINCT period FROM summaries WHERE dataset_id = ? AND metric = ? ORDER BY period",
        (ds["id"], metric),
    ).fetchall()]
    period = q.get("period") or (periods[0] if periods else "2026-W46")
    return AnalysisConfig(metric=metric, period=period, contamination_rate=alpha,
                          max_share=cap, estimator=est).validated()


def _context(ds_id: str, q: dict):
    ds = _dataset_or_404(ds_id)
    cfg = _config(ds, q)
    rows = store.active_rows(conn(), ds_id, metric=cfg.metric, period=cfg.period)
    summaries = store.rows_to_summaries(rows)
    isolated = store.active_isolated_units(conn(), ds_id, metric=cfg.metric, period=cfg.period)
    bad_units = {u["unit_id"] for u in ds["metadata"].get("unit_truth", []) if u.get("is_bad")}
    truth = None
    if ds.get("scenario"):
        by_metric = ds["metadata"].get("truth_by_metric") or {}
        truth = by_metric.get(cfg.metric, ds["truth"] if cfg.metric == ds["metadata"].get("metric") else None)
    return ds, cfg, rows, summaries, isolated, bad_units, truth


def _analysis_payload(ds_id: str, q: dict) -> dict:
    ds, cfg, rows, summaries, isolated, bad_units, truth = _context(ds_id, q)
    res = analyze(summaries, config=cfg, isolated_units=isolated, truth=truth, bad_units=bad_units)
    cmp = compare(summaries, config=cfg, isolated_units=isolated, truth=truth, bad_units=bad_units)
    curve = quantile_curve(summaries, config=cfg, isolated_units=isolated)
    iso_records = store.isolations_all(conn(), ds_id)
    active_iso = [i for i in iso_records if i["released_at"] is None]
    res.update(
        {
            "dataset": {
                "id": ds["id"], "name": ds["name"], "scenario": ds["scenario"],
                "seed": ds["seed"], "truth": ds["truth"], "metadata": ds["metadata"],
                "created_at": ds["created_at"],
            },
            "compare": cmp,
            "curve": curve,
            "receipts": rows,
            "metrics": _dataset_metrics(ds),
            "periods": [r["period"] for r in conn().execute(
                "SELECT DISTINCT period FROM summaries WHERE dataset_id = ? AND metric = ? ORDER BY period",
                (ds_id, cfg.metric),
            ).fetchall()],
            "metric_truth": (ds.get("metadata") or {}).get("truth_by_metric") or {},
            "isolations": iso_records,
            "active_isolation_count": len(active_iso),
            "labels": ESTIMATOR_LABELS,
            "scenarios": {k: {"label": v["label"], "description": v["description"]} for k, v in SCENARIOS.items()},
            "intake_policy": {
                "allowed_fields": sorted(ALLOWED_RECEIPT_FIELDS),
                "forbidden_fields": sorted(FORBIDDEN_KEYS),
                "statement": "服务端只接收聚合摘要，任何逐记录字段都会被拒绝。",
            },
        }
    )
    return res


def _summarize_unit(res: dict, unit_id: str) -> dict | None:
    frags = [c for c in res["contributions"] if c["unit_id"] == unit_id]
    if not frags:
        return None
    return {
        "unit_id": unit_id,
        "fragments": frags,
        "weight": sum(f["weight"] for f in frags),
        "claimed_weight": sum(f["claimed_weight"] for f in frags),
        "isolated": all(f["isolated"] for f in frags),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def h_datasets(body, q):
    return {"datasets": store.list_datasets(conn())}


def h_demo(body, q):
    scenario = body.get("scenario", "coordinate")
    if scenario not in SCENARIOS:
        raise store.Conflict(f"未知样例 {scenario!r}")
    seed = int(body.get("seed", 20261124))
    n_units = body.get("n_units")
    panel = generate(scenario, seed=seed, n_units=int(n_units) if n_units else None)
    ds_id = store.create_dataset(
        conn(), name=f"{panel.metadata['label']} · seed {seed}", scenario=scenario, seed=seed,
        truth=panel.truth,
        metadata={
            **panel.metadata,
            "unit_truth": [
                {
                    "unit_id": u.unit_id, "group": u.group, "batch_id": u.batch_id,
                    "true_mean": u.true_mean, "true_n": u.true_n, "is_bad": u.is_bad,
                    "bad_kind": u.bad_kind, "secondary_mean": u.secondary_mean,
                    "true_means": u.true_means,
                }
                for u in panel.units
            ],
        },
    )
    for s in (panel.all_summaries or panel.summaries):
        store.insert_summary(conn(), ds_id, s, source="demo")
    store.record_decision(conn(), ds_id, "demo_created", {"scenario": scenario, "seed": seed,
                                                          "n_units": panel.metadata["n_units"]})
    return {"dataset_id": ds_id, "panel": _panel_view(ds_id)}


def _panel_view(ds_id: str) -> dict:
    ds = _dataset_or_404(ds_id)
    rows = store.active_rows(conn(), ds_id)
    return {
        "dataset": {"id": ds["id"], "name": ds["name"], "scenario": ds["scenario"],
                    "seed": ds["seed"], "truth": ds["truth"], "metadata": ds["metadata"]},
        "receipts": rows,
        "batches": store.batches(conn(), ds_id),
        "history": {u["unit_id"]: store.history_for_unit(conn(), ds_id, u["unit_id"])
                    for u in (ds["metadata"].get("unit_truth") or [])},
    }


def h_panel(ds_id, body, q):
    return _panel_view(ds_id)


def h_analysis(ds_id, body, q):
    return _analysis_payload(ds_id, q)


def h_compare(ds_id, body, q):
    ds, cfg, rows, summaries, isolated, bad_units, truth = _context(ds_id, q)
    return compare(summaries, config=cfg, isolated_units=isolated, truth=truth, bad_units=bad_units)


def h_batches(ds_id, body, q):
    return {"batches": store.batches(conn(), ds_id)}


def h_batch(ds_id, body, q):
    batch_id = q.get("batch_id")
    if not batch_id:
        raise store.Conflict("缺少 batch_id")
    return {"batch_id": batch_id, "receipts": store.batch_receipts(conn(), ds_id, batch_id)}


def h_unit(ds_id, unit_id, body, q):
    ds, cfg, rows, summaries, isolated, bad_units, truth = _context(ds_id, q)
    res = analyze(summaries, config=cfg, isolated_units=isolated, truth=truth, bad_units=bad_units)
    influence = unit_influence(summaries, config=cfg, isolated_units=isolated, unit_id=unit_id,
                               truth=truth, bad_units=bad_units)
    truth_row = next((u for u in ds["metadata"].get("unit_truth", []) if u["unit_id"] == unit_id), None)
    return {
        "unit_id": unit_id,
        "truth": truth_row,
        "summary": _summarize_unit(res, unit_id),
        "influence": influence,
        "history": store.history_for_unit(conn(), ds_id, unit_id),
        "isolation": store.active_isolation(conn(), ds_id, unit_id, cfg.metric, cfg.period),
        "estimator": cfg.estimator,
    }


def h_trace(ds_id, body, q):
    ids = q.get("summary_ids") or ""
    wanted = [s for s in ids.split(",") if s.strip()]
    receipts = []
    for sid in wanted:
        row = store.receipt_by_summary_id(conn(), ds_id, sid.strip())
        if row:
            receipts.append(row)
    batch_ids = sorted({r["batch_id"] for r in receipts})
    batches = []
    for bid in batch_ids:
        all_rows = store.batch_receipts(conn(), ds_id, bid)
        batches.append({"batch_id": bid, "receipts": all_rows})
    superseded = store.superseded_ids(conn(), ds_id)
    for r in receipts:
        r["active"] = r["summary_id"] not in superseded
    return {"requested": wanted, "receipts": receipts, "batches": batches}


def h_intake(ds_id, body, q):
    _dataset_or_404(ds_id)
    receipts = body.get("receipts")
    if receipts is None and "receipt" in body:
        receipts = [body["receipt"]]
    if not isinstance(receipts, list) or not receipts:
        raise store.Conflict("receipts 必须是非空数组")
    if len(receipts) > 500:
        raise store.Conflict("单次最多接收 500 条摘要")
    expanded = []
    for r in receipts:
        expanded.extend(expand_vector_receipt(r))
    if len(expanded) > 500:
        raise store.Conflict("展开均值向量后超过 500 条摘要")
    validated = [validate_receipt(r) for r in expanded]
    note = str(body.get("note") or "").strip()[:500] or None
    saved, conflicts = [], []
    with _lock:
        for s in validated:
            existing = store.receipt_by_summary_id(conn(), ds_id, s.summary_id)
            if existing:
                conflicts.append({"summary_id": s.summary_id, "error": "summary_id 已存在；更正请使用 /revise"})
                continue
            if s.revision_of:
                target = store.receipt_by_summary_id(conn(), ds_id, s.revision_of)
                if not target:
                    conflicts.append({"summary_id": s.summary_id, "error": "revision_of 指向不存在的摘要"})
                    continue
                if target["unit_id"] != s.unit_id:
                    conflicts.append({"summary_id": s.summary_id, "error": "更正必须属于同一单位"})
                    continue
                if s.revision_of in store.superseded_ids(conn(), ds_id):
                    conflicts.append({"summary_id": s.summary_id, "error": "该摘要已被更正过；请更正当前有效版本"})
                    continue
            store.insert_summary(conn(), ds_id, s, source="intake", note=note)
            saved.append(s.summary_id)
        store.record_decision(conn(), ds_id, "intake", {
            "saved": saved, "conflicts": conflicts, "note": note,
            "batch_ids": sorted({s.batch_id for s in validated}),
        })
    if conflicts and not saved:
        raise store.Conflict("全部摘要被拒绝: " + "; ".join(c["error"] for c in conflicts))
    return {"saved": saved, "conflicts": conflicts, "analysis": _analysis_payload(ds_id, {})}


def h_revise(ds_id, body, q):
    ds = _dataset_or_404(ds_id)
    target_id = body.get("summary_id")
    if not target_id:
        raise store.Conflict("缺少 summary_id")
    target = store.receipt_by_summary_id(conn(), ds_id, target_id)
    if not target:
        raise store.Conflict("摘要不存在")
    if target_id in store.superseded_ids(conn(), ds_id):
        raise store.Conflict("该摘要已被更正；请更正当前有效版本")
    mean = _num(body.get("mean"), "mean")
    std = _num(body.get("std", target["std"]), "std")
    n = _num(body.get("n_effective", target["n_effective"]), "n_effective", positive=True)
    lo = _num(body.get("range_low", min(target["range_low"], mean)), "range_low")
    hi = _num(body.get("range_high", max(target["range_high"], mean)), "range_high")
    if hi < lo or not (lo <= mean <= hi):
        raise store.Conflict("均值必须落在声称的范围内")
    note = str(body.get("note") or "更正摘要")[:500]
    new_id = f"{target['summary_id']}-r{int(target.get('revision_count', 0)) or _revision_seq(ds_id, target_id)}"
    summary = Summary(
        summary_id=new_id, unit_id=target["unit_id"], batch_id=target["batch_id"],
        metric=target["metric"], period=target["period"], mean=mean, std=std, n_effective=n,
        range_low=lo, range_high=hi, reported_total=body.get("reported_total"),
        revision_of=target_id,
    )
    store.insert_summary(conn(), ds_id, summary, source="correction", note=note)
    store.record_decision(conn(), ds_id, "revision", {
        "unit_id": target["unit_id"], "replaces": target_id, "new": new_id,
        "mean_before": target["mean"], "mean_after": mean,
        "n_before": target["n_effective"], "n_after": n, "note": note,
    })
    return {"saved": [new_id], "replaces": target_id, "analysis": _analysis_payload(ds_id, {})}


def _revision_seq(ds_id: str, summary_id: str) -> int:
    rows = conn().execute(
        "SELECT COUNT(*) AS c FROM summaries WHERE dataset_id = ? AND revision_of = ?",
        (ds_id, summary_id),
    ).fetchone()
    return int(rows["c"]) + 1


def h_inject(ds_id, body, q):
    """Demo-only corruption injection: rewrite one unit's mean/count.

    ``mode='revision'`` replaces the unit's contribution (a correction).
    ``mode='split'`` files the same unit again in a new batch, which the cap
    must neutralise.  Either way a new receipt is appended; nothing is edited.
    """
    ds = _dataset_or_404(ds_id)
    unit_id = body.get("unit_id")
    if not unit_id:
        raise store.Conflict("缺少 unit_id")
    rows = [r for r in store.active_rows(conn(), ds_id) if r["unit_id"] == unit_id]
    if not rows:
        raise store.Conflict("该单位在当前数据集中没有有效摘要")
    # One unit can report several metric components; inject into the component
    # the caller names, and keep that component's own receipt as the base.
    metric = body.get("metric")
    if metric:
        rows = [r for r in rows if r["metric"] == metric]
        if not rows:
            raise store.Conflict(f"该单位没有指标 {metric!r} 的有效摘要")
    base = rows[-1]
    mean = _num(body.get("mean", base["mean"]), "mean")
    n = _num(body.get("n_effective", base["n_effective"]), "n_effective", positive=True)
    std = _num(body.get("std", base["std"]), "std")
    mode = body.get("mode", "revision")
    if mode not in ("revision", "split"):
        raise store.Conflict("mode 必须是 revision 或 split")
    lo = min(min(r["range_low"] for r in rows), mean)
    hi = max(max(r["range_high"] for r in rows), mean)
    claimed_total = sum(r["n_effective"] for r in rows) or 1.0
    if mode == "revision":
        # A correction replaces the unit's whole contribution: every active
        # fragment of this unit and metric is superseded by a corrected one,
        # with the total count allocated across fragments as before.  Nothing
        # is deleted; the superseded receipts stay in the store.
        saved = []
        for r in rows:
            share = r["n_effective"] / claimed_total
            new_id = f"{r['summary_id']}-inj{_revision_seq(ds_id, r['summary_id'])}"
            summary = Summary(new_id, unit_id, r["batch_id"], r["metric"], r["period"],
                              mean, max(std, 0.01), max(n * share, 0.01), lo, hi,
                              revision_of=r["summary_id"])
            store.insert_summary(conn(), ds_id, summary, source="injected", injected=True,
                                 note="演示注入：替换该单位贡献")
            saved.append(new_id)
        store.record_decision(conn(), ds_id, "inject_revision", {
            "unit_id": unit_id, "summary_ids": saved,
            "replaces": [r["summary_id"] for r in rows], "metric": base["metric"],
            "mean_before": [r["mean"] for r in rows], "mean_after": mean,
            "n_before": [r["n_effective"] for r in rows], "n_after": n,
        })
    else:
        bid = body.get("batch_id") or f"注入批次-{_next_split_index(ds_id, unit_id):02d}"
        new_id = f"{base['summary_id']}-split{_next_split_index(ds_id, unit_id)}"
        summary = Summary(new_id, unit_id, bid, base["metric"], base["period"],
                          mean, max(std, 0.01), n, lo, hi)
        store.insert_summary(conn(), ds_id, summary, source="injected", injected=True,
                             note="演示注入：拆报（权重应被封顶，不增加总权重）")
        saved = [new_id]
        store.record_decision(conn(), ds_id, "inject_split", {
            "unit_id": unit_id, "summary_id": new_id, "batch_id": bid,
            "metric": base["metric"], "n": n, "mean": mean,
        })
    return {"saved": saved, "analysis": _analysis_payload(ds_id, q)}


def _next_split_index(ds_id: str, unit_id: str) -> int:
    rows = conn().execute(
        "SELECT COUNT(*) AS c FROM summaries WHERE dataset_id = ? AND unit_id = ? AND source = 'injected'",
        (ds_id, unit_id),
    ).fetchone()
    return int(rows["c"]) + 1


def h_isolate(ds_id, body, q):
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise store.Conflict("隔离必须填写原因")
    decided_by = str(body.get("decided_by") or "分析员").strip()[:80]
    unit_id = body.get("unit_id")
    if not unit_id:
        raise store.Conflict("缺少 unit_id")
    ds, cfg, rows, summaries, isolated, bad_units, truth = _context(ds_id, q)
    if unit_id in isolated:
        raise store.Conflict("该单位已被隔离")
    before = analyze(summaries, config=cfg, isolated_units=isolated, truth=truth, bad_units=bad_units)
    after = analyze(summaries, config=cfg, isolated_units=isolated | {unit_id}, truth=truth, bad_units=bad_units)
    frags = [c for c in before["contributions"] if c["unit_id"] == unit_id]
    iso_id = store.add_isolation(
        conn(), ds_id, unit_id=unit_id, metric=cfg.metric, period=cfg.period,
        reason=reason, decided_by=decided_by,
        before_center=before["estimates"][cfg.estimator]["center"],
        after_center=after["estimates"][cfg.estimator]["center"],
        before_weight=sum(f["weight"] for f in frags),
        after_weight=0.0,
        before_trimmed=before["estimates"]["trimmed"]["center"],
        after_trimmed=after["estimates"]["trimmed"]["center"],
    )
    store.record_decision(conn(), ds_id, "isolate", {
        "unit_id": unit_id, "reason": reason, "decided_by": decided_by,
        "center_before": before["estimates"][cfg.estimator]["center"],
        "center_after": after["estimates"][cfg.estimator]["center"],
    })
    return {"isolation_id": iso_id, "analysis": _analysis_payload(ds_id, q)}


def h_release(ds_id, body, q):
    iso_id = body.get("isolation_id")
    note = str(body.get("note") or "").strip()
    if not iso_id or not note:
        raise store.Conflict("释放隔离需要 isolation_id 与说明")
    ok = store.release_isolation(conn(), ds_id, iso_id, note=note)
    if not ok:
        raise store.Conflict("隔离记录不存在或已释放")
    store.record_decision(conn(), ds_id, "release_isolation", {"isolation_id": iso_id, "note": note})
    return {"released": iso_id, "analysis": _analysis_payload(ds_id, q)}


def h_simulate(ds_id, body, q):
    _dataset_or_404(ds_id)
    runs = max(10, min(400, int(body.get("runs", 100))))
    alpha = float(body.get("alpha", 0.15))
    cap = float(body.get("max_share", 0.10))
    seed = int(body.get("seed", 7))
    scenario = body.get("scenario", "all")
    if scenario == "all":
        result = {
            "scenarios": simulate_all(runs=runs, seed=seed, contamination_rate=alpha, max_share=cap),
            "null": null_calibration(runs=min(runs, 120), seed=seed + 1),
            "params": {"runs": runs, "alpha": alpha, "max_share": cap, "seed": seed},
        }
        result["order"] = list(SCENARIOS)
    else:
        if scenario not in SCENARIOS:
            raise store.Conflict(f"未知样例 {scenario!r}")
        result = {
            "scenarios": {scenario: simulate_scenario(scenario, runs=runs, seed=seed,
                                                      contamination_rate=alpha, max_share=cap)},
            "params": {"runs": runs, "alpha": alpha, "max_share": cap, "seed": seed},
            "order": [scenario],
        }
    store.cache_simulation(conn(), ds_id, "scenario", result.get("params", {}), result)
    store.record_decision(conn(), ds_id, "simulate", result.get("params", {}))
    return result


def h_simulation_latest(ds_id, body, q):
    _dataset_or_404(ds_id)
    return store.latest_simulation(conn(), ds_id, "scenario") or {"scenarios": {}, "order": []}

def h_audit(ds_id, body, q):
    return {"decisions": store.recent_decisions(conn(), ds_id), "isolations": store.isolations_all(conn(), ds_id)}


ROUTES = [
    ("GET", r"/api/datasets", h_datasets),
    ("POST", r"/api/datasets/demo", h_demo),
    ("GET", r"/api/dataset/{id}/panel", h_panel),
    ("GET", r"/api/dataset/{id}/analysis", h_analysis),
    ("GET", r"/api/dataset/{id}/compare", h_compare),
    ("GET", r"/api/dataset/{id}/batches", h_batches),
    ("GET", r"/api/dataset/{id}/batch", h_batch),
    ("GET", r"/api/dataset/{id}/unit/{unit}", h_unit),
    ("GET", r"/api/dataset/{id}/trace", h_trace),
    ("POST", r"/api/dataset/{id}/intake", h_intake),
    ("POST", r"/api/dataset/{id}/revise", h_revise),
    ("POST", r"/api/dataset/{id}/inject", h_inject),
    ("POST", r"/api/dataset/{id}/isolate", h_isolate),
    ("POST", r"/api/dataset/{id}/release", h_release),
    ("POST", r"/api/dataset/{id}/simulate", h_simulate),
    ("GET", r"/api/dataset/{id}/simulation", h_simulation_latest),
    ("GET", r"/api/dataset/{id}/audit", h_audit),
]


def match(method: str, path: str):
    parts = [p for p in path.split("/") if p]
    for m, pattern, fn in ROUTES:
        if m != method:
            continue
        pp = [p for p in pattern.split("/") if p]
        if len(pp) != len(parts):
            continue
        params = {}
        ok = True
        for want, got in zip(pp, parts):
            if want.startswith("{") and want.endswith("}"):
                # Path segments arrive percent-encoded; unit ids are Chinese.
                params[want[1:-1]] = unquote(got)
            elif want != got:
                ok = False
                break
        if ok:
            return fn, params
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "robustagg/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("ROBUSTAGG_LOG"):
            super().log_message(fmt, *args)

    def _send(self, code, payload, *, content_type="application/json; charset=utf-8", raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False, default=_json_default).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise store.Conflict("请求体过大")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise store.Conflict(f"JSON 解析失败: {exc}") from exc

    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = (STATIC / rel).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self._send(403, {"error": "forbidden"})
            return
        if not target.exists() or target.is_dir():
            target = STATIC / "index.html"
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, None, content_type=ctype, raw=target.read_bytes())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._dispatch("GET", parsed)
        else:
            self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        self._dispatch("POST", parsed)

    def _dispatch(self, method: str, parsed):
        fn, params = match(method, parsed.path)
        if fn is None:
            self._send(404, {"error": "unknown endpoint", "path": parsed.path})
            return
        q = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._read_json() if method == "POST" else {}
            with _lock:
                ds_id = params.get("id") or params.get("dataset")
                if "unit" in params:
                    result = fn(ds_id, params["unit"], body, q)
                elif ds_id:
                    result = fn(ds_id, body, q)
                else:
                    result = fn(body, q)
            self._send(200, result)
        except KeyError as exc:
            self._send(404, {"error": f"数据集不存在: {exc.args[0] if exc.args else ''}"})
        except store.Conflict as exc:
            self._send(422, {"error": str(exc)})
        except ValueError as exc:
            self._send(422, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"内部错误: {exc}"})


def _json_default(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(value)


def ensure_seed_data() -> str:
    with _lock:
        ds = store.latest_dataset(conn())
        if ds:
            return ds["id"]
    body = {"scenario": "coordinate", "seed": 20261124}
    return h_demo(body, {})["dataset_id"]


def run(host: str = "127.0.0.1", port: int = 8765, *, seed: bool = True):
    ds_id = ensure_seed_data() if seed else None
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"robustagg listening on http://{host}:{port}  (dataset {ds_id})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=DATASET_DB)
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args()
    DATASET_DB = args.db
    run(args.host, args.port, seed=not args.no_seed)
