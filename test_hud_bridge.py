"""Bridge tests: severity buckets, derived cycle numbers, and the NDJSON transport."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest

import hud_bridge as hb


class SeverityTest(unittest.TestCase):
    def test_status_kind_matches_the_old_hud_thresholds(self) -> None:
        self.assertEqual(hb.status_kind(2000), "ok")
        self.assertEqual(hb.status_kind(900), "soon")
        self.assertEqual(hb.status_kind(120), "now")
        self.assertEqual(hb.status_kind(0), "now")

    def test_pct_kind(self) -> None:
        self.assertEqual(hb.pct_kind(10), "ok")
        self.assertEqual(hb.pct_kind(50), "soon")
        self.assertEqual(hb.pct_kind(75), "now")
        self.assertEqual(hb.pct_kind(99.9), "now")
        # Reaching the ceiling is its own state, rendered as an achievement.
        self.assertEqual(hb.pct_kind(100), "maxxed")
        self.assertEqual(hb.pct_kind(140), "maxxed")


class DecorateCycleTest(unittest.TestCase):
    def report(self, **over) -> dict:
        base = {
            "api_pct": 40.0,
            "auto_pct": 20.0,
            "total_pct": 33.0,
            "elapsed_days": 10.0,
            "remain_days": 20.0,
            "total_spend_cents": 200000.0,
            "api_days_left": 15.0,
            "on_demand_enabled": False,
            "days": [],
        }
        base.update(over)
        return base

    def test_burn_rate_and_cycle_length(self) -> None:
        out = hb._decorate_cycle(self.report())
        self.assertAlmostEqual(out["burn_cents_per_day"], 20000.0)
        self.assertAlmostEqual(out["cycle_days"], 30.0)

    def test_burn_rate_is_suppressed_at_the_start_of_a_cycle(self) -> None:
        # Under a tenth of a day elapsed, a per-day rate is noise, not signal.
        out = hb._decorate_cycle(self.report(elapsed_days=0.05))
        self.assertEqual(out["burn_cents_per_day"], 0.0)

    def test_burn_delta_compares_two_weeks(self) -> None:
        days = [{"date": f"d{i}", "cents": 100.0} for i in range(7)]
        days += [{"date": f"d{i}", "cents": 200.0} for i in range(7, 14)]
        out = hb._decorate_cycle(self.report(days=days))
        self.assertAlmostEqual(out["burn_delta_pct"], 100.0)

    def test_burn_delta_is_none_without_a_prior_week(self) -> None:
        days = [{"date": f"d{i}", "cents": 100.0} for i in range(5)]
        self.assertIsNone(hb._decorate_cycle(self.report(days=days))["burn_delta_pct"])

    def test_meters_cover_each_pool_and_carry_severity(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=92.0, grok_bot_pct=10.0))
        ids = [m["id"] for m in out["meters"]]
        self.assertEqual(ids, ["cursor.other", "cursor.auto", "cursor.grokbot"])
        by_id = {m["id"]: m for m in out["meters"]}
        self.assertEqual(by_id["cursor.other"]["status"], "now")
        self.assertEqual(by_id["cursor.grokbot"]["status"], "ok")

    def test_a_full_pool_reports_maxxed_not_a_warning(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=100.0))
        by_id = {m["id"]: m for m in out["meters"]}
        self.assertEqual(by_id["cursor.other"]["status"], "maxxed")

    def test_grok_bot_meter_is_omitted_when_absent(self) -> None:
        out = hb._decorate_cycle(self.report())
        self.assertNotIn("cursor.grokbot", [m["id"] for m in out["meters"]])

    def test_meters_never_include_a_time_window(self) -> None:
        # Reset clocks measure elapsed time; mixing them into the consumption
        # dial is the confusion the README warns about.
        out = hb._decorate_cycle(self.report())
        self.assertTrue(all(m["kind"] == "pool" for m in out["meters"]))

    def test_quiet_cycle_needs_no_attention(self) -> None:
        self.assertEqual(hb._decorate_cycle(self.report())["attention"], [])

    def test_exhausted_pool_leads_and_is_celebrated(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=100.0, auto_pct=95.0))
        items = out["attention"]
        self.assertEqual(items[0]["id"], "other-pool-empty")
        self.assertEqual(items[0]["severity"], "maxxed")
        self.assertEqual(items[0]["tone"], "celebrate")
        # Celebratory styling must not cost the reader the actual consequence.
        self.assertIn("named models stop here", items[0]["detail"])
        self.assertEqual(items[1]["tone"], "warn")

    def test_every_item_carries_a_tone_the_renderer_can_style(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=100.0, auto_pct=100.0))
        self.assertTrue(out["attention"])
        for item in out["attention"]:
            self.assertIn(item["tone"], ("celebrate", "warn"))
            self.assertIn(item["provider"], ("cursor",))

    def test_on_demand_changes_what_an_empty_pool_means(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=100.0, on_demand_enabled=True))
        self.assertIn("overage", out["attention"][0]["detail"])
        self.assertEqual(out["attention"][0]["tone"], "celebrate")

    def test_short_runway_is_flagged(self) -> None:
        out = hb._decorate_cycle(self.report(api_pct=80.0, api_days_left=1.5))
        self.assertIn("runway-short", [i["id"] for i in out["attention"]])


class ChatCategoriesTest(unittest.TestCase):
    def test_overhead_is_listed_first_and_tagged(self) -> None:
        rows = hb._chat_categories(
            {"used": 1000, "cats": {"conversation": 600, "tools": 300, "rules": 100}}
        )
        # OVERHEAD_IDS fixes the order: system_prompt, tools, rules, ...
        self.assertEqual([r["id"] for r in rows], ["tools", "rules", "conversation"])
        self.assertTrue(rows[0]["overhead"])
        self.assertFalse(rows[-1]["overhead"])
        self.assertAlmostEqual(rows[-1]["share"], 60.0)

    def test_zero_sized_categories_are_dropped(self) -> None:
        rows = hb._chat_categories({"used": 100, "cats": {"tools": 0, "conversation": 100}})
        self.assertEqual([r["id"] for r in rows], ["conversation"])


class TransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.out = io.StringIO()
        self.bridge = hb.Bridge(
            out=self.out,
            commands={
                "echo": lambda **kw: kw,
                "boom": lambda: (_ for _ in ()).throw(RuntimeError("engine said no")),
            },
        )

    def frames(self) -> list[dict]:
        return [json.loads(l) for l in self.out.getvalue().splitlines() if l.strip()]

    def drain(self, line: str) -> list[dict]:
        self.bridge.handle_line(line)
        deadline = time.time() + 5
        while time.time() < deadline and not self.frames():
            time.sleep(0.01)
        return self.frames()

    def test_response_carries_the_request_id(self) -> None:
        frames = self.drain(json.dumps({"id": 7, "cmd": "echo", "args": {"a": 1}}))
        self.assertEqual(frames[0], {"id": 7, "ok": True, "data": {"a": 1}})

    def test_command_errors_are_reported_not_fatal(self) -> None:
        frames = self.drain(json.dumps({"id": 8, "cmd": "boom"}))
        self.assertFalse(frames[0]["ok"])
        self.assertEqual(frames[0]["error"]["kind"], "RuntimeError")
        self.assertEqual(frames[0]["error"]["message"], "engine said no")

    def test_unknown_command_is_rejected(self) -> None:
        frames = self.drain(json.dumps({"id": 9, "cmd": "nope"}))
        self.assertEqual(frames[0]["error"]["kind"], "unknown_command")

    def test_unparsable_frame_is_rejected(self) -> None:
        frames = self.drain("{not json")
        self.assertEqual(frames[0]["error"]["kind"], "bad_frame")

    def test_blank_lines_are_ignored(self) -> None:
        self.bridge.handle_line("   \n")
        self.assertEqual(self.frames(), [])


class ResetsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._prior = os.environ.get("TOKEN_HUD_DIR")
        os.environ["TOKEN_HUD_DIR"] = tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prior is None:
            os.environ.pop("TOKEN_HUD_DIR", None)
        else:
            os.environ["TOKEN_HUD_DIR"] = self._prior

    def test_every_clock_is_serialisable_and_bounded(self) -> None:
        data = hb.cmd_resets()
        self.assertTrue(data["rows"])
        json.dumps(data)  # no stray datetimes
        for row in data["rows"]:
            self.assertIn(row["kind"], ("session", "weekly"))
            self.assertGreaterEqual(row["elapsed_pct"], 0.0)
            self.assertLessEqual(row["elapsed_pct"], 100.0)
            self.assertIn(row["status"], ("ok", "soon", "now", "off"))
            self.assertIsInstance(row["countdown"], str)


if __name__ == "__main__":
    unittest.main()
