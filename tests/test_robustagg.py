"""End-to-end tests: estimators, cap invariants, revisions, intake, bounds, isolation.

Run with ``python3 -m unittest discover -s tests -v`` (standard library only).
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robustagg import server, store  # noqa: E402
from robustagg.analysis import (  # noqa: E402
    AnalysisConfig,
    analyze,
    build_contributions,
    cap_feasible,
    compare,
    resolve_cap,
    unit_influence,
)
from robustagg.bounds import trimmed_bound  # noqa: E402
from robustagg.estimators import Summary, huber_center, median_center, trimmed_center  # noqa: E402
from robustagg.simulation import null_calibration, simulate_scenario  # noqa: E402
from robustagg.synthetic import SECONDARY_METRIC, generate  # noqa: E402


def make_summary(unit, batch="批次-甲-01", mean=100.0, n=100.0, std=10.0, metric="指标值",
                 sid=None, revision_of=None):
    return Summary(
        summary_id=sid or f"{unit}-{metric}", unit_id=unit, batch_id=batch, metric=metric,
        period="2026-W46", mean=mean, std=std, n_effective=n,
        range_low=mean - 4 * std, range_high=mean + 4 * std, revision_of=revision_of,
    )


def pairs_for(rows, cap_share=0.25, alpha=0.10):
    cfg = AnalysisConfig(metric="指标值", period="2026-W46",
                         contamination_rate=alpha, max_share=cap_share)
    contribs = build_contributions(rows, config=cfg)
    return [(c.summary.mean, c.weight) for c in contribs]


class EstimatorTests(unittest.TestCase):
    def test_trimmed_ignores_extreme_tail(self):
        rows = [(i, 1.0, 100.0 + i, 1.0) for i in range(20)]
        rows.append((20, 1.0, 10_000.0, 1.0))  # one gross outlier
        clean = trimmed_center(rows[:-1], 0.10)
        dirty = trimmed_center(rows, 0.10)
        self.assertAlmostEqual(clean["center"], dirty["center"], delta=1.1)
        self.assertLess(dirty["center"], 200.0)

    def test_trimmed_support_is_partial_only_at_the_boundary(self):
        rows = [(i, 1.0, float(i), 1.0) for i in range(10)]
        res = trimmed_center(rows, 0.15)
        partials = [s for s in res["support"] if s[3]]
        self.assertEqual(len(partials), 2)
        self.assertTrue(all(0 < s[1] < 1 for s in partials))

    def test_huber_resists_shift_but_keeps_center(self):
        rows = [(i, 1.0, 100.0 + (i % 5), 2.0) for i in range(24)]
        base = huber_center(rows)["center"]
        rows.append((99, 1.0, 100.0 + 40 * 25, 2.0))
        moved = huber_center(rows)["center"]
        self.assertAlmostEqual(base, moved, delta=2.0)
        self.assertAlmostEqual(median_center(rows)["center"], base, delta=3.0)

    def test_weighted_median_reads_cumulative_half(self):
        rows = [(0, 1.0, 10.0, 0.0), (1, 10.0, 20.0, 0.0), (2, 1.0, 30.0, 0.0)]
        self.assertEqual(median_center(rows)["center"], 20.0)


class CapInvariantTests(unittest.TestCase):
    def test_resolve_cap_solves_its_own_equation(self):
        claims = [50.0, 100.0, 400.0, 30.0, 60.0]
        for share in (0.20, 0.25, 0.5):
            self.assertTrue(cap_feasible(claims, share))
            cap = resolve_cap(claims, share)
            total = sum(min(c, cap) for c in claims)
            self.assertAlmostEqual(cap, share * total, places=6)
            self.assertAlmostEqual(max(min(c, cap) for c in claims) / total, share, places=6)

    def test_infeasible_share_is_reported_not_silently_collapsed(self):
        claims = [100.0] * 5
        self.assertFalse(cap_feasible(claims, 0.10))  # 5 units cannot each hold <=10%
        cfg = AnalysisConfig(metric="指标值", period="2026-W46", contamination_rate=0.1, max_share=0.1)
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0 + i) for i in range(5)]
        res = analyze(rows, config=cfg)
        self.assertTrue(all(c["weight"] > 0 for c in res["contributions"]))
        self.assertEqual(res["bound"]["status"], "void")
        self.assertIn("cap", {a["id"] for a in res["bound"]["assumptions"] if a["status"] == "unmet"})

    def test_split_report_cannot_buy_more_weight(self):
        cfg = AnalysisConfig(metric="指标值", period="2026-W46", contamination_rate=0.1, max_share=0.1)
        whole = [make_summary(f"u{i}", n=100.0, mean=100.0 + i) for i in range(24)]
        whole[0] = make_summary("u0", n=400.0, mean=105.0)

        pieces = [s for s in whole if s.unit_id != "u0"]
        pieces += [
            make_summary("u0", batch="批次-乙-02", n=200.0, mean=105.0, sid="u0-a"),
            make_summary("u0", batch="批次-丙-03", n=120.0, mean=105.0, sid="u0-b"),
            make_summary("u0", batch="批次-丁-04", n=80.0, mean=105.0, sid="u0-c"),
        ]
        cw = build_contributions(whole, config=cfg)
        cp = build_contributions(pieces, config=cfg)
        w_whole = sum(c.weight for c in cw if c.unit_id == "u0")
        w_split = sum(c.weight for c in cp if c.unit_id == "u0")
        total_w = sum(c.weight for c in cw)
        total_p = sum(c.weight for c in cp)
        self.assertAlmostEqual(w_whole, w_split, places=9)
        self.assertAlmostEqual(w_split / total_p, 0.10, places=6)
        self.assertAlmostEqual(w_whole / total_w, 0.10, places=6)

    def test_split_fragments_share_the_capped_unit_weight(self):
        cfg = AnalysisConfig(metric="指标值", period="2026-W46", contamination_rate=0.1, max_share=0.2)
        rows = [make_summary(f"u{i}", n=100.0, sid=f"u{i}") for i in range(12)]
        rows += [
            make_summary("big", batch="批次-乙-02", n=900.0, sid="big-1"),
            make_summary("big", batch="批次-丙-03", n=300.0, sid="big-2"),
        ]
        contribs = build_contributions(rows, config=cfg)
        frags = [c for c in contribs if c.unit_id == "big"]
        self.assertEqual(len(frags), 2)
        self.assertTrue(all(f.split for f in frags))
        self.assertAlmostEqual(sum(f.weight for f in frags), frags[0].unit_weight, places=9)
        self.assertAlmostEqual(frags[0].weight / frags[1].weight, 3.0, places=6)  # 900:300 split


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = store.connect(self.path)
        server._conn = self.conn  # handlers use the module connection
        self.ds = store.create_dataset(self.conn, name="t", scenario="baseline", truth=100.0)

    def tearDown(self):
        server._conn = None
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_revision_replaces_instead_of_adding(self):
        original = make_summary("u1", n=100.0, mean=100.0, sid="u1-指标值")
        store.insert_summary(self.conn, self.ds, original, source="intake")
        self.assertEqual(len(store.active_rows(self.conn, self.ds)), 1)
        revised = make_summary("u1", n=100.0, mean=170.0, sid="u1-指标值-r1", revision_of="u1-指标值")
        store.insert_summary(self.conn, self.ds, revised, source="correction")
        active = store.active_rows(self.conn, self.ds)
        self.assertEqual([r["summary_id"] for r in active], ["u1-指标值-r1"])
        # The original row still exists and is listed in the unit history.
        history = store.history_for_unit(self.conn, self.ds, "u1")
        self.assertEqual(len(history), 2)
        superseded = [r for r in history if r["superseded"]]
        self.assertEqual([r["summary_id"] for r in superseded], ["u1-指标值"])

    def test_revising_a_superseded_receipt_is_rejected_over_http(self):
        store.insert_summary(self.conn, self.ds, make_summary("u1", sid="u1-指标值"), source="intake")
        store.insert_summary(
            self.conn, self.ds,
            make_summary("u1", mean=120.0, sid="u1-指标值-r1", revision_of="u1-指标值"),
            source="correction",
        )
        with self.assertRaises(store.Conflict):
            server.h_revise(self.ds, {"summary_id": "u1-指标值", "mean": 130.0}, {})

    def test_two_fragments_for_one_unit_across_batches_are_treated_as_a_split(self):
        store.insert_summary(self.conn, self.ds, make_summary("u1", n=60.0, sid="a", batch="批次-甲-01"))
        store.insert_summary(self.conn, self.ds, make_summary("u1", n=40.0, sid="b", batch="批次-乙-02"))
        for i in range(5):
            store.insert_summary(self.conn, self.ds, make_summary(f"o{i}", n=80.0, sid=f"o{i}"))
        active = [r for r in store.active_rows(self.conn, self.ds) if r["unit_id"] == "u1"]
        cfg = AnalysisConfig(metric="指标值", period="2026-W46", max_share=0.8)
        contribs = build_contributions(store.rows_to_summaries(active), config=cfg)
        self.assertTrue(all(c.split for c in contribs))
        self.assertAlmostEqual(sum({c.unit_id: c.unit_weight for c in contribs}.values()), 100.0)


    def _insert_eight(self):
        for i in range(8):
            store.insert_summary(self.conn, self.ds, make_summary(f"u{i}", n=100.0, mean=100.0 + i,
                                                                  sid=f"u{i}"), source="demo")

    def test_correction_replaces_the_contribution_and_keeps_the_original(self):
        self._insert_eight()
        out = server.h_revise(self.ds, {"summary_id": "u3", "mean": 250.0, "note": "换算错误"}, {})
        active = {r["summary_id"]: r for r in store.active_rows(self.conn, self.ds)}
        self.assertNotIn("u3", active)
        self.assertEqual(active[out["saved"][0]]["mean"], 250.0)
        # Original row is still on disk, marked superseded in history.
        history = store.history_for_unit(self.conn, self.ds, "u3")
        self.assertEqual([h["summary_id"] for h in history], ["u3", out["saved"][0]])
        self.assertTrue(history[0]["superseded"])
        self.assertFalse(history[1]["superseded"])
        # One active contribution, not two: the weight total did not grow.
        self.assertEqual(len(store.active_rows(self.conn, self.ds)), 8)

    def test_correction_of_an_already_superseded_receipt_is_refused(self):
        self._insert_eight()
        server.h_revise(self.ds, {"summary_id": "u3", "mean": 250.0}, {})
        with self.assertRaisesRegex(store.Conflict, "已被更正"):
            server.h_revise(self.ds, {"summary_id": "u3", "mean": 260.0}, {})


class IntakePolicyTests(unittest.TestCase):
    def test_forbidden_row_level_keys_are_rejected(self):
        raw = {
            "unit_id": "u1", "batch_id": "b", "metric": "m", "period": "p",
            "mean": 1.0, "std": 1.0, "n_effective": 10, "range_low": 0, "range_high": 2,
            "records": [[1, 2], [3, 4]],
        }
        with self.assertRaisesRegex(store.Conflict, "不接收原始个人记录"):
            server.validate_receipt(raw)

    def test_unknown_fields_are_rejected(self):
        raw = {
            "unit_id": "u1", "batch_id": "b", "metric": "m", "period": "p",
            "mean": 1.0, "std": 1.0, "n_effective": 10, "range_low": 0, "range_high": 2,
            "worker_scores": [1, 2, 3],
        }
        with self.assertRaises(store.Conflict):
            server.validate_receipt(raw)

    def test_mean_must_sit_inside_claimed_range(self):
        raw = {
            "unit_id": "u1", "batch_id": "b", "metric": "m", "period": "p",
            "mean": 99.0, "std": 1.0, "n_effective": 10, "range_low": 0, "range_high": 2,
        }
        with self.assertRaises(store.Conflict):
            server.validate_receipt(raw)

    def test_vector_receipt_expands_to_one_receipt_per_component(self):
        raw = {
            "unit_id": "单位-07", "batch_id": "批次-甲-01", "period": "2026-W46",
            "n_effective": 140,
            "mean_vector": {"指标值": 98.2, "伴随指标": 70.4},
            "std_vector": {"指标值": 11.4, "伴随指标": 6.1},
        }
        rows = server.expand_vector_receipt(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["metric"] for r in rows}, {"指标值", "伴随指标"})
        self.assertEqual({r["summary_id"] for r in rows}, {"单位-07-2026-W46-指标值", "单位-07-2026-W46-伴随指标"})
        for r in rows:
            s = server.validate_receipt(r)
            self.assertLessEqual(s.range_low, s.mean)
            self.assertGreaterEqual(s.range_high, s.mean)

    def test_vector_receipt_may_not_mix_scalar_mean(self):
        with self.assertRaises(store.Conflict):
            server.expand_vector_receipt({
                "unit_id": "u", "batch_id": "b", "period": "p", "mean": 1.0,
                "mean_vector": {"a": 1.0},
            })


class RoutingTests(unittest.TestCase):
    def test_percent_encoded_unit_segment_is_decoded(self):
        fn, params = server.match("GET", "/api/dataset/ds-1/unit/%E5%8D%95%E4%BD%8D-10")
        self.assertIs(fn, server.h_unit)
        self.assertEqual(params["unit"], "单位-10")

    def test_unknown_path_matches_nothing(self):
        fn, _ = server.match("POST", "/api/dataset/ds-1/nope")
        self.assertIsNone(fn)


class InjectionTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = store.connect(self.path)
        server._conn = self.conn
        self.ds = store.create_dataset(self.conn, name="t", scenario="baseline", truth=100.0,
                                       metadata={"metric": "指标值", "metrics": ["指标值"]})
        for i in range(12):
            store.insert_summary(self.conn, self.ds, make_summary(f"u{i:02d}", n=100.0, mean=100.0 + i,
                                                                  sid=f"u{i:02d}"), source="demo")

    def tearDown(self):
        server._conn = None
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_revision_injection_supersedes_every_active_fragment(self):
        # Give u00 a second active fragment in another batch first.
        store.insert_summary(self.conn, self.ds,
                             make_summary("u00", n=50.0, mean=101.0, sid="u00-extra", batch="批次-乙-02"),
                             source="demo")
        out = server.h_inject(self.ds, {"unit_id": "u00", "metric": "指标值",
                                        "mode": "revision", "mean": 500.0, "n_effective": 700.0}, {})
        self.assertEqual(len(out["saved"]), 2)
        active = [r for r in store.active_rows(self.conn, self.ds) if r["unit_id"] == "u00"]
        self.assertEqual({r["summary_id"] for r in active}, set(out["saved"]))
        self.assertTrue(all(r["revision_of"] for r in active))
        # Original receipts are all still on disk.
        history = store.history_for_unit(self.conn, self.ds, "u00")
        self.assertEqual(len(history), 4)

    def test_split_injection_is_absorbed_by_the_cap(self):
        # Three extra fragments claiming 3000 each must not lift u05's share
        # above the 20% ceiling, however many batches they arrive in.
        for _ in range(3):
            out = server.h_inject(self.ds, {"unit_id": "u05", "metric": "指标值",
                                            "mode": "split", "mean": 400.0, "n_effective": 3000.0},
                                  {"max_share": "0.2"})
        res = out["analysis"]
        by_unit = {}
        for c in res["contributions"]:
            by_unit[c["unit_id"]] = by_unit.get(c["unit_id"], 0.0) + c["weight"]
        share = max(by_unit.values()) / res["weighting"]["total_weight"]
        self.assertLessEqual(share, 0.20 + 1e-9)
        self.assertAlmostEqual(share, 0.20, places=6)  # cap binds, and binds exactly
        self.assertTrue(res["weighting"]["cap_binding"])
        self.assertIn("u05", res["weighting"]["split_units"])
        self.assertEqual(sum(1 for c in res["contributions"] if c["unit_id"] == "u05"), 4)

    def test_isolation_requires_a_reason(self):
        with self.assertRaisesRegex(store.Conflict, "原因"):
            server.h_isolate(self.ds, {"unit_id": "u03", "reason": "   "}, {})

    def test_release_requires_a_note(self):
        iso = server.h_isolate(self.ds, {"unit_id": "u03", "reason": "可疑"}, {})["isolation_id"]
        with self.assertRaisesRegex(store.Conflict, "说明"):
            server.h_release(self.ds, {"isolation_id": iso, "note": ""}, {})


class DispatchLockTests(unittest.TestCase):
    """A handler that takes the module lock must not deadlock under _dispatch.

    Every request already holds ``_lock`` when the handler runs; the intake
    handler takes it again to make its check-then-insert atomic.  With a plain
    ``Lock`` that second acquire hung forever, so no intake POST ever returned.
    """

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = store.connect(self.path)
        server._conn = self.conn
        self.ds = store.create_dataset(self.conn, name="t", scenario="baseline", truth=100.0,
                                       metadata={"metric": "指标值", "metrics": ["指标值"]})

    def tearDown(self):
        server._conn = None
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_intake_completes_while_the_dispatch_lock_is_held(self):
        receipt = {
            "summary_id": "S-1", "unit_id": "u1", "batch_id": "b1", "metric": "指标值",
            "period": "2026-W46", "mean": 100.0, "std": 5.0, "n_effective": 40,
            "range_low": 80, "range_high": 120,
        }
        done = []

        def run():
            # _dispatch wraps every handler call in this same lock.
            with server._lock:
                done.append(server.h_intake(self.ds, {"receipts": [receipt]}, {}))
            done.append("returned")

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "h_intake deadlocked while _lock was held")
        self.assertEqual(done[-1], "returned")
        self.assertEqual(done[0]["saved"], ["S-1"])

    def test_intake_note_is_stored_and_audited(self):
        receipt = {
            "summary_id": "S-2", "unit_id": "u2", "batch_id": "b1", "metric": "指标值",
            "period": "2026-W46", "mean": 100.0, "std": 5.0, "n_effective": 40,
            "range_low": 80, "range_high": 120,
        }
        server.h_intake(self.ds, {"receipts": [receipt], "note": "补报上周漏报"}, {})
        row = store.receipt_by_summary_id(self.conn, self.ds, "S-2")
        self.assertEqual(row["note"], "补报上周漏报")
        decision = store.recent_decisions(self.conn, self.ds)[0]
        self.assertEqual(decision["payload"]["note"], "补报上周漏报")


class BoundTests(unittest.TestCase):
    def test_bound_matches_its_formula(self):
        rows = [make_summary(f"u{i}", n=100.0, mean=90.0 + 2 * i) for i in range(20)]
        res = trimmed_bound(pairs_for(rows), 0.10, max_share=0.10, n_units=20)
        span = 90.0 + 2 * 19 - 90.0
        expected = span * (0.10 + 0.10 + max(0.0, 0.10 - 0.10)) / 0.8
        self.assertAlmostEqual(res["offset"], expected, places=9)
        self.assertIn(res["status"], ("conditional", "guaranteed"))
        self.assertEqual(res["interval"][1] - res["interval"][0], 2 * res["offset"])

    def test_bound_is_void_when_known_contamination_exceeds_alpha(self):
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0) for i in range(10)]
        res = trimmed_bound(pairs_for(rows), 0.05, max_share=0.1, n_units=10, known_bad_share=0.30)
        self.assertEqual(res["status"], "void")
        self.assertIsNone(res["interval"])
        unmet = [a["id"] for a in res["assumptions"] if a["status"] == "unmet"]
        self.assertIn("contamination_ceiling", unmet)

    def test_too_few_units_void_the_bound(self):
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0 + i) for i in range(2)]
        res = trimmed_bound(pairs_for(rows), 0.10, max_share=0.1, n_units=2)
        self.assertEqual(res["status"], "void")

    def test_cap_above_alpha_widens_the_bound(self):
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0 + i) for i in range(8)]
        pairs = pairs_for(rows)
        small = trimmed_bound(pairs, 0.10, max_share=0.10, n_units=8)["offset"]
        large = trimmed_bound(pairs, 0.10, max_share=0.30, n_units=8)["offset"]
        self.assertGreater(large, small)

    def test_a_void_bound_does_not_vouch_for_the_gap(self):
        # alpha=0.05 while known contamination is 10%: the bound is void, so
        # the numeric offset must not be reused to call the naive/trimmed gap
        # "explained by the assumption".
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0 + i) for i in range(8)]
        cfg = AnalysisConfig(metric="指标值", period="2026-W46",
                             contamination_rate=0.05, max_share=0.25)
        res = compare(rows, config=cfg, bad_units={"u7"})
        self.assertEqual(res["bound_status"], "void")
        self.assertIsNone(res["explained_by_assumption"])
        inf = unit_influence(rows, config=cfg, isolated_units=set(), unit_id="u7",
                             bad_units={"u7"})
        self.assertEqual(inf["bound_with"]["status"], "void")
        self.assertIsNone(inf["within_bound"])


class SimulationTests(unittest.TestCase):
    def test_robust_gain_under_coordinated_shift(self):
        res = simulate_scenario("coordinate", runs=24, seed=3, contamination_rate=0.12, max_share=0.10)
        self.assertGreater(res["gain_uncapped_vs_trimmed_p90"], 1.5)
        self.assertLess(res["results"]["trimmed"]["p90_abs_err"], res["results"]["naive_uncapped"]["p90_abs_err"])

    def test_bound_covers_truth_when_assumptions_hold(self):
        res = simulate_scenario("independent", runs=24, seed=5, contamination_rate=0.12, max_share=0.10)
        cov = res["bound_coverage"]
        self.assertGreater(cov["graded"], 0)
        self.assertEqual(cov["void"], 0)
        self.assertGreaterEqual(cov["rate"], 0.9)

    def test_bound_is_void_when_contamination_exceeds_assumption(self):
        res = simulate_scenario("over_ceiling", runs=16, seed=5, contamination_rate=0.08, max_share=0.10)
        self.assertEqual(res["bound_coverage"]["graded"], 0)
        self.assertEqual(res["bound_coverage"]["void"], res["runs"])

    def test_null_calibration_keeps_clean_panels_stable(self):
        res = null_calibration(runs=10, seed=2, alphas=(0.05, 0.20), cap_shares=(0.25,), n_units=20)
        for setting in res["by_setting"].values():
            self.assertLess(setting["results"]["trimmed"]["p90_shift"], 6.0)
            self.assertLess(setting["results"]["huber"]["p90_shift"], 6.0)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.conn = store.connect(self.path)
        server._conn = self.conn  # handlers use the module connection
        self.ds = store.create_dataset(self.conn, name="t", scenario="baseline", truth=100.0)

    def tearDown(self):
        server._conn = None
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_isolation_keeps_reason_impact_and_release_record(self):
        rows = [make_summary(f"u{i}", n=100.0, mean=100.0 + i, sid=f"u{i}") for i in range(10)]
        for r in rows:
            store.insert_summary(self.conn, self.ds, r, source="demo")
        cfg = AnalysisConfig(metric="指标值", period="2026-W46", contamination_rate=0.1, max_share=0.3)
        before = analyze(rows, config=cfg)
        after = analyze(rows, config=cfg, isolated_units={"u9"})
        iso_id = store.add_isolation(
            self.conn, self.ds, unit_id="u9", metric="指标值", period="2026-W46",
            reason="均值超出范围且无法复核", decided_by="测试",
            before_center=before["estimates"]["trimmed"]["center"],
            after_center=after["estimates"]["trimmed"]["center"],
            before_weight=before["weighting"]["total_weight"] / 10,
            after_weight=0.0,
            before_trimmed=before["estimates"]["trimmed"]["center"],
            after_trimmed=after["estimates"]["trimmed"]["center"],
        )
        self.assertEqual(store.active_isolated_units(self.conn, self.ds, metric="指标值", period="2026-W46"),
                         {"u9"})
        # Receipts are untouched by isolation.
        self.assertEqual(len(store.active_rows(self.conn, self.ds)), 10)
        self.assertTrue(store.release_isolation(self.conn, self.ds, iso_id, note="复核后确认合法"))
        self.assertEqual(store.active_isolated_units(self.conn, self.ds, metric="指标值", period="2026-W46"), set())
        records = store.isolations_all(self.conn, self.ds)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["reason"], "均值超出范围且无法复核")
        self.assertEqual(rec["release_note"], "复核后确认合法")
        self.assertAlmostEqual(rec["before_trimmed"], before["estimates"]["trimmed"]["center"])
        self.assertAlmostEqual(rec["after_trimmed"], after["estimates"]["trimmed"]["center"])
        self.assertNotEqual(rec["before_center"], rec["after_center"])


class GeneratorTests(unittest.TestCase):
    def test_panel_has_truth_per_component_and_matching_weights(self):
        panel = generate("coordinate", seed=20261124)
        self.assertEqual(set(panel.truth_by_metric), {"指标值", SECONDARY_METRIC})
        self.assertAlmostEqual(
            sum(u.true_mean * u.true_n for u in panel.units) / sum(u.true_n for u in panel.units),
            panel.truth, places=5,
        )
        self.assertAlmostEqual(
            sum(u.secondary_mean * u.true_n for u in panel.units) / sum(u.true_n for u in panel.units),
            panel.truth_by_metric[SECONDARY_METRIC], places=5,
        )
        # A unit reports both components with the same count.
        by_unit = {}
        for s in panel.all_summaries:
            by_unit.setdefault(s.unit_id, []).append(s)
        for frags in by_unit.values():
            self.assertEqual(len(frags), 2)
            self.assertEqual(len({f.n_effective for f in frags}), 1)

    def test_inflated_count_scenario_claims_more_weight(self):
        panel = generate("inflate_n", seed=7)
        bad = [u for u in panel.units if u.is_bad]
        self.assertTrue(bad)
        self.assertGreater(bad[0].corruption["n_multiplier"], 5)

    def test_truth_is_not_inflated_by_corruption(self):
        panel = generate("over_ceiling", seed=11)
        clean_mean = sum(u.true_mean * u.true_n for u in panel.units) / sum(u.true_n for u in panel.units)
        self.assertAlmostEqual(panel.truth, clean_mean, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
