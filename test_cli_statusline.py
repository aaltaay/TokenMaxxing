import json
import tempfile
import unittest
from pathlib import Path

import cli_statusline as cs

PAYLOAD = {
    "session_id": "abc-123",
    "transcript_path": "C:/Users/me/.claude/projects/p/abc-123.jsonl",
    "cwd": "C:/work/secret-project",
    "model": {"id": "claude-opus", "display_name": "Claude Opus"},
    "context_window": {"total_input_tokens": 120000, "total_output_tokens": 4000,
                       "context_window_size": 200000, "used_percentage": 60.0},
    "cost": {"total_cost_usd": 2.5},
}


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_pointer_carries_identifiers_and_counters_only(self):
        record = cs.pointer(PAYLOAD, now=100)
        self.assertEqual(record["session_id"], "abc-123")
        self.assertEqual(record["at"], 100)
        self.assertEqual(record["cost_usd"], 2.5)
        self.assertEqual(record["context_window"]["context_window_size"], 200000)
        text = json.dumps(record)
        self.assertNotIn("secret-project", text)
        self.assertNotIn("transcript", text)

    def test_unknown_fields_stay_absent_rather_than_becoming_zero(self):
        record = cs.pointer({"session_id": "x", "context_window": {"used_percentage": None}}, now=1)
        self.assertNotIn("context_window", record)
        self.assertNotIn("cost_usd", record)
        self.assertIsNone(cs.pointer({"cwd": "C:/work"}))

    def test_pointer_is_replaced_atomically_and_leaves_no_partial_file(self):
        cs.write_pointer(cs.pointer(PAYLOAD, now=1), self.home)
        cs.write_pointer(cs.pointer({**PAYLOAD, "session_id": "def-456"}, now=2), self.home)
        written = json.loads((self.home / cs.POINTER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(written["session_id"], "def-456")
        self.assertEqual([p.name for p in self.home.iterdir()], [cs.POINTER_NAME])

    def test_status_line_reports_unknown_counters_without_inventing_them(self):
        self.assertEqual(cs.line(PAYLOAD), "Claude Opus  in 120000  out 4000  ctx 60%")
        self.assertEqual(cs.line({}), "?  in ?  out ?  ctx ?%")


if __name__ == "__main__":
    unittest.main()
