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


def claude(model, usage, request='req-1', **extra):
    return {'type': 'assistant', 'requestId': request, 'message': {'model': model, 'usage': usage}, **extra}


class ClaudeSessionCostTests(unittest.TestCase):
    def test_each_token_class_is_priced_at_its_own_rate(self):
        usage = {'input_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000,
                 'cache_creation_input_tokens': 1_000_000, 'output_tokens': 1_000_000}
        self.assertAlmostEqual(c.claude_request_cents('claude-opus-5', usage), (5 + 0.5 + 6.25 + 25) * 100)
        self.assertAlmostEqual(c.claude_request_cents('claude-fable-5-1', usage), (10 + 0.25 + 12.5 + 50) * 100)

    def test_one_hour_cache_writes_cost_twice_input(self):
        usage = {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 1_000_000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 250_000, 'ephemeral_1h_input_tokens': 750_000}}
        self.assertAlmostEqual(c.claude_request_cents('claude-sonnet-5', usage), (0.25 * 2.5 + 0.75 * 4.0) * 100)

    def test_dated_and_long_context_model_ids_price_like_the_base_model(self):
        self.assertEqual(c.claude_rates('claude-opus-4-6-20260201'), c.CLAUDE_RATES['claude-opus-4-6'])
        self.assertEqual(c.claude_rates('claude-sonnet-5[1m]'), c.CLAUDE_RATES['claude-sonnet-5'])
        self.assertIsNone(c.claude_rates('claude-fable-5-2'))

    def test_streaming_records_sharing_a_request_id_are_one_request(self):
        usage = {'input_tokens': 10, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': 100}
        records = [claude('claude-opus-5', usage), claude('claude-opus-5', usage), claude('claude-opus-5', usage),
                   claude('claude-opus-5', usage, request='req-2')]
        result = c.estimate_records_for('claude', records)
        self.assertEqual(result['priced_requests'], 2)
        self.assertAlmostEqual(result['cents'], 2 * (10 * 5 + 100 * 25) / 10_000)
        self.assertFalse(result['partial'])

    def test_unknown_models_errors_and_sidechains_are_left_unpriced(self):
        usage = {'input_tokens': 10, 'output_tokens': 10}
        records = [claude('claude-opus-5', usage), claude('mystery-model', usage, request='req-2'),
                   claude('claude-opus-5', usage, request='req-3', isApiErrorMessage=True),
                   claude('claude-opus-5', usage, request='req-4', isSidechain=True),
                   claude('<synthetic>', usage, request='req-5')]
        result = c.estimate_records_for('claude', records)
        self.assertEqual(result['priced_requests'], 1)
        self.assertEqual(result['skipped_records'], 1)
        self.assertTrue(result['partial'])

    def test_claude_file_estimate_is_cached_per_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'log.jsonl'
            path.write_text(json.dumps(claude('claude-haiku-4-5', {'input_tokens': 1_000_000, 'output_tokens': 0})) + '\n', encoding='utf-8')
            self.assertAlmostEqual(c.estimate_file(path, 'claude')['cents'], 100)
            self.assertIsNone(c.estimate_file(path, 'codex')['cents'])


if __name__ == '__main__':
    unittest.main()
