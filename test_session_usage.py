import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3
import session_usage as s

class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'session.jsonl'
        s._cache.clear()
    def parse(self,provider,records):
        self.path.write_text('\n'.join(json.dumps(r) for r in records)+'\n{partial',encoding='utf-8')
        return s.parse_session(provider,self.path,100)
    def test_codex_uses_last_input_not_cumulative_context(self):
        r=self.parse('codex',[{'type':'session_meta','payload':{'id':'one','cwd':'C:/work/project','source':'cli'}},
          {'type':'event_msg','timestamp':'2026-01-01T00:00:00Z','payload':{'type':'token_count','info':{
          'last_token_usage':{'input_tokens':40,'cached_input_tokens':30,'output_tokens':4},
          'total_token_usage':{'total_tokens':9999},'model_context_window':100}}}])
        self.assertEqual(r['used'],40);self.assertEqual(r['limit'],100)
        self.assertEqual(r['metrics']['Recorded cumulative tokens'],9999)
    def test_codex_subagents_are_excluded(self):
        self.assertIsNone(self.parse('codex',[{'type':'session_meta','payload':{'source':{'subagent':{}}}}]))
    def test_claude_duplicates_are_not_summed_and_errors_do_not_replace_real_usage(self):
        row={'type':'assistant','message':{'model':'claude','usage':{'input_tokens':2,'cache_read_input_tokens':30,'cache_creation_input_tokens':5,'output_tokens':4}}}
        r=self.parse('claude',[row,row,{'type':'assistant','isApiErrorMessage':True,'message':{'model':'<synthetic>','usage':{'input_tokens':0}}}])
        self.assertEqual(r['used'],37);self.assertIsNone(r['limit'])
        self.assertEqual(r['metrics']['Last request output'],4)
    def test_missing_cache_fields_remain_unknown(self):
        r=self.parse('claude',[{'type':'assistant','message':{'usage':{'input_tokens':0,'output_tokens':0}}}])
        self.assertIsNone(r['used']);self.assertEqual(r['metrics']['Last request output'],0)
    def test_transcript_text_is_never_returned(self):
        r=self.parse('claude',[{'type':'user','message':{'content':'private secret conversation'}}])
        self.assertNotIn('private secret',json.dumps(r));self.assertEqual(r['metrics'],{})
    def test_pin_selects_requested_session_not_latest(self):
        rows=[{'id':'new','name':'New'},{'id':'old','name':'Old'}]
        with patch.object(s,'file_sessions',return_value=rows):
            self.assertEqual(s.get_sessions('codex','old')['chat']['id'],'old')
            self.assertEqual(s.get_sessions('codex')['chat']['id'],'new')
            self.assertIsNone(s.get_sessions('codex','missing')['chat'])
    def test_cursor_skips_missing_stale_selection_and_uses_valid_records(self):
        db=Path(self.tmp.name)/'cursor.db'
        con=sqlite3.connect(db)
        con.executescript('CREATE TABLE composerHeaders(composerId TEXT,recency INTEGER,isArchived INTEGER,isSubagent INTEGER,value TEXT); CREATE TABLE cursorDiskKV(key TEXT,value TEXT);')
        con.executemany('INSERT INTO composerHeaders VALUES (?,?,0,0,?)',[('missing',300,'{}'),('valid',200,'{}'),('old',100,'{}')])
        con.executemany('INSERT INTO cursorDiskKV VALUES (?,?)',[('composerData:valid','{"name":"Valid","contextTokensUsed":0}'),('composerData:old','{"name":"Old"}')])
        con.commit();con.close()
        with patch.object(s.cu,'cursor_state_db',return_value=db):
            result=s.get_sessions('cursor')
        self.assertEqual(result['chat']['id'],'valid');self.assertEqual(result['chat']['used'],0)
        self.assertEqual(len(result['chats']),2)

if __name__=='__main__':unittest.main()
