import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import overview_stats as ov

BASE = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc).timestamp()


def stamp(offset):
    return datetime.fromtimestamp(BASE + offset, timezone.utc).isoformat().replace('+00:00', 'Z')


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')


def assistant(offset, msg_id, tools=(), usage=None):
    return {'type': 'assistant', 'timestamp': stamp(offset), 'message': {
        'id': msg_id, 'model': 'claude-opus-5',
        'content': [{'type': 'tool_use', 'name': name} for name in tools],
        'usage': usage or {'input_tokens': 1, 'output_tokens': 10, 'cache_read_input_tokens': 100,
                           'cache_creation_input_tokens': 5}}}


def prompt(offset, text='do the thing'):
    return {'type': 'user', 'timestamp': stamp(offset), 'message': {'content': text}}


class OverviewStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.claude = root / 'claude'
        self.codex = root / 'codex'
        patcher = patch.object(ov.su, 'session_root', lambda p: self.claude if p == 'claude' else self.codex)
        patcher.start()
        self.addCleanup(patcher.stop)
        cost = patch.object(ov.sc, 'summarize_file', return_value={'cents': 250})
        cost.start()
        self.addCleanup(cost.stop)
        ov._facts.clear()

    def test_agent_time_human_time_and_peak_come_from_the_logs(self):
        write(self.claude / 'proj' / 'main.jsonl', [
            prompt(0), assistant(60, 'a', ['Bash', 'Edit']), assistant(120, 'b', ['Read']),
            prompt(300), assistant(600, 'c', ['mcp__Claude_Browser__navigate']),
            # Injected reminders are not the person typing.
            prompt(610, '<system-reminder>x</system-reminder>'),
            # The same request logged twice is counted once.
            assistant(600, 'c'),
        ])
        write(self.claude / 'proj' / 'main' / 'subagents' / 'agent-1.jsonl', [
            prompt(100, 'parent task'), assistant(100, 's1', ['Grep']), assistant(400, 's2'),
        ])
        write(self.codex / '2026' / 'sub.jsonl', [
            {'type': 'session_meta', 'timestamp': stamp(0), 'payload': {'source': {'subagent': 'review'}}},
            {'type': 'event_msg', 'timestamp': stamp(10), 'payload': {'type': 'user_message'}},
            {'type': 'response_item', 'timestamp': stamp(20), 'payload': {'type': 'function_call', 'name': 'exec_command'}},
        ])
        result = ov.overview(BASE - 10, now=BASE + 3600)

        claude = result['providers']['claude']
        self.assertEqual((claude['sessions'], claude['subagents']), (1, 1))
        self.assertEqual(claude['agent_hours'], round((610 + 300) / 3600, 2))
        self.assertEqual(claude['tokens']['output'], 30 + 20)
        self.assertEqual(claude['cents'], 500)
        self.assertEqual(result['providers']['codex']['subagents'], 1)
        # Only the two typed prompts in the main session count; one sitting.
        self.assertEqual(result['prompts'], 2)
        self.assertEqual(result['sittings'], 1)
        self.assertAlmostEqual(result['human_hours'], (300 + ov.SITTING_PAD) / 3600, places=2)
        # The Codex subagent ends at 20s, before the Claude subagent starts at 100s.
        self.assertEqual(result['peak']['agents'], 2)
        tools = dict(result['tools'])
        self.assertEqual(tools['Shell'], 2)
        self.assertEqual((tools['Edit'], tools['Read'], tools['Browser'], tools['Search']), (1, 1, 1, 1))

    def test_prompts_with_a_screenshot_attached_count_and_tool_results_do_not(self):
        write(self.claude / 'p' / 'a.jsonl', [
            prompt(0, [{'type': 'image', 'source': {}}, {'type': 'text', 'text': 'is this right?'}]),
            prompt(10, [{'type': 'image', 'source': {}}]),
            prompt(20, [{'type': 'tool_result', 'content': 'ok'}]),
            prompt(30, [{'type': 'text', 'text': '<system-reminder>x</system-reminder>'}]),
            assistant(40, 'x'),
        ])
        result = ov.overview(BASE - 10, now=BASE + 3600)
        self.assertEqual(result['prompts'], 2)

    def test_idle_gaps_are_not_agent_time_and_old_logs_are_left_out(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'x'), assistant(ov.IDLE_GAP + 100, 'y')])
        old = self.claude / 'p' / 'old.jsonl'
        write(old, [assistant(-90 * 86400, 'z')])
        os.utime(old, (BASE - 90 * 86400, BASE - 90 * 86400))
        result = ov.overview(BASE - 10, now=BASE + 3600)
        self.assertEqual(result['providers']['claude']['agent_hours'], 0)
        self.assertEqual(result['providers']['claude']['sessions'], 1)

    def test_cursor_foreground_requests_count_as_your_time_and_agent_time(self):
        write(self.claude / 'p' / 'a.jsonl', [prompt(0), assistant(30, 'x')])
        interactive = [BASE + 600 + 60 * i for i in range(10)]
        result = ov.overview(BASE - 10, now=BASE + 3600,
                             cursor_minutes={'interactive': interactive, 'headless': [BASE + 7200]})
        # The Claude prompt and ten minutes of Cursor requests, 10 min apart, are one sitting.
        self.assertEqual(result['sittings'], 1)
        self.assertEqual(result['human_hours'], round((1140 + ov.SITTING_PAD) / 3600, 2))
        # Ten foreground minutes plus one background minute.
        self.assertEqual(result['cursor_agent_hours'], round((600 + 60) / 3600, 2))
        self.assertEqual(result['prompts'], 1)

    def test_your_time_is_split_into_calendar_days(self):
        midnight = datetime(2026, 9, 20).timestamp()
        minutes = [midnight - 1800 + 60 * i for i in range(60)]  # 23:30 to 00:29
        result = ov.overview(midnight - 86400, now=midnight + 3600, cursor_minutes={'interactive': minutes})
        days = {d['date']: d['hours'] for d in result['desk_days']}
        self.assertEqual(days['2026-09-19'], 0.5)
        self.assertEqual(days['2026-09-20'], round((29 * 60 + ov.SITTING_PAD) / 3600, 2))
        self.assertAlmostEqual(sum(days.values()), result['human_hours'], places=1)

    def test_unchanged_logs_are_not_parsed_again(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'x'), assistant(60, 'y')])
        ov.overview(BASE - 10, now=BASE + 3600)
        with patch.object(ov, '_claude_facts', side_effect=AssertionError('reparsed')):
            ov.overview(BASE - 10, now=BASE + 3600)


if __name__ == '__main__':
    unittest.main()
