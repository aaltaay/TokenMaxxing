"""HUD smoke tests. The HTML shell does not need a display."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen


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


class OpenDesignAppleTokensTest(unittest.TestCase):
    def test_required_hex_and_type(self) -> None:
        import opendesign_apple as apple

        self.assertEqual(apple.BG, "#ffffff")
        self.assertEqual(apple.SURFACE, "#f5f5f7")
        self.assertEqual(apple.SURFACE_WARM, "#fbfbfd")
        self.assertEqual(apple.FG, "#1d1d1f")
        self.assertEqual(apple.FG_2, "#424245")
        self.assertEqual(apple.MUTED, "#6e6e73")
        self.assertEqual(apple.META, "#86868b")
        self.assertEqual(apple.BORDER, "#d2d2d7")
        self.assertEqual(apple.BORDER_SOFT, "#e8e8ed")
        self.assertEqual(apple.ACCENT, "#0071e3")
        self.assertEqual(apple.ACCENT_ON, "#ffffff")
        self.assertEqual(apple.ACCENT_HOVER, "#0077ed")
        self.assertEqual(apple.TEXT_XS, "12px")
        self.assertEqual(apple.TEXT_SM, "14px")
        self.assertEqual(apple.TEXT_BASE, "17px")
        self.assertEqual(apple.TEXT_LG, "21px")
        self.assertEqual(apple.TEXT_XL, "28px")
        self.assertEqual(apple.RADIUS_SM, "8px")
        self.assertEqual(apple.RADIUS_MD, "12px")
        self.assertEqual(apple.RADIUS_LG, "18px")
        self.assertEqual(apple.RADIUS_PILL, "980px")
        self.assertEqual(apple.ELEV_RAISED, "0 12px 32px rgba(0, 0, 0, 0.08)")
        self.assertIn("Segoe UI Variable Display", apple.FONT_DISPLAY)
        self.assertIn("Segoe UI Semibold", apple.FONT_DISPLAY)
        self.assertIn("Segoe UI Variable Text", apple.FONT_BODY)
        self.assertIn("Cascadia Mono", apple.FONT_MONO)
        self.assertEqual(apple.WEIGHT_BODY, "400")
        self.assertEqual(apple.WEIGHT_EMPHASIS, "600")

    def test_css_root_emits_contract(self) -> None:
        from opendesign_apple import css_root

        css = css_root()
        self.assertIn(":root {", css)
        for token in (
            "--bg: #ffffff",
            "--surface: #f5f5f7",
            "--accent: #0071e3",
            "--radius-pill: 980px",
            "--text-base: 17px",
        ):
            self.assertIn(token, css)

    def test_shell_stays_on_tokens_not_dark_apple(self) -> None:
        root = Path(__file__).resolve().parent
        css = (root / "hud" / "apple.css").read_text(encoding="utf-8")
        html = (root / "hud" / "index.html").read_text(encoding="utf-8")
        py = (root / "token_hud.py").read_text(encoding="utf-8")
        self.assertIn("var(--accent)", css)
        self.assertIn("var(--radius-pill)", css)
        self.assertIn("var(--elev-raised)", css)
        self.assertIn("This cycle", html)
        self.assertIn("This chat", html)
        self.assertIn("Resets", html)
        self.assertIn("Test buzz", html)
        for blob in (css, html, py):
            self.assertNotIn("#1c1c1e", blob)
            self.assertNotIn("#0A84FF", blob)
            self.assertNotIn("#2c2c2e", blob)


class HudCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["TOKEN_HUD_DIR"] = self.tmp.name


class ResetsTabTest(HudCase):
    def test_resets_tab_test_buzz_and_mark(self) -> None:
        from token_hud import TokenHud

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        self.assertEqual(hud.root.title(), "Token HUD")
        tabs = [hud.nb.tab(i, "text").strip() for i in hud.nb.tabs()]
        self.assertEqual(tabs, ["This cycle", "This chat", "Resets"])
        hud.nb.select(hud.tab_resets)
        claude = hud.reset_cards["claude"].cget("text")
        self.assertIn("SESSION", claude)
        self.assertIn("WEEKLY", claude)
        self.assertIn("PLACEHOLDER", claude)
        self.assertIn("ET", claude)
        self.assertEqual(hud.reset_cards["claude"].pill.cget("text"), "OK")
        hud._test_buzz()
        self.assertTrue(hud.banner.winfo_ismapped())
        self.assertIn("Test buzz", hud.banner_label.cget("text"))
        hud._mark_session("claude")
        claude = hud.reset_cards["claude"].cget("text")
        self.assertIn("anchored", claude)
        self.assertTrue(Path(os.environ["TOKEN_HUD_DIR"], "reset-schedule.json").exists())
        from reset_schedule import load_config

        cfg = load_config()
        self.assertTrue(cfg["providers"]["claude"]["session_anchor"])


class LayoutAndBillingTest(HudCase):
    def test_all_tabs_and_meters(self) -> None:
        from token_hud import ThinMeter, TokenHud, WIN_MIN_H, WIN_MIN_W

        hud = TokenHud()
        self.addCleanup(hud.root.destroy)
        self.assertGreaterEqual(hud.root.minsize()[0], WIN_MIN_W)
        self.assertGreaterEqual(hud.root.minsize()[1], WIN_MIN_H)
        self.assertIsInstance(hud.meter_api, ThinMeter)
        self.assertIsInstance(hud.meter_chat, ThinMeter)
        hud.nb.select(hud.tab_cycle)
        self.assertTrue(hud.tab_cycle.winfo_ismapped())
        hud.nb.select(hud.tab_chat)
        self.assertTrue(hud.tab_chat.winfo_ismapped())
        self.assertFalse(hud.tab_cycle.winfo_ismapped())
        hud.nb.select(hud.tab_resets)
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
        self.assertEqual(hud.header_plan.cget("text"), "Ultra")
        self.assertIn("Overall 41%", hud.header_status.cget("text"))
        self.assertEqual(hud.header_api_pct.cget("text"), "75%")
        self.assertEqual(hud.spend_big.cget("text"), "$184.22")
        self.assertIn("$100.00", hud.kv_included.cget("text"))
        self.assertEqual(len(hud.chats_tree.get_children()), 1)
        self.assertIn("scarce pool", hud.why.get("1.0", "end"))
        snap = hud.state()
        self.assertEqual(snap["header"]["plan"], "Ultra")
        self.assertEqual(snap["cycle"]["spend"], "$184.22")
        self.assertEqual(snap["cycle"]["findings"], ["Other Models is the scarce pool."])

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
        self.assertEqual(hud.chat_model.cget("text"), "grok-4.6")
        self.assertEqual(hud.chat_pct.cget("text"), "80%")
        self.assertIn("204,140", hud.chat_input.cget("text"))
        self.assertIn("System prompt", hud.body.cget("text"))
        self.assertAlmostEqual(hud.meter_chat._pct, 80.0)


class HttpHudTest(HudCase):
    def test_state_and_test_buzz_action(self) -> None:
        from token_hud import HudServer, TokenHud

        hud = TokenHud(demo=True)
        self.addCleanup(hud.root.destroy)
        server = HudServer(("127.0.0.1", 0), hud)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"

        with urlopen(base + "/api/state") as resp:
            state = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(state["header"]["plan"], "Ultra")
        self.assertIn("SESSION", state["resets"]["claude"]["summary"])
        self.assertFalse(state["banner"]["visible"])

        with urlopen(base + "/tokens.css") as resp:
            css = resp.read().decode("utf-8")
        self.assertIn("--accent: #0071e3", css)

        with urlopen(base + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("Token HUD", html)
        self.assertIn("Test buzz", html)

        req = Request(
            base + "/api/action",
            data=json.dumps({"op": "test_buzz"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            after = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(after["banner"]["visible"])
        self.assertEqual(after["banner"]["text"], "Test buzz")


if __name__ == "__main__":
    unittest.main()
