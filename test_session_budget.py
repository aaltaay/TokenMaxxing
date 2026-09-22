import json
import os
import tempfile
import unittest
from pathlib import Path

import session_budget as b

HOUR = 3600
WEEK_RESET = 2_000_000_000
SESSION_RESET = WEEK_RESET - 3 * 86400


def reading(session, weekly, session_reset=SESSION_RESET, week_reset=WEEK_RESET, extra=()):
    return [{'id': 'five_hour', 'used_percent': session, 'resets_at': session_reset, 'window_minutes': 300},
            {'id': 'seven_day', 'used_percent': weekly, 'resets_at': week_reset, 'window_minutes': 10080},
            *extra]


class LedgerTests(unittest.TestCase):
    def feed(self, readings, provider='claude'):
        state = {}
        for at, windows in enumerate(readings, start=1):
            b.observe(state, provider, windows, at * 120)
        return state

    def test_a_session_worth_of_movement_measures_the_weekly_share(self):
        # 0 -> 44 on the 5-hour window moved the weekly window 70 -> 75: 5/44.
        state = self.feed([reading(0, 70), reading(9, 71), reading(20, 72), reading(33, 74), reading(44, 75)])
        [pair] = b.estimates(state, 'claude', reading(44, 75))
        self.assertEqual(pair['status'], 'measured')
        self.assertAlmostEqual(pair['share'], 5 / 44)

    def test_resets_never_read_as_usage(self):
        state = self.feed([reading(40, 70), reading(2, 71, session_reset=SESSION_RESET + 5 * HOUR),
                           reading(5, 1, session_reset=SESSION_RESET + 5 * HOUR, week_reset=WEEK_RESET + 7 * 86400)])
        [pair] = b.estimates(state, 'claude', reading(5, 1))
        self.assertEqual(pair['session_points'], 0)
        self.assertEqual(pair['status'], 'measuring')

    def test_too_little_movement_is_still_measuring(self):
        state = self.feed([reading(0, 70), reading(10, 71)])
        [pair] = b.estimates(state, 'claude', reading(10, 71))
        self.assertEqual(pair['status'], 'measuring')
        self.assertIsNone(pair['share'])
        self.assertEqual(pair['session_points'], 10)

    def test_a_held_reading_is_not_counted_twice(self):
        state = {}
        b.observe(state, 'claude', reading(0, 70), 100)
        b.observe(state, 'claude', reading(30, 74), 200)
        b.observe(state, 'claude', reading(30, 74), 200)
        b.observe(state, 'claude', reading(0, 70), 150)
        [pair] = b.estimates(state, 'claude', reading(30, 74))
        self.assertEqual(pair['session_points'], 30)

    def test_pairs_form_only_inside_one_quota_bucket(self):
        scoped = lambda used: {'id': 'weekly_scoped:fable', 'used_percent': used, 'resets_at': WEEK_RESET, 'window_minutes': 10080}
        codex = [{'id': 'codex:primary', 'used_percent': 0, 'resets_at': SESSION_RESET, 'window_minutes': 300},
                 {'id': 'codex:secondary', 'used_percent': 10, 'resets_at': WEEK_RESET, 'window_minutes': 10080},
                 {'id': 'codex_other:secondary', 'used_percent': 10, 'resets_at': WEEK_RESET, 'window_minutes': 10080}]
        # Pairs only form inside one quota bucket.
        self.assertEqual([(s['id'], w['id']) for s, w in b.pairs(codex)], [('codex:primary', 'codex:secondary')])
        self.assertEqual([(s['id'], w['id']) for s, w in b.pairs(reading(0, 0, extra=[scoped(38)]))], [('five_hour', 'seven_day')])

    def test_a_weekly_cap_that_never_moves_with_the_session_is_unrelated(self):
        state = self.feed([reading(0, 40), reading(35, 40), reading(70, 40)])
        [pair] = b.estimates(state, 'claude', reading(70, 40))
        self.assertEqual(pair['status'], 'unrelated')

    def test_only_recent_weekly_cycles_are_kept(self):
        state = {}
        at = 0
        for week in range(5):
            reset = WEEK_RESET + week * 7 * 86400
            for session, weekly in ((0, 0), (50, 5)):
                at += 1
                b.observe(state, 'claude', reading(session, weekly, session_reset=reset - HOUR, week_reset=reset), at)
        cycles = state['claude']['pairs']['five_hour|seven_day']
        self.assertEqual(len(cycles), b.KEEP_CYCLES)
        self.assertEqual(cycles[-1]['week'], WEEK_RESET + 4 * 7 * 86400)


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prior = os.environ.get('TOKEN_HUD_DIR')
        os.environ['TOKEN_HUD_DIR'] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__('TOKEN_HUD_DIR', self.prior) if self.prior else os.environ.pop('TOKEN_HUD_DIR', None))

    def snapshot(self, session, weekly, at, **extra):
        return {'providers': [{'id': 'claude', 'status': 'ok', 'fetched_at': at, 'windows': reading(session, weekly), **extra}]}

    def test_fresh_readings_persist_and_held_ones_are_ignored(self):
        b.attach(self.snapshot(0, 70, 100))
        b.attach(self.snapshot(40, 90, 200, warning='Reusing the last reading'))
        result = b.attach(self.snapshot(30, 74, 300))
        [pair] = result['providers'][0]['budget']
        self.assertEqual(pair['session_points'], 30)
        self.assertEqual(pair['status'], 'measured')
        saved = json.loads((Path(self.tmp.name) / 'session-budget.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['claude']['at'], 300)

    def test_a_corrupt_ledger_starts_over_instead_of_failing(self):
        (Path(self.tmp.name) / 'session-budget.json').write_text('{broken', encoding='utf-8')
        result = b.attach(self.snapshot(0, 70, 100))
        self.assertEqual(result['providers'][0]['budget'][0]['status'], 'measuring')


if __name__ == '__main__':
    unittest.main()
