"""The ledger: what it stores, that storing it changes no figure, and that
history, once stored, never changes."""
import collections
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import cursor_usage as cu
import ledger
import overview_stats as ov
from test_overview_stats import BASE, USAGE, assistant, codex_event, codex_tokens, prompt, stamp, write

BODY = 'output line\n' * 400  # big enough to be skimmed


def compact(path, records, mode='w'):
    """Records written the way Claude Code writes them: no spaces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding='utf-8') as handle:
        handle.write(''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in records))


def tool_use(offset, msg_id, tool_id, name, session):
    return {'parentUuid': 'p', 'isSidechain': False, 'message': {
        'id': msg_id, 'model': 'claude-opus-5', 'role': 'assistant', 'usage': USAGE,
        'content': [{'type': 'text', 'text': BODY}, {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {}}]},
        'requestId': 'r-' + msg_id, 'type': 'assistant', 'uuid': 'u-' + msg_id, 'timestamp': stamp(offset),
        'userType': 'external', 'cwd': 'c', 'sessionId': session, 'version': '9'}


def tool_result(offset, uuid, session, with_result=True, **extra):
    record = {'parentUuid': 'p', 'isSidechain': False, 'promptId': 'q', 'type': 'user', **extra,
              'message': {'role': 'user', 'content': [
                  {'tool_use_id': 'toolu_1', 'type': 'tool_result', 'content': BODY, 'is_error': False}]},
              'uuid': uuid, 'timestamp': stamp(offset)}
    if with_result:
        record['toolUseResult'] = {'stdout': BODY, 'stderr': '', 'nested': {'type': 'text', 'timestamp': 'x'}}
    return {**record, 'sourceToolAssistantUUID': 'a', 'userType': 'external', 'cwd': 'c',
            'sessionId': session, 'version': '9'}


def attachment(offset, uuid, session):
    return {'parentUuid': 'p', 'isSidechain': False,
            'attachment': {'type': 'file', 'content': BODY, 'nested': [{'type': 'user', 'timestamp': stamp(-999)}]},
            'type': 'attachment', 'uuid': uuid, 'timestamp': stamp(offset), 'userType': 'external', 'cwd': 'c',
            'sessionId': session, 'version': '9'}


def figures(result):
    """An Overview without what describes the ledger file itself."""
    return {key: value for key, value in result.items() if key not in ('backfill', 'db')}


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.claude = self.root / 'claude'
        self.codex = self.root / 'codex'
        patcher = patch.object(ledger.su, 'session_root', lambda p: self.claude if p == 'claude' else self.codex)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hud = self.root / 'hud'
        env = patch.dict(os.environ, {'TOKEN_HUD_DIR': str(self.hud)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(ledger.close)

    def overview(self, since=BASE - 10, now=BASE + 3600, **kwargs):
        ledger.default().ingest(parallel=False)
        return ov.overview(since, now=now, **kwargs)

    def restart(self):
        """What a fresh engine starts with: nothing in memory, the file on disk."""
        ledger.close()
        return ledger.default()

    def fresh_overview(self, since=BASE - 10, now=BASE + 3600):
        """The overview a ledger that never saw the logs before gives."""
        hud = self.root / f'fresh-{time.monotonic_ns()}'
        led = ledger.Ledger(hud / ledger.DB_NAME)
        try:
            led.ingest(parallel=False)
            return ov.overview(since, now=now, led=led)
        finally:
            led.close()

    def counts(self):
        with ledger.default().reading() as con:
            return {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in ('logs', 'seen', 'requests', 'prompts', 'tools', 'busy', 'request_slots', 'tool_slots')}

    def write_logs(self):
        """Sessions over three days: a Claude session with big tool output, a
        subagent, a fork, a rewind, and two Codex sessions."""
        records = []
        for day in range(3):
            base = day * 86400 - 3600 * 5
            records += [prompt(base, uuid=f'p{day}', sessionId='main'),
                        tool_use(base + 30, f'm{day}', f't{day}', 'Bash', 'main'),
                        tool_result(base + 40, f'r{day}', 'main', with_result=day != 1),
                        attachment(base + 41, f'at{day}', 'main'),
                        tool_use(base + 950, f'n{day}', f'tn{day}', 'Edit', 'main'),
                        prompt(base + 1200, uuid=f'q{day}', sessionId='main'),
                        tool_use(base + 1260, f'o{day}', f'to{day}', 'Read', 'main')]
        records += records[:3]  # a rewind logs earlier records again
        compact(self.claude / 'p' / 'main.jsonl', records)
        compact(self.claude / 'p' / 'main' / 'subagents' / 'agent-1.jsonl',
                [tool_use(100 + 400 * i, f's{i}', f'ts{i}', 'Grep', 'main') for i in range(5)])
        compact(self.claude / 'p' / 'fork.jsonl', records[:4] + [
            prompt(7000, uuid='f1', sessionId='fork'), tool_use(7030, 'f2', 'tf2', 'Write', 'fork')])
        write(self.codex / '2026' / 'a.jsonl', [
            {'type': 'turn_context', 'timestamp': stamp(-86400), 'payload': {'model': 'gpt-6-astra'}},
            codex_event(-86400 + 5, 'user_message'), codex_event(-86400 + 30, 'agent_message'),
            codex_tokens(-86400 + 30, (100, 20, 10), (100, 20, 10)),
            {'type': 'response_item', 'timestamp': stamp(-86400 + 45), 'payload': {'type': 'function_call', 'name': 'shell'}},
            codex_tokens(-86400 + 60, (100, 20, 10), (100, 20, 10)),
            codex_tokens(-86400 + 90, (250, 50, 30), (150, 30, 20)),
        ])
        write(self.codex / '2026' / 'b.jsonl', [
            {'type': 'session_meta', 'timestamp': stamp(0), 'payload': {'source': {'subagent': 'review'}}},
            codex_event(10, 'user_message'), codex_tokens(20, (10, 0, 5), (10, 0, 5)),
        ])

    def grow_logs(self):
        compact(self.claude / 'p' / 'main.jsonl', [
            tool_use(30 - 3600 * 5, 'm0', 't0', 'Bash', 'main'),  # counted already
            prompt(2 * 86400, uuid='late', sessionId='main'),
            tool_use(2 * 86400 + 20, 'late-m', 'late-t', 'Bash', 'main')], mode='a')
        write(self.codex / '2026' / 'a.jsonl', [codex_tokens(-86400 + 200, (300, 50, 40), (50, 0, 10))], mode='a')


class DirectComputationTest(LedgerTestCase):
    """The ledger's sums against reading every log whole, as the Overview did."""

    def direct(self, since):
        out = {p: collections.Counter() for p in ('claude', 'codex')}
        tools, prompts = collections.Counter(), []
        for provider, root in (('claude', self.claude), ('codex', self.codex)):
            for path in root.rglob('*.jsonl'):
                facts = ledger.read_log(provider, path)
                for t, cents, a, b, c, d, _ in facts['requests']:
                    if t >= since:
                        out[provider].update({'cents': cents or 0, 'unpriced': cents is None, 'input': a,
                                              'output': b, 'cache_read': c, 'cache_write': d})
                tools.update(group for t, group in facts['tools'] if t >= since)
                prompts += [p for p in facts['prompts'] if p[0] >= since]
                seconds = sum(e - max(s, since) for s, e in facts['intervals'] if e >= since)
                out[provider]['seconds'] += seconds
        return out, tools, len(prompts)

    def test_every_range_matches_reading_the_logs_whole(self):
        self.write_logs()
        midnight = datetime.fromtimestamp(BASE).replace(hour=0, minute=0, second=0).timestamp()
        for since in (BASE - 10 * 86400, midnight - 86400, midnight, BASE - 3600 * 5 + 35, BASE + 123.4):
            with self.subTest(since=since):
                result = self.overview(since=since, now=BASE + 3 * 86400)
                expected, tools, prompts = self.direct(since)
                for provider, row in result['providers'].items():
                    want = expected[provider]
                    self.assertAlmostEqual(row['cents'], round(want['cents'], 2), places=6)
                    self.assertEqual(row['unpriced_requests'], want['unpriced'])
                    self.assertEqual(row['tokens'], {key: want[key] for key in
                                                     ('input', 'output', 'cache_read', 'cache_write') if key in want}
                                     or {})
                    self.assertEqual(row['agent_hours'], round(want['seconds'] / 3600, 2))
                self.assertEqual(dict(result['tools']), dict(tools))
                self.assertEqual(result['prompts'], prompts)
                day_cents = sum(d['cents']['claude'] for d in result['days'])
                self.assertAlmostEqual(day_cents, result['providers']['claude']['cents'], places=1)

    def test_skimmed_and_parsed_records_read_the_same(self):
        self.write_logs()
        for path in self.claude.rglob('*.jsonl'):
            skimmed = ledger.read_log('claude', path)
            with patch.object(ledger, 'SKIM_OVER', math.inf):
                parsed = ledger.read_log('claude', path)
            self.assertEqual(skimmed, parsed)

    def test_worker_processes_read_what_one_thread_reads(self):
        self.write_logs()
        expected = self.overview()
        ledger.close()
        for name in os.listdir(self.hud):
            os.unlink(self.hud / name)
        with patch.object(ledger, 'POOL_AFTER', 0), patch.object(ledger.Ledger, '_read_one',
                                                                   side_effect=AssertionError('read on the thread')):
            ledger.default().ingest(parallel=True)
        self.assertEqual(figures(ov.overview(BASE - 10, now=BASE + 3600)), figures(expected))

    def test_without_worker_processes_the_thread_reads_everything(self):
        self.write_logs()
        expected = self.fresh_overview()
        with patch.object(ledger, 'POOL_AFTER', 0), patch('sys.stderr'), \
                patch.object(ledger.concurrent.futures, 'ProcessPoolExecutor', side_effect=OSError('no processes')):
            ledger.default().ingest(parallel=True)
        self.assertEqual(figures(ov.overview(BASE - 10, now=BASE + 3600)), figures(expected))


class SkimTest(unittest.TestCase):
    def line(self, record):
        return json.dumps(record, separators=(',', ':')).encode() + b'\n'

    def test_tool_results_and_attachments_give_their_session_and_time(self):
        for record in (tool_result(5, 'u', 's'), tool_result(5, 'u', 's', with_result=False), attachment(5, 'u', 's')):
            self.assertEqual(ledger._skim(self.line(record)), {'sessionId': 's', 'timestamp': stamp(5)})

    def test_anything_that_might_count_is_parsed_whole(self):
        human = tool_result(5, 'u', 's', origin={'kind': 'human'})
        typed = {**prompt(5, BODY), 'sessionId': 's'}
        mixed = tool_result(5, 'u', 's')
        mixed['message']['content'].insert(0, {'type': 'text', 'text': 'look'})
        for record in (human, typed, mixed, tool_use(5, 'm', 't', 'Bash', 's')):
            self.assertIsNone(ledger._skim(self.line(record)))
        # Cut short, run together with another record, or not JSON at all.
        whole = self.line(attachment(5, 'u', 's'))
        for line in (whole[:-1], whole[:200] + whole, b'{"type":"x",' + b'z' * 3000 + b'\n'):
            self.assertIsNone(ledger._skim(line))

    def test_a_cut_inside_a_nested_value_is_never_taken_for_the_top(self):
        record = attachment(5, 'u', 's')
        # The last `,"type":"` sits inside a value after the record's own type.
        record['rendered'] = {'kind': 'block', 'type': 'x', 'sessionId': 'other', 'timestamp': stamp(99)}
        self.assertIsNone(ledger._skim(self.line(record)))


class HistoryTest(LedgerTestCase):
    def test_reading_again_changes_nothing(self):
        self.write_logs()
        first = self.overview()
        counts = self.counts()
        led = self.restart()
        with patch.object(ledger, 'read_log', side_effect=AssertionError('read an unchanged log')):
            led.ingest(parallel=False)
            self.assertEqual(figures(ov.overview(BASE - 10, now=BASE + 3600)), figures(first))
        self.assertEqual(self.counts(), counts)

    def test_a_grown_log_continues_from_where_it_stopped_as_a_whole_read_would(self):
        self.write_logs()
        self.overview()
        self.restart()
        self.grow_logs()
        offsets = []
        real = ledger.read_log

        def read_log(provider, path, offset=0, *rest):
            offsets.append(offset)
            return real(provider, path, offset, *rest)
        with patch.object(ledger, 'read_log', read_log):
            continued = self.overview(since=BASE - 2 * 86400, now=BASE + 3 * 86400)
        self.assertEqual(len(offsets), 2)
        self.assertTrue(all(offset > 0 for offset in offsets))
        self.assertEqual(figures(continued), figures(self.fresh_overview(since=BASE - 2 * 86400, now=BASE + 3 * 86400)))
        # The Codex counters a read left carry on: the grown request is priced.
        self.assertEqual(continued['providers']['codex']['tokens']['output'], 10 + 20 + 10 + 5)

    def test_a_truncated_or_replaced_log_is_read_again_from_the_start(self):
        self.write_logs()
        self.overview()
        compact(self.claude / 'p' / 'main.jsonl', [prompt(0, uuid='new', sessionId='main'),
                                                   tool_use(30, 'new-m', 'new-t', 'Write', 'main')])
        result = self.overview()
        self.assertEqual(figures(result), figures(self.fresh_overview()))
        with ledger.default().reading() as con:
            self.assertEqual(con.execute('SELECT sum(n) FROM request_slots').fetchone()[0],
                             con.execute('SELECT count(*) FROM requests').fetchone()[0])
            self.assertEqual(con.execute('SELECT sum(n) FROM tool_slots').fetchone()[0],
                             con.execute('SELECT count(*) FROM tools').fetchone()[0])

    def test_prices_are_fixed_when_a_request_is_first_read(self):
        log = self.claude / 'p' / 'a.jsonl'
        write(log, [assistant(0, 'a')])
        cheap = ledger.sc.claude_request_cents('claude-opus-5', USAGE)
        self.assertEqual(self.overview()['providers']['claude']['cents'], round(cheap, 2))
        with patch.dict(ledger.sc.CLAUDE_RATES, {'claude-opus-5': (100.0, 10.0, 125.0, 500.0)}):
            dear = ledger.sc.claude_request_cents('claude-opus-5', USAGE)
            self.restart()
            write(log, [assistant(60, 'b')], mode='a')
            # The first request keeps the price it was read at.
            self.assertEqual(self.overview()['providers']['claude']['cents'], round(cheap + dear, 2))
        # And going back to the old table reprices nothing either.
        self.restart()
        self.assertEqual(self.overview()['providers']['claude']['cents'], round(cheap + dear, 2))

    def test_a_codex_log_found_to_be_a_subagent_drops_the_prompts_it_stored(self):
        log = self.codex / '2026' / 'c.jsonl'
        write(log, [codex_event(10, 'user_message')])
        self.assertEqual(self.overview()['prompts'], 1)
        write(log, [{'type': 'session_meta', 'timestamp': stamp(20), 'payload': {'source': {'subagent': 'x'}}}], mode='a')
        result = self.overview()
        self.assertEqual(result['prompts'], 0)
        self.assertEqual(result['providers']['codex']['subagents'], 1)

    def test_the_overview_counts_what_a_log_in_use_gained_seconds_ago(self):
        log = self.claude / 'p' / 'a.jsonl'
        write(log, [assistant(0, 'a')])
        self.overview()
        write(log, [assistant(30, 'b')], mode='a')
        # No ingest: the Overview's own catch-up reads the log in use.
        result = ov.overview(BASE - 10, now=BASE + 3600)
        self.assertEqual(result['providers']['claude']['tokens']['output'], 20)

    def test_two_engines_on_one_ledger_store_each_stretch_once(self):
        # The installed app and a source run, say: each reads what the logs
        # gained, and the slower one finds it stored already.
        self.write_logs()
        first = ledger.default()
        first.ingest(parallel=False)
        second = ledger.Ledger(first.path)
        self.addCleanup(second.close)
        second.ingest(parallel=False)
        self.grow_logs()
        compact(self.claude / 'p' / 'new.jsonl', [tool_use(50, 'x', 'tx', 'Bash', 'new')])
        real = second._store

        def racing_store(*args):
            first.ingest(parallel=False)
            real(*args)
        with patch.object(second, '_store', racing_store):
            second.ingest(parallel=False)
        since, now = BASE - 2 * 86400, BASE + 3 * 86400
        self.assertEqual(figures(ov.overview(since, now=now, led=second)),
                         figures(self.fresh_overview(since=since, now=now)))

    def test_reads_during_an_ingest_see_whole_logs(self):
        for n in range(30):
            write(self.claude / 'p' / f's{n}.jsonl', [assistant(n, f'a{n}'), assistant(n + 1, f'b{n}')])
        led = ledger.default()
        real = led._store

        def slow_store(*args):
            time.sleep(0.005)
            return real(*args)
        seen, errors = [], []
        with patch.object(led, '_store', slow_store):
            worker = threading.Thread(target=led.ingest, kwargs={'parallel': False})
            worker.start()
            while worker.is_alive():
                try:
                    seen.append(ov.overview(BASE - 10, now=BASE + 3600, led=led)['providers']['claude']['tokens'])
                except Exception as exc:  # pragma: no cover - the failure being tested for
                    errors.append(exc)
            worker.join()
        self.assertEqual(errors, [])
        outputs = [tokens.get('output', 0) for tokens in seen]
        # Every log lands whole: two requests of ten output tokens each.
        self.assertTrue(all(value % 20 == 0 for value in outputs))
        self.assertEqual(outputs, sorted(outputs))
        self.assertEqual(ov.overview(BASE - 10, now=BASE + 3600, led=led)['providers']['claude']['tokens']['output'], 600)


class BackfillTest(LedgerTestCase):
    def test_logs_are_read_newest_first_and_say_from_when_all_is_complete(self):
        mtimes = {}
        for n in range(4):
            log = self.claude / 'p' / f's{n}.jsonl'
            write(log, [assistant(n * 1000, f'a{n}')])
            mtimes[n] = BASE + n * 1000 + 5
            os.utime(log, (mtimes[n], mtimes[n]))
        led = ledger.default()
        self.assertEqual(led.backfill(), {'done': False, 'read_bytes': 0, 'total_bytes': 0, 'complete_from': None})
        order = []
        real = led._store

        def store_two(provider, path, *rest):
            order.append(Path(path).stem)
            real(provider, path, *rest)
            if len(order) == 2:
                led._stop.set()
        with patch.object(led, '_store', store_two):
            led.ingest(parallel=False)
        self.assertEqual(order, ['s3', 's2'])
        state = led.backfill()
        self.assertFalse(state['done'])
        self.assertLess(state['read_bytes'], state['total_bytes'])
        # Everything after the newest log still unread is in.
        self.assertEqual(state['complete_from'], mtimes[1])
        led._stop.clear()
        led.ingest(parallel=False)
        state = led.backfill()
        self.assertTrue(state['done'])
        self.assertEqual(state['read_bytes'], state['total_bytes'])
        self.assertEqual(state['complete_from'], BASE)

    def test_a_ledger_read_before_counts_as_done_before_its_first_look(self):
        write(self.claude / 'p' / 'a.jsonl', [assistant(0, 'a')])
        ledger.default().ingest(parallel=False)
        self.assertTrue(self.restart().backfill()['done'])


class FocusTest(LedgerTestCase):
    NOW = BASE + 86400

    def record(self, *spans):
        return ledger.default().record_focus([{'app': a, 'start': s, 'end': e} for a, s, e in spans], now=self.NOW)

    def stored(self):
        with ledger.default().reading() as con:
            return con.execute('SELECT app, start, end FROM focus ORDER BY app, start').fetchall()

    def test_a_span_sent_again_or_grown_is_stored_once(self):
        self.assertEqual(self.record(('claude', BASE, BASE + 60)), 1)
        self.record(('claude', BASE, BASE + 60))
        self.record(('claude', BASE, BASE + 120))
        self.ledger_restart_and_record(('claude', BASE, BASE + 180))
        self.assertEqual(self.stored(), [('claude', BASE, BASE + 180)])

    def ledger_restart_and_record(self, span):
        self.restart()
        self.record(span)

    def test_spans_of_one_app_close_together_merge_and_others_do_not(self):
        self.record(('claude', BASE, BASE + 60), ('claude', BASE + 75, BASE + 100),  # 15 s apart: one
                    ('claude', BASE + 116, BASE + 200),  # 16 s after: its own
                    ('cursor', BASE + 50, BASE + 90))
        self.record(('claude', BASE + 90, BASE + 120))  # bridges the two
        self.assertEqual(self.stored(), [('claude', BASE, BASE + 200), ('cursor', BASE + 50, BASE + 90)])

    def test_bad_spans_are_dropped(self):
        bad = [{'app': 'notepad', 'start': BASE, 'end': BASE + 1},
               {'app': 'claude', 'start': BASE + 10, 'end': BASE + 10},
               {'app': 'claude', 'start': BASE + 10, 'end': BASE},
               {'app': 'claude', 'start': float('nan'), 'end': BASE},
               {'app': 'claude', 'start': BASE, 'end': float('inf')},
               {'app': 'claude', 'start': True, 'end': BASE},
               {'app': 'claude', 'start': '1', 'end': BASE},
               {'app': 'claude', 'start': self.NOW, 'end': self.NOW + 61},
               {'app': 'claude', 'start': BASE, 'end': BASE + 6 * 3600 + 1},
               {'app': ['claude'], 'start': BASE, 'end': BASE + 1},
               'claude', None]
        good = {'app': 'codex', 'start': self.NOW - 10, 'end': self.NOW + 60}
        self.assertEqual(ledger.default().record_focus(bad + [good], now=self.NOW), 1)
        self.assertEqual(ledger.default().record_focus('not a list', now=self.NOW), 0)
        self.assertEqual(self.stored(), [('codex', self.NOW - 10, self.NOW + 60)])


class CursorTest(LedgerTestCase):
    ACCOUNT = 'test@example.com'

    def event(self, at, cents=1.5, **extra):
        return {'timestamp': str(int(at * 1000)), 'model': 'm', 'isHeadless': False,
                'tokenUsage': {'inputTokens': 10, 'outputTokens': 2, 'cacheReadTokens': 3, 'cacheWriteTokens': 4,
                               'totalCents': cents}, **extra}

    def remember(self, events, covered=None):
        return cu.remember_activity(self.ACCOUNT, events, covered)

    def cursor(self, since=BASE - 3600, now=BASE + 3600):
        return self.overview(since=since, now=now, cursor_account=self.ACCOUNT)

    def test_an_event_fetched_again_is_counted_once(self):
        events = [self.event(BASE), self.event(BASE + 60, conversationId='sand-subagent-1')]
        self.remember(events)
        self.remember(events + [self.event(BASE + 120)])
        # A later fetch that reprices or recounts an event changes nothing.
        self.remember([self.event(BASE, cents=99)])
        block = self.cursor()['cursor']
        self.assertEqual((block['requests'], block['cents']), (3, 4.5))
        self.assertEqual(block['tokens'], {'input': 30, 'output': 6, 'cache_read': 9, 'cache_write': 12})
        with ledger.default().reading() as con:
            kinds = sorted(kind for kind, in con.execute('SELECT kind FROM cursor_events'))
        self.assertEqual(kinds, ['interactive', 'interactive', 'subagent'])

    def test_complete_only_where_fetches_saw_every_event(self):
        self.remember([self.event(BASE)], covered=(BASE - 600, BASE + 1800))
        self.assertTrue(self.cursor(since=BASE - 600, now=BASE + 3600)['cursor']['complete'])
        # Before the covered stretch, or long after the last fetch: not known.
        self.assertFalse(self.cursor(since=BASE - 601, now=BASE + 3600)['cursor']['complete'])
        self.assertFalse(self.cursor(since=BASE - 600, now=BASE + 1800 + ov.CURSOR_FRESH + 1)['cursor']['complete'])
        # Fetches that overlap join up.
        self.remember([], covered=(BASE - 7200, BASE - 300))
        self.assertTrue(self.cursor(since=BASE - 7200, now=BASE + 3600)['cursor']['complete'])

    def test_each_days_value_is_known_only_for_covered_days(self):
        midnight = datetime(2026, 9, 20).timestamp()
        self.remember([self.event(midnight - 3600), self.event(midnight + 600, cents=2)],
                      covered=(midnight - 1800, midnight + 3600))
        result = self.cursor(since=midnight - 86400, now=midnight + 3600)
        days = {d['date']: d['cents']['cursor'] for d in result['days']}
        self.assertEqual(days, {'2026-09-19': None, '2026-09-20': 2})
        self.assertFalse(result['cursor']['complete'])
        self.assertEqual(result['cursor']['cents'], 3.5)

    def test_an_unknown_value_is_not_zero(self):
        self.remember([self.event(BASE), self.event(BASE + 1, cents=None)], covered=(BASE - 3600, BASE + 3600))
        block = self.cursor()['cursor']
        self.assertIsNone(block['cents'])
        self.assertEqual(block['requests'], 2)

    def test_another_accounts_events_and_minutes_never_count(self):
        cu.remember_activity('previous@example.com', [self.event(BASE)], (BASE - 3600, BASE + 3600))
        self.assertIsNone(self.cursor()['cursor'])
        self.assertIsNone(self.cursor()['cursor_agent_hours'])

    def test_minutes_count_as_agent_time_and_yours(self):
        self.remember([self.event(BASE + 60 * i) for i in range(10)])
        result = self.cursor()
        self.assertEqual(result['cursor_agent_hours'], round(600 / 3600, 2))
        self.assertEqual(result['sittings'], 1)


class MigrationTest(LedgerTestCase):
    def test_a_new_ledger_is_at_the_latest_layout(self):
        led = ledger.default()
        with led.reading() as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], len(ledger.SCHEMA))
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_a_ledger_from_a_newer_version_is_left_alone(self):
        self.hud.mkdir(parents=True)
        con = sqlite3.connect(self.hud / ledger.DB_NAME)
        con.execute(f'PRAGMA user_version = {len(ledger.SCHEMA) + 1}')
        con.close()
        with self.assertRaises(ledger.LedgerError):
            ledger.default()

    def test_the_old_activity_store_is_taken_in_once_and_the_old_cache_removed(self):
        self.hud.mkdir(parents=True)
        store = self.hud / 'cursor-activity.json'
        store.write_text(json.dumps({'schema_version': 1, 'email': 'test@example.com', 'minutes': {
            'interactive': [BASE, BASE + 60], 'headless': [BASE + 120], 'subagent': []}}), encoding='utf-8')
        (self.hud / 'overview-facts.json').write_text('{}', encoding='utf-8')
        led = ledger.default()
        self.assertEqual(led.cursor_minutes('test@example.com'),
                         {'interactive': [BASE, BASE + 60], 'headless': [BASE + 120], 'subagent': []})
        self.assertFalse((self.hud / 'overview-facts.json').exists())
        # Changed afterwards (an older engine still writing it): not taken in again.
        store.write_text(json.dumps({'schema_version': 1, 'email': 'test@example.com', 'minutes': {
            'interactive': [BASE + 600], 'headless': [], 'subagent': []}}), encoding='utf-8')
        self.assertEqual(self.restart().cursor_minutes('test@example.com')['interactive'], [BASE, BASE + 60])

    def test_an_activity_store_that_is_not_one_is_ignored(self):
        self.hud.mkdir(parents=True)
        (self.hud / 'cursor-activity.json').write_text('{"minutes": ', encoding='utf-8')
        self.assertIsNone(ledger.default().cursor_minutes('test@example.com'))


if __name__ == '__main__':
    unittest.main()
