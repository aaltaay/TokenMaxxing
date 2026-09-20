"""Integrity checks for observed Cursor values and complete event ranges."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import cursor_usage as cu


class NumericIntegrityTests(unittest.TestCase):
    def test_unknown_is_not_zero(self):
        for value in (None, "", "not a number", "nan", "inf"):
            self.assertIsNone(cu.to_float(value))
            self.assertIsNone(cu.to_int(value))
        self.assertEqual(cu.to_float(0), 0)
        self.assertEqual(cu.to_int("0"), 0)

    def test_zero_cost_does_not_fall_back_to_another_field(self):
        self.assertEqual(cu._event_cents({"tokenUsage": {"totalCents": 0}, "chargedCents": 10}), 0)
        self.assertIsNone(cu._event_cents({}))
        self.assertIsNone(cu._event_cents({"chargedCents": 10}))

    def test_model_names_do_not_imply_pool_membership(self):
        self.assertEqual(cu.pool_for_model("grok-4.6"), "unknown")
        self.assertEqual(cu.pool_for_model("model-a", ["model-a"]), "cursor")
        self.assertEqual(cu.pool_for_model("claude-example", ["model-a"]), "unknown")

    def test_local_missing_context_is_unavailable(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:x", "{}"))
        try:
            result = cu.snapshot_for(con, "x")
        finally:
            con.close()
        for field in ("used", "limit", "pct", "overhead", "conversation", "tax_pct", "num_sub"):
            self.assertIsNone(result[field], field)

    def test_missing_event_metrics_remain_unknown_in_sums(self):
        events = [{"model": "a", "timestamp": 1, "isHeadless": True,
                   "tokenUsage": {"totalCents": 2, "inputTokens": 5}},
                  {"model": "a", "timestamp": 2, "isHeadless": True,
                   "tokenUsage": {"totalCents": 0}}]
        result = cu._event_summaries(events, {}, {}, [])
        self.assertEqual(result["headless"], {"n": 2, "cents": 2})
        self.assertIsNone(result["event_models"][0]["in"])
        self.assertIsNone(result["event_models"][0]["out"])

    def test_missing_headless_flag_is_not_classified_as_interactive(self):
        result = cu._event_summaries([{"model": "a", "tokenUsage": {"totalCents": 1}}], {}, {}, [])
        self.assertIsNone(result["interactive"]["n"])
        self.assertIsNone(result["headless"]["n"])
        self.assertEqual(result["unclassified"]["n"], 1)

    def test_personalized_and_pricing_stories_are_removed(self):
        self.assertEqual(cu.build_findings({}), [])


class PaginationTests(unittest.TestCase):
    def test_more_than_twelve_pages_are_fetched(self):
        pages = [{"totalUsageEventsCount": 13, "usageEventsDisplay": [{"id": n}]} for n in range(13)]
        with patch.object(cu, "dashboard_request", side_effect=pages) as request:
            result = cu.fetch_events("unused", {})
        self.assertTrue(result["events_complete"])
        self.assertEqual(len(result["events"]), 13)
        self.assertEqual(request.call_count, 13)

    def test_no_total_count_does_not_claim_completion(self):
        with patch.object(cu, "dashboard_request", return_value={"usageEventsDisplay": [{"id": 1}]}):
            result = cu.fetch_events("unused", {})
        self.assertFalse(result["events_complete"])
        self.assertIsNone(result["events_total"])

    def test_page_limit_is_explicitly_partial(self):
        with patch.object(cu, "dashboard_request", return_value={"totalUsageEventsCount": 5, "usageEventsDisplay": [{"id": 1}]}):
            result = cu.fetch_events("unused", {}, max_pages=1)
        self.assertFalse(result["events_complete"])
        self.assertIn("limit", result["events_incomplete_reason"])

    def test_time_limit_does_not_make_a_request(self):
        with patch.object(cu, "dashboard_request") as request:
            result = cu.fetch_events("unused", {}, max_seconds=0)
        request.assert_not_called()
        self.assertFalse(result["events_complete"])

    def test_page_error_retains_partial_status(self):
        pages = [{"totalUsageEventsCount": 2, "usageEventsDisplay": [{"id": 1}]}, RuntimeError("secret")]
        with patch.object(cu, "dashboard_request", side_effect=pages):
            result = cu.fetch_events("unused", {})
        self.assertEqual(len(result["events"]), 1)
        self.assertFalse(result["events_complete"])
        self.assertNotIn("secret", result["events_incomplete_reason"])

    def test_repeated_page_and_changing_count_are_rejected(self):
        first = {"totalUsageEventsCount": 3, "usageEventsDisplay": [{"id": 1}]}
        for second in (first, {"totalUsageEventsCount": 4, "usageEventsDisplay": [{"id": 2}]}):
            with self.subTest(second=second), patch.object(cu, "dashboard_request", side_effect=[first, second]):
                result = cu.fetch_events("unused", {})
            self.assertFalse(result["events_complete"])

    def test_observed_empty_range_is_complete_zero(self):
        with patch.object(cu, "dashboard_request", return_value={"totalUsageEventsCount": 0, "usageEventsDisplay": []}):
            result = cu.fetch_events("unused", {})
        self.assertTrue(result["events_complete"])
        self.assertEqual(result["events_total"], 0)


class ReportTests(unittest.TestCase):
    def fetch_mock_report(self, summary=None, period=None, events=None, cache=None, current_identity=None):
        summary = summary if summary is not None else {}
        period = period if period is not None else {}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "cache.json"
            db = Path(directory) / "state.db"
            db.touch()
            if cache is not None:
                path.write_text(json.dumps(cache))
            for name, value in (("cache_path", path), ("cursor_state_db", db),
                                ("connect", MagicMock()), ("session_cookie", "unused"),
                                ("access_token", None),
                                ("chat_names", {}), ("cloud_agent_names", {})):
                stack.enter_context(patch.object(cu, name, return_value=value))
            stack.enter_context(patch.object(cu, "item_text", side_effect=lambda con, key:
                                            "test@example.com" if key == "cursorAuth/cachedEmail" else None))
            if current_identity is not None:
                stack.enter_context(patch.object(cu, "_current_cursor_identity", return_value=current_identity))
            def response(cookie, endpoint, *args, **kwargs):
                return {"/api/auth/me": {"id": "test"}, "/api/usage-summary": summary,
                        "/api/dashboard/get-current-period-usage": period,
                        "/api/dashboard/get-aggregated-usage-events": {"aggregations": [], "totalInputTokens": 0}}[endpoint]
            request = stack.enter_context(patch.object(cu, "dashboard_request", side_effect=response))
            if events is not None:
                stack.enter_context(patch.object(cu, "fetch_events", return_value=events))
            result = cu.fetch_cycle()
            return result, request.call_count

    def test_missing_source_values_are_not_fake_zeros_or_cycle_dates(self):
        report, _ = self.fetch_mock_report()
        for field in ("api_pct", "auto_pct", "total_pct", "total_spend_cents", "included_cents",
                      "bonus_cents", "on_demand_enabled", "elapsed_days", "remain_days", "agg_input"):
            self.assertIsNone(report[field], field)
        self.assertEqual(report["schema_version"], 2)
        self.assertFalse(report["events_complete"])

    def test_partial_events_are_not_presented_as_cycle_totals(self):
        summary = {"billingCycleStart": "2026-09-01T00:00:00Z", "billingCycleEnd": "2026-10-01T00:00:00Z"}
        events = {"events": [{"model": "a", "tokenUsage": {"totalCents": 5}}],
                  "events_total": 20, "events_complete": False,
                  "events_incomplete_reason": "timeout", "events_pages": 1}
        report, _ = self.fetch_mock_report(summary=summary, events=events)
        self.assertEqual(report["events_fetched"], 1)
        self.assertEqual(report["events_total"], 20)
        self.assertEqual(report["days"], [])
        self.assertEqual(report["conversations"], [])
        self.assertIsNone(report["headless"]["n"])
        self.assertEqual(report["agg_input"], 0)

    def test_recorded_zero_spend_does_not_fall_back(self):
        report, _ = self.fetch_mock_report(summary={"individualUsage": {"plan": {"breakdown": {"total": 99}}}},
                                          period={"planUsage": {"totalSpend": 0}})
        self.assertEqual(report["total_spend_cents"], 0)

    def test_old_schema_and_expired_cache_are_rejected(self):
        for cache in ({"fetched_at": time.time(), "api_pct": 100},
                      {"schema_version": 2, "source": "cursor_dashboard", "fetched_at": 1, "api_pct": 100}):
            report, calls = self.fetch_mock_report(cache=cache)
            self.assertGreater(calls, 0)
            self.assertIsNone(report["api_pct"])

    def test_current_cache_keeps_source_timestamp(self):
        now = time.time()
        report, calls = self.fetch_mock_report(cache={"schema_version": 2, "source": "cursor_dashboard",
                                                     "fetched_at": now, "api_pct": 0, "email": "test@example.com"})
        self.assertEqual(calls, 0)
        self.assertEqual(report["fetched_at"], now)
        self.assertEqual(report["api_pct"], 0)
        self.assertTrue(report["cached"])

    def test_other_account_cache_triggers_a_new_request(self):
        report, calls = self.fetch_mock_report(cache={"schema_version": 2, "source": "cursor_dashboard",
                                                     "fetched_at": time.time(), "api_pct": 99,
                                                     "email": "previous@example.com"})
        self.assertGreater(calls, 0)
        self.assertIsNone(report["api_pct"])
        self.assertEqual(report["email"], "test@example.com")

    def test_account_switch_during_fetch_rejects_result(self):
        with self.assertRaisesRegex(RuntimeError, "account changed"):
            self.fetch_mock_report(current_identity=("new-session", "other@example.com"))


class CacheValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cycle-cache.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cu, "cache_path", return_value=self.path))
        self.identity = self.stack.enter_context(patch.object(
            cu, "_current_cursor_identity", return_value=("test-session", "test@example.com")))
        self.report = {"schema_version": cu.SCHEMA_VERSION, "source": "cursor_dashboard",
                       "fetched_at": time.time(), "email": "test@example.com", "api_pct": 17}

    def write_cache(self, report=None):
        self.path.write_text(json.dumps(self.report if report is None else report), encoding="utf-8")

    def test_same_account_cache_keeps_original_timestamp_and_zero(self):
        self.report.update(email="TEST@example.com", api_pct=0)
        self.write_cache()
        result = cu.read_cached_cycle()
        self.assertEqual(result["fetched_at"], self.report["fetched_at"])
        self.assertEqual(result["api_pct"], 0)
        self.assertTrue(result["cached"])
        self.assertFalse(result["stale"])

    def test_logout_unknown_identity_and_switch_reject_cache(self):
        self.write_cache()
        for identity in ((None, "test@example.com"), ("session", None), ("session", "other@example.com")):
            with self.subTest(identity=identity):
                self.identity.return_value = identity
                self.assertIsNone(cu.read_cached_cycle())

    def test_invalid_source_schema_and_timestamp_reject_cache(self):
        for change in ({"schema_version": 1}, {"source": "fixture"}, {"fetched_at": None},
                       {"fetched_at": True}, {"fetched_at": "123"}, {"fetched_at": float("nan")},
                       {"fetched_at": -1}, {"fetched_at": time.time() + 1000}, {"email": None}):
            with self.subTest(change=change):
                self.write_cache({**self.report, **change})
                self.assertIsNone(cu.read_cached_cycle(allow_stale=True))
        self.write_cache([])
        self.assertIsNone(cu.read_cached_cycle())

    def test_aged_cache_requires_explicit_request_and_stays_stale(self):
        self.report["fetched_at"] -= cu.CACHE_TTL_S + 1
        self.write_cache()
        self.assertIsNone(cu.read_cached_cycle())
        result = cu.read_cached_cycle(allow_stale=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["fetched_at"], self.report["fetched_at"])

    def test_unreadable_identity_never_trusts_cached_data(self):
        self.write_cache()
        self.identity.side_effect = sqlite3.OperationalError("database unavailable")
        self.assertIsNone(cu.read_cached_cycle())


class FetchCoordinationTests(unittest.TestCase):
    def test_overlapping_force_refresh_reports_in_progress_without_using_cache(self):
        entered, finish = threading.Event(), threading.Event()
        results = []

        def fetch(**kwargs):
            entered.set()
            finish.wait(2)
            return {"fresh": True}

        with patch.object(cu, "_fetch_cycle", side_effect=fetch) as underlying:
            worker = threading.Thread(target=lambda: results.append(cu.fetch_cycle(force=True)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(RuntimeError, "already in progress"):
                    cu.fetch_cycle(force=True)
                self.assertEqual(underlying.call_count, 1)
            finally:
                finish.set()
                worker.join(2)
        self.assertEqual(results, [{"fresh": True}])

    def test_failed_fetch_releases_lock_for_retry(self):
        with patch.object(cu, "_fetch_cycle", side_effect=[RuntimeError("unavailable"), {"fresh": True}]):
            with self.assertRaises(RuntimeError):
                cu.fetch_cycle(force=True)
            self.assertEqual(cu.fetch_cycle(force=True), {"fresh": True})

    def test_request_budget_shrinks_and_exhaustion_stops_new_requests(self):
        with patch.object(cu.time, "monotonic", side_effect=[100, 140, 150]):
            self.assertEqual(cu._request_timeout(150, 45), 45)
            self.assertEqual(cu._request_timeout(150, 45), 10)
            with self.assertRaises(TimeoutError):
                cu._request_timeout(150, 45)


if __name__ == "__main__":
    unittest.main()
