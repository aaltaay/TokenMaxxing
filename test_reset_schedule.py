"""Schedule math and config for Claude / Codex reset buzzers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import reset_schedule as rs


ET = rs.et_tz()
try:
    REFERENCE_ET = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    REFERENCE_ET = None


class HudDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"TOKEN_HUD_DIR": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_missing_config_writes_defaults(self) -> None:
        path = rs.config_path()
        self.assertFalse(path.exists())
        cfg = rs.load_config()
        self.assertTrue(path.exists())
        self.assertEqual(cfg["providers"]["claude"]["session_hours"], 5)
        self.assertTrue(cfg["providers"]["claude"]["weekly_placeholder"])
        self.assertEqual(cfg["prewarn_minutes"], 15)
        self.assertTrue(cfg["prewarn_enabled"])
        disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(disk["timezone"], "America/New_York")

    def test_partial_config_merges_defaults(self) -> None:
        rs.config_path().write_text('{"prewarn_minutes": 3, "providers": {"claude": {"weekly_time": "21:15"}}}', encoding="utf-8")
        cfg = rs.load_config()
        self.assertEqual(cfg["prewarn_minutes"], 3)
        self.assertEqual(cfg["providers"]["claude"]["weekly_time"], "21:15")
        self.assertEqual(cfg["providers"]["claude"]["session_hours"], 5)
        self.assertEqual(cfg["providers"]["codex"]["label"], "Codex / ChatGPT")
        self.assertTrue(cfg["buzz_enabled"])

    def test_corrupt_config_replaced(self) -> None:
        rs.config_path().write_text("{not json", encoding="utf-8")
        cfg = rs.load_config()
        self.assertEqual(cfg["version"], 1)
        self.assertTrue(rs.config_path().exists())
        self.assertTrue((Path(self.tmp.name) / "reset-schedule.bad.json").exists())

    def test_mark_session_writes_anchor(self) -> None:
        when = datetime(2026, 9, 20, 13, 7, tzinfo=ET)
        rs.mark_session_started("claude", when)
        cfg = rs.load_config()
        self.assertTrue(cfg["providers"]["claude"]["session_anchor"].startswith("2026-09-20T13:07:00"))


class MathTest(unittest.TestCase):
    def test_weekly_from_wednesday_to_thursday(self) -> None:
        now = datetime(2026, 9, 16, 12, 0, tzinfo=ET)  # Wednesday
        prev, nxt = rs.weekly_bounds(now, "thursday", "09:00")
        self.assertEqual(nxt, datetime(2026, 9, 17, 9, 0, tzinfo=ET))
        self.assertEqual(prev, datetime(2026, 9, 10, 9, 0, tzinfo=ET))

    def test_weekly_same_day_already_past(self) -> None:
        now = datetime(2026, 9, 17, 10, 0, tzinfo=ET)  # Thursday after 09:00
        prev, nxt = rs.weekly_bounds(now, "thu", "09:00")
        self.assertEqual(nxt, datetime(2026, 9, 24, 9, 0, tzinfo=ET))
        self.assertEqual(prev, datetime(2026, 9, 17, 9, 0, tzinfo=ET))

    def test_weekly_same_day_still_ahead(self) -> None:
        now = datetime(2026, 9, 17, 8, 0, tzinfo=ET)
        prev, nxt = rs.weekly_bounds(now, 3, "09:00")
        self.assertEqual(nxt, datetime(2026, 9, 17, 9, 0, tzinfo=ET))
        self.assertEqual(prev, datetime(2026, 9, 10, 9, 0, tzinfo=ET))

    def test_session_anchor_next_is_plus_hours(self) -> None:
        now = datetime(2026, 9, 20, 13, 7, tzinfo=ET)
        start, nxt, anchored = rs.session_bounds(now, 5, "2026-09-20T13:07:00-04:00")
        self.assertTrue(anchored)
        self.assertEqual(start, now)
        self.assertEqual(nxt, now + timedelta(hours=5))

    def test_session_anchor_rolls_after_window(self) -> None:
        now = datetime(2026, 9, 20, 19, 10, tzinfo=ET)
        start, nxt, anchored = rs.session_bounds(now, 5, "2026-09-20T13:07:00-04:00")
        self.assertTrue(anchored)
        self.assertEqual(start, datetime(2026, 9, 20, 18, 7, tzinfo=ET))
        self.assertEqual(nxt, datetime(2026, 9, 20, 23, 7, tzinfo=ET))

    def test_session_rolling_midnight(self) -> None:
        now = datetime(2026, 9, 20, 13, 0, tzinfo=ET)
        start, nxt, anchored = rs.session_bounds(now, 5, None)
        self.assertFalse(anchored)
        self.assertEqual(start, datetime(2026, 9, 20, 10, 0, tzinfo=ET))
        self.assertEqual(nxt, datetime(2026, 9, 20, 15, 0, tzinfo=ET))

    def test_weekday_aliases(self) -> None:
        self.assertEqual(rs.parse_weekday("Thursday"), 3)
        self.assertEqual(rs.parse_weekday("thu"), 3)
        self.assertEqual(rs.parse_weekday(3), 3)

    def test_fmt_countdown(self) -> None:
        self.assertEqual(rs.fmt_countdown(0), "now")
        self.assertEqual(rs.fmt_countdown(45), "45s")
        self.assertEqual(rs.fmt_countdown(125), "2m 05s")
        self.assertEqual(rs.fmt_countdown(5 * 3600 + 12 * 60), "5h 12m")
        self.assertEqual(rs.fmt_countdown(4 * 86400 + 2 * 3600), "4d 2h")

    @unittest.skipIf(REFERENCE_ET is None, "System timezone database is unavailable")
    def test_eastern_fallback_matches_zoneinfo(self) -> None:
        eastern = rs._Eastern()
        samples = [
            "2026-01-15T17:00:00+00:00",
            "2026-03-08T06:59:00+00:00",
            "2026-03-08T07:00:00+00:00",
            "2026-07-04T16:00:00+00:00",
            "2026-11-01T05:59:00+00:00",
            "2026-11-01T06:00:00+00:00",
        ]
        for raw in samples:
            utc = datetime.fromisoformat(raw)
            reference = utc.astimezone(REFERENCE_ET)
            fallback = utc.astimezone(eastern)
            self.assertEqual(reference.isoformat(), fallback.isoformat(), raw)
            self.assertEqual(reference.fold, fallback.fold, raw)

    def test_eastern_fallback_utc_boundaries_without_tzdata(self) -> None:
        eastern = rs._Eastern()
        samples = [
            ("2026-01-15T17:00:00+00:00", "2026-01-15T12:00:00-05:00", 0),
            ("2026-03-08T06:59:00+00:00", "2026-03-08T01:59:00-05:00", 0),
            ("2026-03-08T07:00:00+00:00", "2026-03-08T03:00:00-04:00", 0),
            ("2026-07-04T16:00:00+00:00", "2026-07-04T12:00:00-04:00", 0),
            ("2026-11-01T05:59:00+00:00", "2026-11-01T01:59:00-04:00", 0),
            ("2026-11-01T06:00:00+00:00", "2026-11-01T01:00:00-05:00", 1),
            ("2026-11-01T06:59:00+00:00", "2026-11-01T01:59:00-05:00", 1),
            ("2026-11-01T07:00:00+00:00", "2026-11-01T02:00:00-05:00", 0),
        ]
        for raw, expected, fold in samples:
            with self.subTest(utc=raw):
                utc = datetime.fromisoformat(raw)
                local = utc.astimezone(eastern)
                self.assertEqual(local.isoformat(), expected)
                self.assertEqual(local.fold, fold)
                self.assertEqual(local.timestamp(), utc.timestamp())

    def test_eastern_fallback_fold_offsets(self) -> None:
        eastern = rs._Eastern()
        for month, day, hour, offsets in ((3, 8, 2, (-5, -4)), (11, 1, 1, (-4, -5))):
            for fold, offset in enumerate(offsets):
                local = datetime(2026, month, day, hour, 30, tzinfo=eastern, fold=fold)
                self.assertEqual(local.utcoffset(), timedelta(hours=offset))

    def test_et_tz_works_when_zoneinfo_data_is_missing(self) -> None:
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assertIsInstance(rs.et_tz(), rs._Eastern)


class BuzzGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"TOKEN_HUD_DIR": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.cfg = rs.load_config()
        self.cfg["providers"]["claude"]["weekly_placeholder"] = False
        self.cfg["providers"]["codex"]["weekly_placeholder"] = False
        self.cfg["providers"]["claude"]["session_anchor"] = "2026-09-20T10:00:00-04:00"
        rs.save_config(self.cfg)

    def test_mark_now_does_not_due_reset(self) -> None:
        now = datetime(2026, 9, 20, 10, 0, 5, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        reset_ids = [e["id"] for e in due if e["phase"] == "reset" and e["kind"] == "session"]
        self.assertEqual(reset_ids, [])

    def test_session_reset_within_grace(self) -> None:
        now = datetime(2026, 9, 20, 15, 0, 8, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        resets = [e for e in due if e["phase"] == "reset" and e["provider"] == "claude" and e["kind"] == "session"]
        self.assertEqual(len(resets), 1)
        self.assertIn("claude:session:", resets[0]["id"])

    def test_session_reset_outside_grace_silent(self) -> None:
        now = datetime(2026, 9, 20, 16, 30, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        resets = [e for e in due if e["phase"] == "reset" and e["kind"] == "session"]
        self.assertEqual(resets, [])

    def test_prewarn_before_session(self) -> None:
        now = datetime(2026, 9, 20, 14, 58, 30, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        pre = [e for e in due if e["phase"] == "prewarn" and e["provider"] == "claude"]
        self.assertEqual(len(pre), 1)
        self.assertIn(":prewarn:", pre[0]["id"])

    def test_unanchored_session_never_buzzes(self) -> None:
        self.cfg["providers"]["claude"]["session_anchor"] = None
        now = datetime(2026, 9, 20, 15, 0, 5, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        sess = [e for e in due if e["kind"] == "session" and e["provider"] == "claude"]
        self.assertEqual(sess, [])

    def test_placeholder_weekly_never_buzzes(self) -> None:
        self.cfg["providers"]["claude"]["weekly_placeholder"] = True
        now = datetime(2026, 9, 17, 9, 0, 10, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        weekly = [e for e in due if e["kind"] == "weekly" and e["provider"] == "claude"]
        self.assertEqual(weekly, [])

    def test_weekly_reset_when_confirmed(self) -> None:
        now = datetime(2026, 9, 17, 9, 0, 10, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        weekly = [e for e in due if e["phase"] == "reset" and e["kind"] == "weekly" and e["provider"] == "claude"]
        self.assertEqual(len(weekly), 1)

    def test_consume_dedupes(self) -> None:
        now = datetime(2026, 9, 20, 15, 0, 8, tzinfo=ET)
        first = rs.consume_due_events(now, self.cfg)
        second = rs.consume_due_events(now, self.cfg)
        self.assertTrue(first)
        self.assertEqual(second, [])
        self.assertTrue(rs.buzzed_path().exists())

    def test_disabled_clock_silent(self) -> None:
        self.cfg["providers"]["claude"]["session_enabled"] = False
        now = datetime(2026, 9, 20, 15, 0, 8, tzinfo=ET)
        due = rs.due_events(now, self.cfg)
        sess = [e for e in due if e["kind"] == "session" and e["provider"] == "claude"]
        self.assertEqual(sess, [])


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"TOKEN_HUD_DIR": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_status_mentions_placeholder_and_path(self) -> None:
        text = rs.render_status(datetime(2026, 9, 20, 12, 0, tzinfo=ET))
        self.assertIn("PLACEHOLDER", text)
        self.assertIn("not Cursor billing-cycle", text)
        self.assertIn(str(rs.config_path()), text)

    def test_test_buzz_cli(self) -> None:
        with patch.object(rs, "play_buzz", return_value="mock") as buzz:
            code = rs.main(["--test-buzz"])
        buzz.assert_called_once_with()
        self.assertEqual(code, 0)

    def test_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            rs.mark_session_started("grok")


if __name__ == "__main__":
    unittest.main()
