"""Stdlib tests for provider reset schedule math."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import reset_schedule as rs


def utc(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


class ScheduleMathTests(unittest.TestCase):
    def test_weekly_thursday_midnight_et_from_sunday_afternoon(self) -> None:
        # Sunday 2026-09-20 15:00 UTC. Next Thu 00:00 EDT = 2026-09-24 04:00 UTC.
        now = utc(2026, 9, 20, 15, 0)
        nxt = rs.next_weekly_fire("thursday", "00:00", now, lookback_s=0)
        self.assertEqual(nxt.astimezone(timezone.utc), utc(2026, 9, 24, 4, 0))
        self.assertIn("Thu", rs.fmt_et(nxt))
        self.assertIn("12:00 AM", rs.fmt_et(nxt))

    def test_weekly_lookback_keeps_just_passed_fire(self) -> None:
        fire = utc(2026, 9, 24, 4, 0)
        now = fire + timedelta(seconds=45)
        nxt = rs.next_weekly_fire("thursday", "00:00", now, lookback_s=120)
        self.assertEqual(nxt.astimezone(timezone.utc), fire)

    def test_weekly_lookback_skips_old_fire(self) -> None:
        fire = utc(2026, 9, 24, 4, 0)
        now = fire + timedelta(minutes=10)
        nxt = rs.next_weekly_fire("thursday", "00:00", now, lookback_s=120)
        self.assertEqual(nxt.astimezone(timezone.utc), utc(2026, 10, 1, 4, 0))

    def test_weekly_winter_est_offset(self) -> None:
        # Friday 2026-01-02. Next Thu 00:00 EST = 2026-01-08 05:00 UTC.
        now = utc(2026, 1, 2, 12, 0)
        nxt = rs.next_weekly_fire("thursday", "00:00", now, lookback_s=0)
        self.assertEqual(nxt.astimezone(timezone.utc), utc(2026, 1, 8, 5, 0))

    def test_session_rolling_from_anchor(self) -> None:
        anchor = datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=-4)))
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
        nxt = rs.next_session_fire(anchor, 5, now, lookback_s=0)
        self.assertEqual(nxt, anchor + timedelta(hours=5))
        later = datetime(2026, 9, 20, 16, 0, tzinfo=timezone(timedelta(hours=-4)))
        nxt2 = rs.next_session_fire(anchor, 5, later, lookback_s=0)
        self.assertEqual(nxt2, anchor + timedelta(hours=10))

    def test_session_manual_expires(self) -> None:
        anchor = utc(2026, 9, 20, 10, 0)
        now = utc(2026, 9, 20, 16, 0)
        self.assertIsNone(rs.next_session_fire(anchor, 5, now, lookback_s=0, mode="manual"))
        still = utc(2026, 9, 20, 14, 30)
        nxt = rs.next_session_fire(anchor, 5, still, lookback_s=0, mode="manual")
        self.assertEqual(nxt, utc(2026, 9, 20, 15, 0))

    def test_session_none_without_anchor(self) -> None:
        self.assertIsNone(rs.next_session_fire(None, 5, utc(2026, 9, 20, 12)))

    def test_parse_hhmm_ampm(self) -> None:
        self.assertEqual(rs.parse_hhmm("11:00"), (11, 0))
        self.assertEqual(rs.parse_hhmm("11:00 PM"), (23, 0))
        self.assertEqual(rs.parse_hhmm("12:30 AM"), (0, 30))

    def test_fmt_countdown(self) -> None:
        self.assertEqual(rs.fmt_countdown(None), "mark session")
        self.assertEqual(rs.fmt_countdown(45), "45s")
        self.assertEqual(rs.fmt_countdown(125), "2m 05s")
        self.assertEqual(rs.fmt_countdown(3725), "1h 2m 05s")
        self.assertEqual(rs.fmt_countdown(90000), "1d 1h 0m")


class ConfigAndBuzzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patcher = patch.object(rs, "hud_dir", lambda: self.dir)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tmp.cleanup()

    def test_ensure_config_creates_defaults(self) -> None:
        self.assertFalse(rs.config_path().exists())
        cfg = rs.ensure_config()
        self.assertTrue(rs.config_path().exists())
        disk = json.loads(rs.config_path().read_text(encoding="utf-8"))
        self.assertEqual(disk["providers"]["claude"]["session_hours"], 5)
        self.assertEqual(cfg["pre_warn_minutes"], 2)
        self.assertTrue(cfg["pre_warn_enabled"])

    def test_corrupt_config_falls_back(self) -> None:
        rs.config_path().parent.mkdir(parents=True, exist_ok=True)
        rs.config_path().write_text("{not-json", encoding="utf-8")
        cfg = rs.load_config()
        self.assertEqual(cfg["providers"]["codex"]["session_hours"], 5)

    def test_partial_config_merges(self) -> None:
        rs.ensure_config()
        rs.config_path().write_text(
            json.dumps({"providers": {"claude": {"weekly_time": "08:30"}}}),
            encoding="utf-8",
        )
        cfg = rs.load_config()
        self.assertEqual(cfg["providers"]["claude"]["weekly_time"], "08:30")
        self.assertEqual(cfg["providers"]["claude"]["session_hours"], 5)
        self.assertEqual(cfg["providers"]["codex"]["label"], "Codex/ChatGPT")

    def test_due_events_fire_once(self) -> None:
        cfg = rs.default_config()
        cfg["pre_warn_enabled"] = False
        cfg["providers"]["claude"]["weekly_enabled"] = False
        cfg["providers"]["codex"]["enabled"] = False
        cfg["providers"]["claude"]["session_anchor"] = "2026-09-20T10:00:00-04:00"
        fire = datetime(2026, 9, 20, 15, 0, tzinfo=timezone(timedelta(hours=-4)))
        state: dict = {"fired_ids": {}, "last_buzz": {}}
        first = rs.due_events(cfg, state, fire)
        sessions = [e for e in first if e.provider == "claude" and e.kind == "session"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].phase, "fire")
        rs.record_fired(state, sessions[0], fire)
        again = rs.due_events(cfg, state, fire + timedelta(seconds=20))
        self.assertEqual([e for e in again if e.provider == "claude" and e.kind == "session"], [])

    def test_prewarn_then_fire(self) -> None:
        cfg = rs.default_config()
        cfg["pre_warn_minutes"] = 2
        cfg["pre_warn_enabled"] = True
        cfg["providers"]["claude"]["weekly_enabled"] = False
        cfg["providers"]["codex"]["enabled"] = False
        cfg["providers"]["claude"]["session_anchor"] = "2026-09-20T10:00:00-04:00"
        fire = datetime(2026, 9, 20, 15, 0, tzinfo=timezone(timedelta(hours=-4)))
        state: dict = {"fired_ids": {}, "last_buzz": {}}
        pre_at = fire - timedelta(seconds=90)
        pre = [e for e in rs.due_events(cfg, state, pre_at) if e.phase == "prewarn"]
        self.assertEqual(len(pre), 1)
        rs.record_fired(state, pre[0], pre_at)
        still_pre = [e for e in rs.due_events(cfg, state, fire - timedelta(seconds=30)) if e.phase == "prewarn"]
        self.assertEqual(still_pre, [])
        fires = [e for e in rs.due_events(cfg, state, fire) if e.phase == "fire"]
        self.assertEqual(len(fires), 1)

    def test_disabled_meter_does_not_buzz(self) -> None:
        cfg = rs.default_config()
        cfg["providers"]["claude"]["session_enabled"] = False
        cfg["providers"]["claude"]["weekly_enabled"] = False
        cfg["providers"]["codex"]["enabled"] = False
        cfg["providers"]["claude"]["session_anchor"] = "2026-09-20T10:00:00-04:00"
        fire = datetime(2026, 9, 20, 15, 0, tzinfo=timezone(timedelta(hours=-4)))
        self.assertEqual(rs.due_events(cfg, {"fired_ids": {}, "last_buzz": {}}, fire), [])

    def test_mark_session_writes_anchor(self) -> None:
        cfg = rs.ensure_config()
        now = datetime(2026, 9, 20, 13, 45, tzinfo=timezone(timedelta(hours=-4)))
        rs.mark_session_now(cfg, "codex", now)
        disk = json.loads(rs.config_path().read_text(encoding="utf-8"))
        self.assertTrue(disk["providers"]["codex"]["session_anchor"].startswith("2026-09-20T13:45"))

    def test_meter_views_need_anchor(self) -> None:
        cfg = rs.default_config()
        views = rs.meter_views(cfg, {"last_buzz": {}}, utc(2026, 9, 20, 12))
        claude_session = next(v for v in views if v.provider == "claude" and v.kind == "session")
        self.assertTrue(claude_session.needs_anchor)
        self.assertIsNone(claude_session.next_at)
        claude_week = next(v for v in views if v.provider == "claude" and v.kind == "weekly")
        self.assertIsNotNone(claude_week.next_at)
        self.assertTrue(claude_week.weekly_placeholder)


if __name__ == "__main__":
    unittest.main()
