import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import ledger
import overview_stats as ov

BASE = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc).timestamp()
USAGE = {'input_tokens': 1, 'output_tokens': 10, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 5}


def stamp(offset):
    return datetime.fromtimestamp(BASE + offset, timezone.utc).isoformat().replace('+00:00', 'Z')


def write(path, records, mode='w'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding='utf-8') as handle:
        handle.write(''.join(json.dumps(r) + '\n' for r in records))


def assistant(offset, msg_id, tools=(), usage=None, model='claude-opus-5', **extra):
    return {'type': 'assistant', 'timestamp': stamp(offset), **extra, 'message': {
        'id': msg_id, 'model': model,
        'content': [{'type': 'tool_use', 'name': name} for name in tools],
        'usage': usage or USAGE}}


def prompt(offset, text='do the thing', **extra):
    return {'type': 'user', 'timestamp': stamp(offset), **extra, 'message': {'content': text}}


def codex_event(offset, kind, **payload):
    return {'type': 'event_msg', 'timestamp': stamp(offset), 'payload': {'type': kind, **payload}}


def codex_tokens(offset, total, last):
    fields = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    return codex_event(offset, 'token_count', info={'total_token_usage': dict(zip(fields, total)),
                                                    'last_token_usage': dict(zip(fields, last))})


class OverviewTestCase(unittest.TestCase):
    """Logs in a scratch folder, read into a scratch ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.claude = root / 'claude'
        self.codex = root / 'codex'
        patcher = patch.object(ov.su, 'session_root', lambda p: self.claude if p == 'claude' else self.codex)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'TOKEN_HUD_DIR': str(root / 'hud')})
        env.start()
        self.addCleanup(env.stop)
        # Closed before the folder goes: Windows will not delete an open database.
        self.addCleanup(ledger.close)

    def run_overview(self, since=BASE - 10, **kwargs):
        ledger.default().ingest(parallel=False)
        return ov.overview(since, now=kwargs.pop('now', BASE + 3600), **kwargs)


class OverviewStatsTest(OverviewTestCase):
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
        result = self.run_overview()

        claude = result['providers']['claude']
        self.assertEqual((claude['sessions'], claude['subagents']), (1, 1))
        self.assertEqual(claude['agent_hours'], round((610 + 300) / 3600, 2))
        self.assertEqual(claude['tokens']['output'], 30 + 20)
        # Every request is priced, the subagent's included.
        self.assertEqual(claude['cents'], round(5 * ov.sc.claude_request_cents('claude-opus-5', USAGE), 2))
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
            prompt(35, [{'type': 'text', 'text': '[Request interrupted by user]'}]),
            assistant(40, 'x'),
        ])
        result = self.run_overview()
        self.assertEqual(result['prompts'], 2)

    def test_only_prompts_the_log_says_you_sent_count(self):
        write(self.claude / 'p' / 'a.jsonl', [
            prompt(0, 'mine', origin={'kind': 'human'}),
            prompt(10, 'Monitor event fired', origin={'kind': 'task-notification'}),
            prompt(20, 'Background (from the session that started this one)', turnOrigin='sdk'),
            prompt(30, 'This session is being continued from a previous conversation', isCompactSummary=True, turnOrigin='human'),
            assistant(40, 'x'),
        ])
        self.assertEqual(self.run_overview()['prompts'], 1)

    def test_a_forked_sessions_copy_of_its_parent_is_counted_once(self):
        parent = [prompt(0, 'go', uuid='u1', sessionId='parent'), assistant(60, 'a', ['Bash'], sessionId='parent')]
        write(self.claude / 'p' / 'parent.jsonl', parent)
        write(self.claude / 'p' / 'fork.jsonl', parent + [prompt(4000, 'more', uuid='u2', sessionId='fork'),
                                                          assistant(4060, 'b', ['Read'], sessionId='fork')])
        write(self.claude / 'p' / 'fork' / 'subagents' / 'agent-1.jsonl', [assistant(30, 's', ['Grep'], sessionId='parent')])
        result = self.run_overview(now=BASE + 7200)
        claude = result['providers']['claude']
        self.assertEqual(result['prompts'], 2)
        self.assertEqual(claude['tokens']['output'], 20)
        self.assertEqual(result['tool_calls'], 2)
        # The copied subagent log holds nothing of the fork's own.
        self.assertEqual((claude['sessions'], claude['subagents']), (2, 0))

    def test_records_a_rewound_session_logs_again_count_once(self):
        first = [prompt(0, 'go', uuid='u1'),
                 {**assistant(60, 'a'), 'message': {'id': 'a', 'model': 'claude-opus-5', 'usage': USAGE,
                                                    'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash'}]}}]
        write(self.claude / 'p' / 'a.jsonl', first + [assistant(120, 'b')] + first)
        result = self.run_overview()
        self.assertEqual((result['prompts'], result['tool_calls']), (1, 1))
        self.assertEqual(result['providers']['claude']['tokens']['output'], 20)

    def test_only_what_happened_in_the_range_counts(self):
        write(self.claude / 'p' / 'a.jsonl', [
            prompt(-90000), assistant(-90000, 'old', ['Bash']),
            prompt(0), assistant(30, 'new', ['Read']),
        ])
        result = self.run_overview()
        claude = result['providers']['claude']
        self.assertEqual(claude['tokens']['output'], 10)
        self.assertEqual(dict(result['tools']), {'Read': 1})
        self.assertEqual(result['prompts'], 1)
        self.assertEqual(claude['cents'], round(ov.sc.claude_request_cents('claude-opus-5', USAGE), 2))

    def test_a_range_starting_inside_a_quarter_hour_counts_from_its_start(self):
        # Requests and tool calls are summed per quarter hour; a range that
        # starts mid-way through one still counts only what came after it.
        write(self.claude / 'p' / 'a.jsonl', [assistant(100, 'early', ['Bash']), assistant(500, 'late', ['Read'])])
        result = self.run_overview(since=BASE + 300)
        self.assertEqual(result['providers']['claude']['tokens']['output'], 10)
        self.assertEqual(dict(result['tools']), {'Read': 1})

    def test_current_models_are_priced_and_unknown_ones_are_flagged(self):
        write(self.claude / 'p' / 'a.jsonl', [
            assistant(0, 'a', model='claude-opus-5-5'), assistant(10, 'b', model='claude-sonnet-5-5'),
            assistant(20, 'c', model='claude-future-9'), assistant(30, 'd', model='<synthetic>'),
        ])
        claude = self.run_overview()['providers']['claude']
        expected = ov.sc.claude_request_cents('claude-opus-5-5', USAGE) + ov.sc.claude_request_cents('claude-sonnet-5-5', USAGE)
        self.assertEqual(claude['cents'], round(expected, 2))
        self.assertEqual(claude['unpriced_requests'], 1)

    def test_idle_gaps_are_not_agent_time_and_old_logs_are_left_out(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'x'), assistant(ov.IDLE_GAP + 100, 'y')])
        old = self.claude / 'p' / 'old.jsonl'
        write(old, [assistant(-90 * 86400, 'z')])
        os.utime(old, (BASE - 90 * 86400, BASE - 90 * 86400))
        result = self.run_overview()
        self.assertEqual(result['providers']['claude']['agent_hours'], 0)
        self.assertEqual(result['providers']['claude']['sessions'], 1)

    def test_cursor_chats_count_as_your_time_and_their_subagents_only_as_agent_time(self):
        write(self.claude / 'p' / 'a.jsonl', [prompt(0), assistant(30, 'x')])
        interactive = [BASE + 600 + 60 * i for i in range(10)]
        subagent = [BASE + 3000 + 60 * i for i in range(20)]
        result = self.run_overview(cursor_minutes={'interactive': interactive, 'headless': [BASE + 7200],
                                                   'subagent': subagent})
        # The Claude prompt and ten minutes of Cursor requests, 10 min apart, are one sitting.
        self.assertEqual(result['sittings'], 1)
        self.assertEqual(result['human_hours'], round((1140 + ov.SITTING_PAD) / 3600, 2))
        # Ten foreground minutes, twenty subagent minutes and one background minute.
        self.assertEqual(result['cursor_agent_hours'], round((600 + 1200 + 60) / 3600, 2))
        self.assertEqual(result['prompts'], 1)

    def test_your_time_is_split_into_calendar_days_and_todays_hours(self):
        midnight = datetime(2026, 9, 20).timestamp()
        minutes = [midnight - 1800 + 60 * i for i in range(60)]  # 23:30 to 00:29
        result = ov.overview(midnight - 86400, now=midnight + 3600, cursor_minutes={'interactive': minutes})
        days = {d['date']: d['hours'] for d in result['desk_days']}
        self.assertEqual(days['2026-09-19'], 0.5)
        self.assertEqual(days['2026-09-20'], round((29 * 60 + ov.SITTING_PAD) / 3600, 2))
        self.assertAlmostEqual(sum(days.values()), result['human_hours'], places=1)
        self.assertEqual([d['date'] for d in result['days']], ['2026-09-19', '2026-09-20'])
        hours = result['hours']
        self.assertEqual(len(hours), 24)
        self.assertAlmostEqual(hours[0]['you'], (29 * 60 + ov.SITTING_PAD) / 3600, places=2)
        self.assertAlmostEqual(hours[0]['cursor'], 30 / 60, places=2)
        self.assertEqual(result['first_seen'], minutes[0])

    def test_a_growing_log_is_read_from_where_it_left_off(self):
        path = self.claude / 'p' / 'a.jsonl'
        write(path, [prompt(0), assistant(30, 'a')])
        self.run_overview()
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(assistant(60, 'b')) + '\n' + json.dumps(assistant(90, 'c'))[:25])
        with patch.object(ledger, '_typed', side_effect=AssertionError('reread an old line')):
            result = self.run_overview()
        # The half-written last line waits until it is finished.
        self.assertEqual(result['providers']['claude']['tokens']['output'], 20)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(assistant(90, 'c'))[25:] + '\n')
        result = self.run_overview()
        self.assertEqual(result['providers']['claude']['tokens']['output'], 30)
        self.assertEqual(result['prompts'], 1)

    def test_unchanged_logs_are_not_read_again(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'x'), assistant(60, 'y')])
        self.run_overview()
        with patch.object(ledger, '_read', side_effect=AssertionError('reread')):
            self.run_overview()

    def test_codex_counters_continue_across_reads_and_restarts(self):
        log = self.codex / '2026' / 'rollout.jsonl'
        write(log, [
            {'type': 'turn_context', 'timestamp': stamp(0), 'payload': {'model': 'gpt-6-astra'}},
            codex_event(5, 'user_message'), codex_event(30, 'agent_message'),
            codex_tokens(30, (100, 20, 10), (100, 20, 10)),
        ])
        self.run_overview()
        ledger.close()
        write(log, [
            # The same cumulative counters again, then the next request.
            codex_tokens(50, (100, 20, 10), (100, 20, 10)),
            codex_event(60, 'user_message'),
            codex_tokens(90, (250, 50, 30), (150, 30, 20)),
        ], mode='a')
        codex = self.run_overview()['providers']['codex']
        # Priced as one request each: the saved counters and model say so.
        self.assertEqual((codex['tokens']['output'], codex['unpriced_requests']), (30, 0))
        self.assertEqual(codex['tokens']['input'], (100 - 20) + (150 - 30))


class OverviewPayloadTest(OverviewTestCase):
    """The keys the renderer reads, new ones included."""

    def test_each_day_carries_its_value_per_provider(self):
        midnight = datetime(2026, 9, 20).timestamp()
        write(self.claude / 'p' / 'a.jsonl', [
            {**assistant(0, 'a'), 'timestamp': datetime.fromtimestamp(midnight - 600, timezone.utc).isoformat()},
            {**assistant(0, 'b'), 'timestamp': datetime.fromtimestamp(midnight + 600, timezone.utc).isoformat()},
            {**assistant(0, 'c'), 'timestamp': datetime.fromtimestamp(midnight + 700, timezone.utc).isoformat()},
        ])
        result = self.run_overview(since=midnight - 86400, now=midnight + 3600)
        one = ov.sc.claude_request_cents('claude-opus-5', USAGE)
        days = {d['date']: d['cents'] for d in result['days']}
        self.assertEqual(days['2026-09-19'], {'cursor': None, 'claude': round(one, 2), 'codex': 0})
        self.assertEqual(days['2026-09-20']['claude'], round(2 * one, 2))
        self.assertAlmostEqual(sum(d['claude'] for d in days.values()), result['providers']['claude']['cents'], places=2)

    def test_no_cursor_account_means_no_cursor_block(self):
        result = self.run_overview()
        self.assertIsNone(result['cursor'])

    def test_the_backfill_and_the_database_are_described(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'x')])
        result = self.run_overview()
        self.assertTrue(result['backfill']['done'])
        self.assertEqual(result['backfill']['read_bytes'], result['backfill']['total_bytes'])
        self.assertEqual(result['backfill']['complete_from'], BASE)
        self.assertEqual(Path(result['db']['path']), ledger.db_path())
        self.assertGreater(result['db']['bytes'], 0)
        self.assertEqual(ov.progress()['read'], result['backfill']['read_bytes'])

    def test_focus_time_per_app_and_per_day(self):
        midnight = datetime(2026, 9, 20).timestamp()
        ledger.default().record_focus([
            {'app': 'claude', 'start': midnight - 1800, 'end': midnight + 1800},
            {'app': 'cursor', 'start': midnight + 3600, 'end': midnight + 4500},
            # Before the range: only its start counts toward when tracking began.
            {'app': 'codex', 'start': midnight - 3 * 86400, 'end': midnight - 3 * 86400 + 60},
        ], now=midnight + 7200)
        focus = self.run_overview(since=midnight - 86400, now=midnight + 7200)['focus']
        self.assertEqual(focus['hours'], round(4500 / 3600, 2))
        self.assertEqual(focus['apps'], [{'id': 'claude', 'label': 'Claude', 'hours': 1.0},
                                         {'id': 'cursor', 'label': 'Cursor', 'hours': 0.25}])
        self.assertEqual(focus['days'], [{'date': '2026-09-19', 'hours': 0.5, 'apps': {'claude': 0.5}},
                                         {'date': '2026-09-20', 'hours': 0.75, 'apps': {'claude': 0.5, 'cursor': 0.25}}])
        self.assertEqual(focus['tracked_since'], midnight - 3 * 86400)
        self.assertEqual(focus['idle_cutoff_s'], 300)


if __name__ == '__main__':
    unittest.main()
