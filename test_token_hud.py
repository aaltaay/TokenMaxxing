"""HUD Resets tab smoke (needs a display / xvfb)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
