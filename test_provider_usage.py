"""Quota regression tests: missing values never become a full or empty meter."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import provider_usage as pu


class NormalizeCodexTest(unittest.TestCase):
    def normalized(self, raw):
        return pu.normalize_codex({"rateLimits": {"primary": raw}}, fetched_at=100)

    def test_weekly_primary_does_not_invent_five_hour_window(self):
        result = self.normalized({"usedPercent": 3, "windowDurationMins": 10080,
                                  "resetsAt": 1790473180})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["windows"], [{"id": "codex:primary", "label": "Weekly",
            "used_percent": 3, "resets_at": 1790473180, "window_minutes": 10080}])
        self.assertEqual(result["fetched_at"], 100)

    def test_explicit_zero_is_real_usage(self):
        self.assertEqual(self.normalized({"usedPercent": 0})["windows"][0]["used_percent"], 0)

    def test_missing_null_boolean_strings_and_invalid_numbers_are_not_zero(self):
        values = [None, True, False, "0", "50", -1, 101, float("nan"), float("inf")]
        for value in values:
            with self.subTest(value=value):
                result = self.normalized({"usedPercent": value})
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["windows"], [])
        self.assertEqual(self.normalized({})["windows"], [])

    def test_missing_duration_and_reset_stay_null(self):
        window = self.normalized({"usedPercent": 12})["windows"][0]
        self.assertIsNone(window["window_minutes"])
        self.assertIsNone(window["resets_at"])
        self.assertEqual(window["label"], "Primary")

    def test_multi_bucket_response_is_authoritative(self):
        result = pu.normalize_codex({"rateLimits": {"primary": {"usedPercent": 1}},
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300}},
                "codex_other": {"limitName": "Other", "secondary": {
                    "usedPercent": 44, "windowDurationMins": 10080}},
            }})
        self.assertEqual([w["used_percent"] for w in result["windows"]], [10, 44])
        self.assertEqual([w["label"] for w in result["windows"]], ["5-hour", "Other · Weekly"])

    def test_empty_multi_bucket_response_does_not_use_legacy(self):
        result = pu.normalize_codex({"rateLimits": {"primary": {"usedPercent": 99}},
                                    "rateLimitsByLimitId": {}})
        self.assertEqual(result["windows"], [])

    def test_missing_and_invalid_payloads_are_unavailable(self):
        for raw in (None, [], {}, {"rateLimits": None}, {"rateLimits": {"primary": None}}):
            with self.subTest(raw=raw):
                self.assertEqual(pu.normalize_codex(raw)["status"], "unavailable")


class NormalizeClaudeTest(unittest.TestCase):
    def test_real_percentages_and_utc_reset(self):
        result = pu.normalize_claude({
            "five_hour": {"utilization": 0, "resets_at": "2026-09-20T06:00:00Z"},
            "seven_day": {"utilization": 23.5, "resets_at": None},
            "seven_day_sonnet": {"utilization": 17, "resets_at": None},
            "seven_day_opus": None,
            "extra_usage": {"utilization": 95},
        }, fetched_at=123)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fetched_at"], 123)
        self.assertEqual([w["used_percent"] for w in result["windows"]], [0, 23.5, 17])
        self.assertEqual(result["windows"][0]["resets_at"], 1789884000)
        self.assertEqual(result["windows"][2]["label"], "Weekly (sonnet)")

    def test_model_scoped_weekly_caps_and_the_surface_breakdown_are_read(self):
        reset = "2026-09-22T10:59:59.554149+00:00"
        result = pu.normalize_claude({
            "five_hour": {"utilization": 11.0, "resets_at": reset},
            "seven_day": {"utilization": 81.0, "resets_at": reset},
            "seven_day_sonnet": {"utilization": 5, "resets_at": reset},
            "limits": [
                {"kind": "session", "percent": 11, "resets_at": reset, "scope": None},
                {"kind": "weekly_all", "percent": 81, "resets_at": reset, "scope": None},
                {"kind": "weekly_scoped", "percent": 38, "resets_at": reset,
                 "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
                # Already reported under its own seven_day_* key: not added twice.
                {"kind": "weekly_scoped", "percent": 5, "resets_at": reset,
                 "scope": {"model": {"display_name": "Sonnet"}}},
                {"kind": "weekly_scoped", "percent": None, "scope": {"model": {"display_name": "Mystery"}}},
            ],
            "seven_day_breakdown": {"as_of": "2026-09-21T05:41:07Z", "rows": [
                {"key": "claude_code", "display_name": "Claude Code", "percent": 98},
                {"key": "cowork", "display_name": "Cowork", "percent": 2},
                {"key": "broken", "display_name": "Broken", "percent": "n/a"}]},
        }, fetched_at=1)
        self.assertEqual([w["label"] for w in result["windows"]],
                         ["5-hour", "Weekly", "Weekly (sonnet)", "Weekly (Fable only)"])
        fable = result["windows"][3]
        self.assertEqual((fable["id"], fable["used_percent"], fable["window_minutes"]), ("weekly_scoped:fable", 38, 10080))
        self.assertEqual(result["breakdown"]["rows"], [{"name": "Claude Code", "percent": 98}, {"name": "Cowork", "percent": 2}])

    def test_missing_percentage_is_not_zero(self):
        for raw in ({}, {"utilization": None}, {"utilization": "0"}, {"utilization": True}):
            self.assertEqual(pu.normalize_claude({"five_hour": raw})["windows"], [])

    def test_reset_without_timezone_is_unknown(self):
        for reset in (None, "2026-09-20T06:00:00", "invalid", True, -5, 10**30):
            with self.subTest(reset=reset):
                result = pu.normalize_claude({"five_hour": {"utilization": 1, "resets_at": reset}})
                self.assertIsNone(result["windows"][0]["resets_at"])


class FetchClaudeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def credentials(self, expires=None):
        auth = {"accessToken": "synthetic-test-secret"}
        if expires is not None:
            auth["expiresAt"] = expires
        (Path(self.directory.name) / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": auth}), encoding="utf-8")

    @patch.object(pu.request, "build_opener")
    def test_missing_login_does_not_request_or_invent(self, opener):
        result = pu._read_claude()
        opener.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["windows"], [])

    @patch.object(pu.request, "build_opener")
    def test_expired_credentials_are_not_refreshed(self, opener):
        self.credentials(expires=1)
        result = pu._read_claude()
        opener.assert_not_called()
        self.assertIn("expired", result["reason"])
        self.assertNotIn("synthetic-test-secret", json.dumps(result))

    @patch.object(pu.request, "build_opener")
    def test_read_only_endpoint_with_no_redirects_and_no_secret_in_result(self, opener):
        self.credentials(expires=(time.time() + 500) * 1000)
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b'{"five_hour":{"utilization":0,"resets_at":null}}'
        result = pu._read_claude()
        self.assertEqual(result["status"], "ok")
        req = opener.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(opener.call_args.args, (pu._NoRedirect,))
        self.assertNotIn("synthetic-test-secret", json.dumps(result))

    @patch.object(pu.request, "build_opener")
    def test_auth_error_is_sanitized_and_never_retried(self, opener):
        self.credentials()
        opener.return_value.open.side_effect = HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 401, "synthetic-test-secret", {}, io.BytesIO())
        result = pu._read_claude()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("synthetic-test-secret", json.dumps(result))
        self.assertEqual(opener.return_value.open.call_count, 1)


class FetchCodexTest(unittest.TestCase):
    def process(self, frames):
        process = Mock()
        process.stdout = io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
        process.poll.return_value = 0
        return process

    @patch.object(pu, "_codex_command", return_value="codex")
    @patch.object(pu.subprocess, "Popen")
    def test_only_initialization_and_read_quota_are_sent(self, popen, command):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"rateLimits": {"primary": {
                "usedPercent": 7, "windowDurationMins": 10080}}}},
        ])
        popen.return_value = process
        result = pu._read_codex()
        self.assertEqual(result["windows"][0]["used_percent"], 7)
        frames = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([frame["method"] for frame in frames],
                         ["initialize", "initialized", "account/rateLimits/read"])

    @patch.object(pu, "_codex_command", return_value="codex")
    @patch.object(pu.subprocess, "Popen")
    def test_rpc_failure_is_unavailable_and_sanitized(self, popen, command):
        popen.return_value = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "error": {"message": "sensitive-server-message"}},
        ])
        result = pu._read_codex()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("sensitive-server-message", json.dumps(result))

    @patch.object(pu, "_codex_command", return_value=None)
    def test_missing_cli_is_unavailable(self, command):
        self.assertEqual(pu._read_codex()["status"], "unavailable")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.cache_patch = patch.object(pu, "_cache", None)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        for name in ("_last_good", "_attempted", "_backoff_until"):
            patcher = patch.object(pu, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    def ok_claude(self):
        return pu.normalize_claude({"five_hour": {"utilization": 12, "resets_at": time.time() + 3600}})

    def ok(self):
        return pu.normalize_codex({"rateLimits": {"primary": {
            "usedPercent": 4, "windowDurationMins": 10080,
            "resetsAt": time.time() + 1000}}})

    def unavailable(self):
        return pu._provider("claude", pu.CLAUDE_SOURCE, reason="Unavailable.")

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_cached_reads_do_not_refetch_or_return_mutable_cache(self, codex, claude):
        codex.return_value, claude.return_value = self.ok(), self.unavailable()
        first = pu.get_provider_usage(force=True)
        first["providers"][0]["windows"][0]["used_percent"] = 99
        second = pu.get_provider_usage()
        self.assertEqual(second["providers"][0]["windows"][0]["used_percent"], 4)
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(claude.call_count, 1)

    def test_peek_marks_stale_without_network(self):
        result = self.ok()
        result["fetched_at"] = time.time() - 1000
        pu._cache = {"fetched_at": time.time() - 1000, "providers": [result]}
        with patch.object(pu, "_read_codex") as reader:
            self.assertEqual(pu.peek_provider_usage()["providers"][0]["status"], "stale")
            reader.assert_not_called()
        self.assertEqual(pu._cache["providers"][0]["status"], "ok")

    def test_passed_reset_is_stale_never_an_invented_new_window(self):
        result = self.ok()
        result["windows"][0]["resets_at"] = time.time() - 1
        pu._cache = {"fetched_at": time.time(), "providers": [result]}
        updated = pu.peek_provider_usage()["providers"][0]
        self.assertEqual(updated["status"], "stale")
        self.assertEqual(updated["windows"][0]["used_percent"], 4)

    def test_expired_session_does_not_hide_current_weekly_quota(self):
        now = time.time()
        for result in (
            pu.normalize_codex({"rateLimits": {
                "primary": {"usedPercent": 95, "windowDurationMins": 300, "resetsAt": now - 1},
                "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": now + 86400},
            }}, fetched_at=now),
            pu.normalize_claude({
                "five_hour": {"utilization": 95, "resets_at": now - 1},
                "seven_day": {"utilization": 12, "resets_at": now + 86400},
            }, fetched_at=now),
        ):
            with self.subTest(provider=result["id"]):
                pu._cache = {"fetched_at": now, "providers": [result]}
                updated = pu.peek_provider_usage()["providers"][0]
                self.assertEqual(updated["status"], "ok")
                self.assertEqual(updated["windows"][0]["status"], "expired")
                self.assertEqual(updated["windows"][1]["status"], "ok")
                self.assertEqual(updated["windows"][1]["used_percent"], 12)
                self.assertEqual(updated["windows"][0]["resets_at"], now - 1)
                self.assertNotIn("status", result["windows"][0])

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_force_during_active_fetch_waits_then_reads_fresh_account_data(self, codex, claude):
        started, release, contended = threading.Event(), threading.Event(), threading.Event()
        results, failures = {}, []

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()

            def acquire(self, *args, **kwargs):
                if self.lock.locked():
                    contended.set()
                return self.lock.acquire(*args, **kwargs)

            def release(self):
                self.lock.release()

        def quota_read():
            if codex.call_count == 1:
                started.set()
                release.wait(2)
                percent = 15
            else:
                percent = 42
            return pu.normalize_codex({"rateLimits": {"primary": {
                "usedPercent": percent, "windowDurationMins": 10080,
                "resetsAt": time.time() + 86400}}})

        def fetch(name):
            try:
                results[name] = pu.get_provider_usage(force=True)
            except Exception as exc:
                failures.append(exc)

        codex.side_effect = quota_read
        claude.return_value = self.unavailable()
        pu._cache = {"fetched_at": time.time(), "providers": [self.ok(), self.unavailable()]}
        first = threading.Thread(target=fetch, args=("before_login",))
        second = threading.Thread(target=fetch, args=("after_login",))
        with patch.object(pu, "_fetch_lock", ObservedLock()):
            first.start()
            try:
                self.assertTrue(started.wait(1))
                second.start()
                self.assertTrue(contended.wait(1))
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(codex.call_count, 2)
        self.assertEqual(claude.call_count, 2)
        self.assertEqual(results["before_login"]["providers"][0]["windows"][0]["used_percent"], 15)
        self.assertEqual(results["after_login"]["providers"][0]["windows"][0]["used_percent"], 42)
        self.assertEqual(pu.peek_provider_usage()["providers"][0]["windows"][0]["used_percent"], 42)

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_failed_request_holds_last_reading_without_leaking_exception_details(self, codex, claude):
        codex.return_value, claude.return_value = self.ok(), self.unavailable()
        first = pu.get_provider_usage(force=True)
        codex.side_effect = RuntimeError("sensitive-provider-details")
        result = pu.get_provider_usage(force=True)
        provider = result["providers"][0]
        # The reading that did return data stays on screen, labelled, instead
        # of the dashboard blanking and refilling on every failed poll.
        self.assertEqual(provider["status"], "ok")
        self.assertEqual(provider["windows"], first["providers"][0]["windows"])
        self.assertIn("warning", provider)
        self.assertNotIn("sensitive-provider-details", json.dumps(result))

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_held_reading_goes_stale_and_never_becomes_zero_usage(self, codex, claude):
        codex.return_value, claude.return_value = self.ok(), self.unavailable()
        pu.get_provider_usage(force=True)
        codex.return_value = pu._provider("codex", pu.CODEX_SOURCE, reason="Quota request failed.")
        with patch.object(pu, "STALE_SECONDS", -1):
            provider = pu.get_provider_usage(force=True)["providers"][0]
        self.assertEqual(provider["status"], "stale")
        self.assertTrue(all(w["used_percent"] for w in provider["windows"]))

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_rate_limited_claude_backs_off_instead_of_polling_every_minute(self, codex, claude):
        codex.return_value = self.ok()
        claude.return_value = pu._provider("claude", pu.CLAUDE_SOURCE, reason="limited", retry_after=300)
        pu.get_provider_usage(force=True)
        pu.get_provider_usage(force=True)
        self.assertEqual(claude.call_count, 1)
        self.assertIn("Retrying in", pu.peek_provider_usage()["providers"][1]["reason"])

    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_automatic_polling_is_spaced_but_a_requested_refresh_is_not(self, codex, claude):
        codex.return_value, claude.return_value = self.ok(), self.ok_claude()
        pu.get_provider_usage(force=True)
        pu.get_provider_usage(force=False)
        self.assertEqual(claude.call_count, 1)
        pu.get_provider_usage(force=True)
        self.assertEqual(claude.call_count, 2)

    @patch.object(pu, "FETCH_TIMEOUT", 0.03)
    @patch.object(pu, "_read_claude")
    @patch.object(pu, "_read_codex")
    def test_slow_provider_does_not_block_other_result(self, codex, claude):
        release = threading.Event()
        self.addCleanup(release.set)
        codex.return_value = self.ok()
        claude.side_effect = lambda: (release.wait(1), self.unavailable())[1]
        started = time.monotonic()
        result = pu.get_provider_usage(force=True)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result["providers"][0]["status"], "ok")
        self.assertEqual(result["providers"][1]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
