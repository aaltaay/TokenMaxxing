"""Bridge tests: severity buckets, derived cycle numbers, and the NDJSON transport."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

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
    def test_derived_average_uses_reported_spend_and_actual_elapsed_days(self):
        data = hb._decorate_cycle({"elapsed_days": 10, "remain_days": 20, "total_spend_cents": 200000})
        self.assertEqual(data["burn_cents_per_day"], 20000)
        self.assertEqual(data["cycle_days"], 30)

    def test_missing_values_are_not_zero(self):
        data = hb._decorate_cycle({})
        self.assertIsNone(data["burn_cents_per_day"])
        self.assertIsNone(data["cycle_days"])
        self.assertTrue(all(m["pct"] is None for m in data["meters"]))
        self.assertEqual(data["attention"], [])

    def test_observed_zero_is_preserved(self):
        data = hb._decorate_cycle({"elapsed_days": 1, "total_spend_cents": 0, "api_pct": 0})
        self.assertEqual(data["burn_cents_per_day"], 0)
        self.assertEqual(data["meters"][0]["pct"], 0)

    def test_short_cycle_does_not_claim_zero_burn(self):
        data = hb._decorate_cycle({"elapsed_days": 0.05, "total_spend_cents": 1000})
        self.assertIsNone(data["burn_cents_per_day"])

    def test_no_runway_or_sparse_week_comparison_is_presented(self):
        data = hb._decorate_cycle({"api_days_left": 1.5, "days": [{"date":"2026-09-20","cents":50}]})
        self.assertIsNone(data["api_days_left"])
        self.assertIsNone(data["burn_delta_pct"])
        self.assertEqual(data["spark"], [])

    def test_alert_reports_percentage_without_inventing_access_rules(self):
        data = hb._decorate_cycle({"api_pct":100,"auto_pct":95})
        text = " ".join(i["title"]+i["detail"] for i in data["attention"])
        self.assertIn("100% used", text)
        self.assertNotIn("stop", text)
        self.assertNotIn("overage", text)
        self.assertNotIn("disabled", text)

    def test_explicit_on_demand_status_can_be_reported(self):
        data=hb._decorate_cycle({"api_pct":100,"on_demand_enabled":False})
        self.assertIn("reported as disabled", data["attention"][0]["detail"])

    def test_invalid_numeric_values_do_not_enter_meters(self):
        data=hb._decorate_cycle({"api_pct":float("nan"),"auto_pct":True,"grok_bot_pct":float("inf")})
        self.assertTrue(all(m["pct"] is None for m in data["meters"]))


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

    def test_manual_defaults_never_create_provider_clocks_or_alerts(self):
        with patch.object(hb.pu, "peek_provider_usage", return_value=None):
            self.assertEqual(hb.cmd_resets()["rows"], [])
            self.assertEqual(hb.cmd_due()["events"], [])

    def snapshot(self, status="ok", age=0, remaining=60):
        return {"providers":[{"id":"codex","label":"Codex","status":status,
            "source":"provider", "fetched_at":time.time()-age,
            "windows":[{"id":"weekly", "label":"Weekly", "used_percent":3,
                        "window_minutes":10080, "resets_at":time.time()+remaining}]}]}

    def test_only_provider_timestamps_are_exposed(self):
        snapshot=self.snapshot()
        with patch.object(hb.pu,"peek_provider_usage",return_value=snapshot):
            rows=hb.cmd_resets()["rows"]
            self.assertEqual(rows[0]["resets_at"],snapshot["providers"][0]["windows"][0]["resets_at"])
            self.assertNotIn("elapsed_pct",rows[0])

    def test_stale_data_never_triggers_alerts(self):
        for snapshot in (self.snapshot(status="stale"),self.snapshot(age=200),self.snapshot(remaining=-1)):
            with patch.object(hb.pu,"peek_provider_usage",return_value=snapshot):
                self.assertEqual(hb.cmd_due()["events"],[])

    def test_verified_prewarning_is_deduplicated(self):
        snapshot=self.snapshot()
        with patch.object(hb.pu,"peek_provider_usage",return_value=snapshot):
            events=hb.cmd_due()["events"]
            self.assertEqual(len(events),1)
            self.assertIn("provider reports",events[0]["message"])
            self.assertEqual(hb.cmd_due()["events"],[])




if __name__ == "__main__":
    unittest.main()
