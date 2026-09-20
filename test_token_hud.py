"""HUD smoke tests (UI cases need a display / xvfb)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path


class DesignHelpersTest(unittest.TestCase):
    def test_bar_and_pct_color(self) -> None:
        from token_hud import NOW, OK, SOON, bar, pct_color

        self.assertEqual(bar(0, 10), "..........")
        self.assertEqual(bar(100, 10), "##########")
        self.assertEqual(pct_color(10), OK)
        self.assertEqual(pct_color(50), SOON)
        self.assertEqual(pct_color(75), NOW)

    def test_status_kind(self) -> None:
        from token_hud import status_kind

        self.assertEqual(status_kind(2000), "ok")
        self.assertEqual(status_kind(900), "soon")
        self.assertEqual(status_kind(120), "now")
        self.assertEqual(status_kind(0), "now")


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs DISPLAY (xvfb-run)")
class ResetsTabTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["TOKEN_HUD_DIR"] = self.tmp.name

    def test_resets_tab_test_buzz_and_mark(self) -> None:
        from token_hud import TokenHud

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        self.assertEqual(hud.root.title(), "Token HUD")
        tabs = [hud.nb.tab(i, "text").strip() for i in hud.nb.tabs()]
        self.assertEqual(tabs, ["This cycle", "This chat", "Resets"])
        hud.nb.select(hud.tab_resets)
        hud.root.update_idletasks()
        hud.root.update()
        claude = hud.reset_cards["claude"].cget("text")
        self.assertIn("SESSION", claude)
        self.assertIn("WEEKLY", claude)
        self.assertIn("PLACEHOLDER", claude)
        self.assertIn("ET", claude)
        self.assertEqual(hud.reset_cards["claude"].pill.cget("text"), "OK")
        hud._test_buzz()
        hud.root.update()
        self.assertTrue(hud.banner.winfo_ismapped())
        self.assertIn("Test buzz", hud.banner_label.cget("text"))
        hud._mark_session("claude")
        hud.root.update()
        claude = hud.reset_cards["claude"].cget("text")
        self.assertIn("anchored", claude)
        self.assertTrue(Path(os.environ["TOKEN_HUD_DIR"], "reset-schedule.json").exists())
        from reset_schedule import load_config

        cfg = load_config()
        self.assertTrue(cfg["providers"]["claude"]["session_anchor"])

    def test_all_tabs_and_meters(self) -> None:
        from token_hud import ThinMeter, TokenHud, WIN_MIN_H, WIN_MIN_W

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        self.assertGreaterEqual(hud.root.minsize()[0], WIN_MIN_W)
        self.assertGreaterEqual(hud.root.minsize()[1], WIN_MIN_H)
        self.assertIsInstance(hud.meter_api, ThinMeter)
        self.assertIsInstance(hud.meter_chat, ThinMeter)
        hud.nb.select(hud.tab_cycle)
        hud.root.update()
        self.assertTrue(hud.tab_cycle.winfo_ismapped())
        hud.nb.select(hud.tab_chat)
        hud.root.update()
        self.assertTrue(hud.tab_chat.winfo_ismapped())
        hud.nb.select(hud.tab_resets)
        hud.root.update()
        self.assertTrue(hud.tab_resets.winfo_ismapped())

    def test_apply_billing_paints_header_and_spend(self) -> None:
        from token_hud import TokenHud

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        hud.report = {
            "plan": "Ultra",
            "elapsed_days": 12.4,
            "total_pct": 41,
            "api_pct": 75,
            "auto_pct": 28,
            "api_msg": "Other Models is the 75% bar.",
            "fetched_at": time.time(),
            "events_fetched": 12,
            "total_spend_cents": 18422,
            "included_cents": 10000,
            "bonus_cents": 8422,
            "on_demand_enabled": False,
            "headless": {"cents": 9010, "n": 8},
            "interactive": {"cents": 9412, "n": 176},
            "agg_input": 2100000,
            "agg_output": 140000,
            "agg_cache_read": 6400000,
            "agg_cache_write": 210000,
            "conversations": [
                {"name": "Demo chat", "cents": 1200, "other_cents": 800, "n": 4, "headless": 0},
            ],
            "models": [{"model": "grok-4.6", "cents": 400, "tier": 2, "input": 1000, "output": 80}],
            "days": [{"date": "2026-09-20", "cents": 500, "n": 3}],
            "findings": ["Other Models is the scarce pool."],
        }
        hud.apply_billing()
        hud.root.update_idletasks()
        self.assertEqual(hud.header_plan.cget("text"), "Ultra")
        self.assertIn("Overall 41%", hud.header_status.cget("text"))
        self.assertEqual(hud.header_api_pct.cget("text"), "75%")
        self.assertEqual(hud.spend_big.cget("text"), "$184.22")
        self.assertEqual(len(hud.chats_tree.get_children()), 1)
        self.assertIn("scarce pool", hud.why.get("1.0", "end"))

    def test_paint_chat_meter(self) -> None:
        from token_hud import TokenHud

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        hud._paint_chat(
            {
                "id": "abc",
                "model": "grok-4.6",
                "pct": 80,
                "used": 204140,
                "limit": 256000,
                "tax_pct": 31,
                "num_sub": 0,
                "breakdown": {
                    "categories": [
                        {"label": "System prompt", "estimatedTokens": 1067},
                        {"id": "conversation", "estimatedTokens": 171664},
                    ]
                },
            }
        )
        hud.root.update_idletasks()
        self.assertEqual(hud.chat_model.cget("text"), "grok-4.6")
        self.assertEqual(hud.chat_pct.cget("text"), "80%")
        self.assertIn("204,140", hud.chat_input.cget("text"))
        self.assertIn("System prompt", hud.body.cget("text"))
        self.assertAlmostEqual(hud.meter_chat._pct, 80.0)


if __name__ == "__main__":
    unittest.main()
