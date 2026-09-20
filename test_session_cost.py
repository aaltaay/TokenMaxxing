import json
import tempfile
import unittest
from pathlib import Path
import session_cost as c


def usage(inp, cached, out):
    return dict(zip(c.FIELDS, (inp, cached, out)))


def event(last, total=None):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'last_token_usage': last, 'total_token_usage': total or last}}}


MODEL = {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}}


class SessionCostTests(unittest.TestCase):
    def test_cached_tokens_are_not_charged_twice_and_reasoning_is_in_output(self):
        u = dict(usage(100_000, 80_000, 1000), reasoning_output_tokens=900)
        self.assertAlmostEqual(c.request_cents('gpt-6-astra', u), 33)

    def test_long_context_applies_to_whole_request(self):
        self.assertAlmostEqual(c.request_cents('gpt-6-astra', usage(300_000, 200_000, 1000)), 247.5)

    def test_duplicate_events_do_not_add_cost(self):
        a = event(usage(100_000, 80_000, 1000))
        b = event(usage(100_000, 80_000, 1000), usage(200_000, 160_000, 2000))
        result = c.estimate_records([MODEL, a, a, b, b])
        self.assertEqual(result['priced_requests'], 2)
        self.assertAlmostEqual(result['cents'], 66)
        self.assertFalse(result['partial'])

    def test_unknown_model_after_switch_is_not_priced_as_astra(self):
        result = c.estimate_records([MODEL, event(usage(10, 0, 1)),
            {'type': 'turn_context', 'payload': {'model': 'unknown'}},
            event(usage(10, 0, 1), usage(20, 0, 2))])
        self.assertTrue(result['partial'])
        self.assertEqual(result['priced_requests'], 1)
        self.assertIsNone(result['last_request_cents'])

    def test_missing_invalid_or_unknown_data_is_not_zero(self):
        for u in ({}, usage(1, 2, 0), usage(True, 0, 0), usage(-1, 0, 0)):
            self.assertIsNone(c.request_cents('gpt-6-astra', u))
        self.assertIsNone(c.request_cents('unknown', usage(1, 0, 0)))
        self.assertIsNone(c.estimate_records([])['cents'])

    def test_recorded_zero_is_zero(self):
        result = c.estimate_records([MODEL, event(usage(0, 0, 0))])
        self.assertEqual(result['cents'], 0)
        self.assertEqual(result['priced_requests'], 1)

    def test_counter_gap_or_reset_is_partial(self):
        result = c.estimate_records([MODEL, event(usage(10, 0, 1), usage(100, 0, 10)),
            event(usage(10, 0, 1), usage(110, 0, 11)), event(usage(1, 0, 0))])
        self.assertTrue(result['partial'])
        self.assertEqual(result['priced_requests'], 1)

    def test_full_file_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_text('\n'.join(map(json.dumps, [MODEL, event(usage(10, 0, 1))])) + '\n')
            first = c.estimate_file(path)
            with path.open('a') as stream:
                stream.write(json.dumps(event(usage(10, 0, 1), usage(20, 0, 2))) + '\n')
            second = c.estimate_file(path)
            self.assertAlmostEqual(second['cents'], 2 * first['cents'])
            with path.open('a') as stream:
                stream.write('{partial')
            self.assertTrue(c.estimate_file(path)['partial'])


if __name__ == '__main__':
    unittest.main()
