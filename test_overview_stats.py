import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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


def turn(offset, msg_id, *tools):
    """An assistant turn whose tool calls carry ids, as real logs' do."""
    return {**assistant(offset, msg_id), 'message': {
        'id': msg_id, 'model': 'claude-opus-5', 'usage': USAGE,
        'content': [{'type': 'tool_use', 'id': tool_id, 'name': name} for tool_id, name in tools]}}


def restart():
    """What a fresh engine starts with: nothing in memory, the file on disk."""
    for thread in threading.enumerate():
        if thread.name == 'overview-save':
            thread.join()
    ov._facts.clear()
    ov._disk.update(loaded=False, dirty=False, unsaved=0.0, saved_at=0.0)


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
        env = patch.dict(os.environ, {'TOKEN_HUD_DIR': str(root / 'hud')})
        env.start()
        self.addCleanup(env.stop)
        restart()
        self.addCleanup(restart)

    def run_overview(self, since=BASE - 10, **kwargs):
        return ov.overview(since, now=kwargs.pop('now', BASE + 3600), **kwargs)

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
        with patch.object(ov, '_typed', side_effect=AssertionError('reread an old line')):
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
        with patch.object(ov, '_read', side_effect=AssertionError('reread')):
            self.run_overview()

    # ── the facts on disk ──

    def write_logs(self):
        """A Claude session with a subagent and a rewound turn, and a Codex
        session whose token counters only make sense read in order."""
        write(self.claude / 'p' / 'main.jsonl', [
            prompt(0, uuid='u1'), turn(60, 'a', ('t0', 'Bash'), ('t2', 'Edit')),
            prompt(200, uuid='u2'), turn(260, 'b', ('t1', 'Read')), turn(60, 'a', ('t0', 'Bash'), ('t2', 'Edit')),
        ])
        write(self.claude / 'p' / 'main' / 'subagents' / 'agent-1.jsonl', [assistant(100, 's1', ['Grep'])])
        write(self.codex / '2026' / 'rollout.jsonl', [
            {'type': 'turn_context', 'timestamp': stamp(0), 'payload': {'model': 'gpt-6-astra'}},
            codex_event(5, 'user_message'), codex_event(30, 'agent_message'),
            codex_tokens(30, (100, 20, 10), (100, 20, 10)),
            {'type': 'response_item', 'timestamp': stamp(45), 'payload': {'type': 'function_call', 'name': 'shell'}},
        ])

    def grow_logs(self):
        """What the logs gain later: ids, reply times and counters from before
        decide how the new lines count."""
        write(self.claude / 'p' / 'main.jsonl', [
            # Logged again by a rewind: counted already, before the restart.
            turn(60, 'a', ('t0', 'Bash'), ('t2', 'Edit')), turn(260, 'b', ('t1', 'Read')),
            # 40 s after the last reply: thinking time only the saved reply time knows.
            prompt(300, uuid='u3'), turn(330, 'c', ('t3', 'Write')),
        ], mode='a')
        write(self.codex / '2026' / 'rollout.jsonl', [
            # The same cumulative counters again, then the next request.
            codex_tokens(50, (100, 20, 10), (100, 20, 10)),
            codex_event(60, 'user_message'),
            codex_tokens(90, (250, 50, 30), (150, 30, 20)),
        ], mode='a')

    def full_read(self):
        """The overview a fresh engine gives with no saved facts at all."""
        restart()
        ov.cache_path().unlink(missing_ok=True)
        result = self.run_overview()
        restart()
        return result

    def test_saved_facts_come_back_exactly_as_they_were(self):
        self.write_logs()
        self.run_overview()
        self.assertEqual(len(ov._facts), 3)
        for facts in ov._facts.values():
            # Through JSON, as the file holds them: tuples stay tuples, ids stay ids.
            self.assertEqual(ov._revive(json.loads(json.dumps(ov._saveable(facts)))), facts)
        claude = ov._facts[('claude', str(self.claude / 'p' / 'main.jsonl'))]
        self.assertEqual(len(claude['seen']), 7)
        self.assertEqual(ov._facts[('codex', str(self.codex / '2026' / 'rollout.jsonl'))]['previous'], (100, 20, 10))
        before = {key: dict(facts) for key, facts in ov._facts.items()}
        ov._save()
        restart()
        with patch.object(ov, '_read', side_effect=AssertionError('reread a saved log')):
            self.run_overview()
        self.assertEqual(ov._facts, before)

    def test_totals_match_cold_warm_and_continued_from_a_saved_offset(self):
        self.write_logs()
        cold = self.run_overview()
        ov._save()
        restart()
        warm = self.run_overview()
        self.assertEqual(warm, cold)
        self.assertEqual(cold['providers']['codex']['tokens']['output'], 10)

        # A fresh engine picks up where the saved offsets end, and counts what
        # the logs gained exactly as reading them whole would.
        ov._save()
        restart()
        self.grow_logs()
        continued = self.run_overview()
        self.assertEqual(continued, self.full_read())
        # Three Claude prompts and two Codex ones; Bash, Edit, Read, Grep, Write and shell.
        self.assertEqual((continued['prompts'], continued['tool_calls']), (5, 6))
        self.assertEqual(continued['think_hours'], round((200 - 60 + 300 - 260 + 60 - 30) / 3600, 2))
        codex = continued['providers']['codex']
        self.assertEqual((codex['tokens']['output'], codex['unpriced_requests']), (30, 0))
        self.assertEqual(continued['providers']['claude']['tokens']['output'], 40)

    def test_a_corrupt_or_foreign_file_reads_every_log_afresh(self):
        self.write_logs()
        expected = self.full_read()
        self.run_overview()
        ov._disk['dirty'] = True
        ov._save()
        good = ov.cache_path().read_text(encoding='utf-8')
        saved = json.loads(good)
        broken = {
            'truncated': good[:len(good) // 2],
            'not json': 'garbage',
            'another layout': json.dumps({**saved, 'version': ov.CACHE_VERSION + 1}),
            'other readers': json.dumps({**saved, 'readers': 'made by other code'}),
            'not a list': json.dumps({**saved, 'logs': {'a': 1}}),
        }
        for label, text in broken.items():
            with self.subTest(label):
                restart()
                ov.cache_path().write_text(text, encoding='utf-8')
                with patch.object(ov, '_read', wraps=ov._read) as read:
                    self.assertEqual(self.run_overview(), expected)
                self.assertEqual(read.call_count, 3)

    def test_a_malformed_entry_rereads_only_its_own_log(self):
        self.write_logs()
        expected = self.full_read()
        self.run_overview()
        ov._disk['dirty'] = True
        ov._save()
        saved = json.loads(ov.cache_path().read_text(encoding='utf-8'))
        breakages = (
            lambda f: f['requests'][0].__setitem__(2, 'many'),
            lambda f: f.__setitem__('seen', 'not base64!'),
            lambda f: f.__setitem__('offset', -1),
            lambda f: f['tools'].append([1, None]),
            lambda f: f.pop('intervals'),
        )
        for index, breakage in enumerate(breakages):
            with self.subTest(index):
                entries = json.loads(json.dumps(saved))
                main = next(e for e in entries['logs'] if e[1].endswith('main.jsonl'))
                breakage(main[2])
                restart()
                ov.cache_path().write_text(json.dumps(entries), encoding='utf-8')
                with patch.object(ov, '_read', wraps=ov._read) as read:
                    self.assertEqual(self.run_overview(), expected)
                self.assertEqual([call.args[0].name for call in read.call_args_list], ['main.jsonl'])

    def test_logs_too_old_for_any_range_are_not_kept_on_disk(self):
        self.write_logs()
        old = self.claude / 'p' / 'old.jsonl'
        write(old, [assistant(-50 * 86400, 'z')])
        os.utime(old, (BASE - 50 * 86400, BASE - 50 * 86400))
        self.run_overview(since=BASE - 60 * 86400, now=BASE - 20 * 86400)
        self.assertIn(('claude', str(old)), ov._facts)
        self.run_overview(now=BASE + 3600)
        ov._save()
        logs = [path for _, path, _ in json.loads(ov.cache_path().read_text(encoding='utf-8'))['logs']]
        self.assertEqual(len(logs), 3)
        self.assertNotIn(str(old), logs)

    def test_the_readers_fingerprint_follows_prices_and_rules(self):
        same = ov._readers()
        self.assertEqual(ov._readers(), same)
        with patch.dict(ov.sc.CLAUDE_RATES, {'claude-opus-5': (1.0, 0.1, 1.25, 5.0)}):
            self.assertNotEqual(ov._readers(), same)
        with patch.object(ov, 'IDLE_GAP', ov.IDLE_GAP + 1):
            self.assertNotEqual(ov._readers(), same)
        with patch.object(ov, 'TOOL_GROUPS', ov.TOOL_GROUPS + (('Plan', ('exitplanmode',)),)):
            self.assertNotEqual(ov._readers(), same)
        self.assertEqual(ov._readers(), same)
        # Ranges and the live caches have no say in it.
        self.run_overview()
        with patch.object(ov, 'SITTING_GAP', 1):
            self.assertEqual(ov._readers(), same)

    def test_a_long_first_read_saves_as_it_goes_and_reports_progress(self):
        self.write_logs()
        seen = []
        real = ov.file_facts

        def file_facts(provider, path, stat=None):
            seen.append(ov.progress())
            ov._disk['unsaved'] = ov.CHECKPOINT_EVERY  # as if each log took that long
            return real(provider, path, stat)
        self.assertIsNone(ov.progress())
        with patch.object(ov, 'file_facts', file_facts), patch.object(ov, '_save', wraps=ov._save) as save:
            self.run_overview()
        total = sum(path.stat().st_size for path in self.claude.rglob('*.jsonl')) + \
            sum(path.stat().st_size for path in self.codex.rglob('*.jsonl'))
        self.assertEqual(seen[0], {'read': 0, 'total': total})
        self.assertGreater(seen[-1]['read'], 0)
        self.assertIsNone(ov.progress())
        self.assertGreaterEqual(save.call_count, 3)
        restart()
        with patch.object(ov, '_read', side_effect=AssertionError('reread a saved log')):
            self.run_overview()


if __name__ == '__main__':
    unittest.main()
