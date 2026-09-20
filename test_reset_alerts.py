import copy
import unittest
import reset_alerts as ra

class AlertsTest(unittest.TestCase):
    def snapshot(self, now=10000, reset=10900, status='ok'):
        return {'providers':[{'id':'claude','label':'Claude','status':status,'fetched_at':now,
            'windows':[{'id':'five_hour','label':'5-hour','used_percent':43,'resets_at':reset,'window_minutes':300}]}]}
    def test_fifteen_minutes_and_exactly_once(self):
        state={}
        self.assertEqual(ra.evaluate(self.snapshot(reset=10901),{},state,10000),[])
        result=ra.evaluate(self.snapshot(),{},state,10000)
        self.assertEqual(result[0]['phase'],'prewarn')
        self.assertIn('15 minutes',result[0]['message'])
        self.assertEqual(ra.evaluate(self.snapshot(),{},state,10000),[])
    def test_time_passing_alone_cannot_claim_reset(self):
        state={}
        ra.evaluate(self.snapshot(),{},state,10000)
        self.assertEqual(ra.evaluate(self.snapshot(now=10901),{},state,10901),[])
        self.assertEqual(ra.evaluate(self.snapshot(now=10901,status='unavailable'),{},state,10901),[])
    def test_fresh_next_window_confirms_reset_after_restart(self):
        state={}
        ra.evaluate(self.snapshot(),{},state,10000)
        restored=copy.deepcopy(state)
        events=ra.evaluate(self.snapshot(now=10910,reset=28900),{},restored,10910)
        self.assertEqual([e['phase'] for e in events],['reset'])
        self.assertEqual(ra.evaluate(self.snapshot(now=10910,reset=28900),{},restored,10910),[])
    def test_changed_schedule_before_boundary_is_not_a_reset(self):
        state={}
        ra.evaluate(self.snapshot(),{},state,10000)
        self.assertEqual(ra.evaluate(self.snapshot(now=10001,reset=28900),{},state,10001),[])
    def test_stale_and_disabled_do_not_alert(self):
        self.assertEqual(ra.evaluate(self.snapshot(),{}, {}, 10200),[])
        self.assertEqual(ra.evaluate(self.snapshot(),{'buzz_enabled':False},{},10000),[])
        self.assertEqual(ra.evaluate(self.snapshot(),{'providers':{'claude':{'session_enabled':False}}},{},10000),[])
    def test_reset_still_works_with_prewarning_disabled(self):
        state={}; cfg={'prewarn_enabled':False}
        self.assertEqual(ra.evaluate(self.snapshot(),cfg,state,10000),[])
        self.assertEqual(ra.evaluate(self.snapshot(now=10901,reset=28900),cfg,state,10901)[0]['phase'],'reset')
    def test_unobserved_window_does_not_invent_a_reset(self):
        self.assertEqual(ra.evaluate(self.snapshot(now=10901,reset=28900),{}, {},10901),[])

if __name__=='__main__': unittest.main()
