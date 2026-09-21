import json
import tempfile
import time
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
    def test_selected_codex_cost_is_attached_without_exposing_path(self):
        rows=[{'id':'one','name':'One','_path':str(self.path)}]
        with patch.object(s,'file_sessions',return_value=rows), patch.object(s,'codex_titles',return_value={}), patch.object(s.session_cost,'estimate_file',return_value={'cents':33}) as estimate:
            result=s.get_sessions('codex','one')
        self.assertEqual(result['chat']['cost']['cents'],33)
        self.assertNotIn('_path',result['chat'])
        estimate.assert_called_once_with(str(self.path), 'codex')
    def test_open_chat_switches_without_any_log_writes(self):
        rows=[{'id':'background','name':'Background','used':999},
              {'id':'one','name':'One','used':100},{'id':'two','name':'Two','used':200}]
        with patch.object(s,'file_sessions',return_value=rows), patch.object(s,'codex_titles',return_value={'one':'First chat','two':'Second chat'}):
            first=s.get_sessions('codex',follow='active',active_title='First chat')
            second=s.get_sessions('codex',follow='active',active_title='Second chat')
            self.assertEqual(first['chat']['used'],100)
            self.assertEqual(second['chat']['used'],200)
            self.assertEqual(second['selection_mode'],'active')
            self.assertIn('Second chat',second['chat']['name'])
            self.assertEqual(s.get_sessions('codex',follow='latest')['chat']['used'],999)
    def test_unavailable_or_ambiguous_open_chat_never_shows_background_usage(self):
        rows=[{'id':'one','name':'One','used':999}]
        with patch.object(s,'file_sessions',return_value=rows), patch.object(s,'codex_titles',return_value={'one':'Duplicate','two':'Duplicate'}):
            for title in [None,'Unknown','Duplicate']:
                self.assertIsNone(s.get_sessions('codex',follow='active',active_title=title)['chat'])
            self.assertEqual(s.get_sessions('codex','one',follow='active',active_title='Unknown')['chat']['used'],999)
    def test_selected_old_session_is_included_beyond_recent_100(self):
        root=Path(self.tmp.name)/'sessions';root.mkdir()
        for i in range(102):
            p=root/f'rollout-{i:03}.jsonl'
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':f'{i:03}'}}))
            import os
            os.utime(p,(i+1,i+1))
        with patch.dict(s.os.environ,{'CODEX_HOME':self.tmp.name}):
            sessions=s.file_sessions('codex','000')
        self.assertEqual(len(sessions),101)
        self.assertIn('000',[row['id'] for row in sessions])
    def test_claude_follows_the_open_session_reported_by_the_status_line(self):
        home=Path(self.tmp.name)/'hud';home.mkdir()
        (home/'claude-active.json').write_text(json.dumps({'session_id':'two','at':time.time(),
            'model':'Claude Opus','cost_usd':1.25,
            'context_window':{'total_input_tokens':1000,'context_window_size':200000,'used_percentage':0.5}}),encoding='utf-8')
        rows=[{'id':'one','name':'One','used':999},{'id':'two','name':'Two','used':5}]
        with patch.dict(s.os.environ,{'TOKENMAXXING_HOME':str(home)}), patch.object(s,'file_sessions',return_value=rows):
            result=s.get_sessions('claude',follow='active')
            latest=s.get_sessions('claude',follow='latest')
        self.assertEqual(result['chat']['id'],'two')
        self.assertEqual(result['selection_mode'],'active')
        self.assertEqual(result['chat']['limit'],200000)
        self.assertEqual(result['chat']['used'],1000)
        self.assertEqual(result['chat']['cost']['cents'],125)
        self.assertEqual(latest['chat']['id'],'one')

    def test_stale_or_missing_pointer_never_shows_a_background_session(self):
        home=Path(self.tmp.name)/'hud2';home.mkdir()
        rows=[{'id':'one','name':'One','used':999,'updated_at':time.time()-s.RECENT_LOG_SECONDS-1}]
        with patch.dict(s.os.environ,{'TOKENMAXXING_HOME':str(home)}), patch.object(s,'file_sessions',return_value=rows):
            result=s.get_sessions('claude',follow='active')
            self.assertIsNone(result['chat'])
            self.assertIsNone(result['follow_source'])
            self.assertIn('15 minutes',result['reason'])
            (home/'claude-active.json').write_text(json.dumps({'session_id':'one','at':time.time()-s.POINTER_MAX_AGE-1}),encoding='utf-8')
            s._cache.clear()
            self.assertIsNone(s.get_sessions('claude',follow='active')['chat'])

    def test_without_a_status_line_the_log_being_written_is_the_open_session(self):
        home=Path(self.tmp.name)/'hud3';home.mkdir()
        rows=[{'id':'live','name':'Live','used':5,'updated_at':time.time()-30},
              {'id':'old','name':'Old','used':999,'updated_at':time.time()-3600}]
        with patch.dict(s.os.environ,{'TOKENMAXXING_HOME':str(home)}), patch.object(s,'file_sessions',return_value=rows):
            result=s.get_sessions('claude',follow='active')
            self.assertEqual(result['chat']['id'],'live')
            self.assertEqual(result['selection_mode'],'active')
            self.assertEqual(result['follow_source'],'recent log')
            pinned=s.get_sessions('claude',pinned='old',follow='active')
            self.assertEqual(pinned['chat']['id'],'old')
            self.assertIsNone(pinned['follow_source'])

    def test_open_claude_window_title_selects_that_session_ahead_of_the_busiest_log(self):
        config=Path(self.tmp.name)/'claude';(config/'sessions').mkdir(parents=True)
        (config/'sessions'/'1.json').write_text(json.dumps({'pid':1,'sessionId':'nova','name':'Pull latest','updatedAt':5}),encoding='utf-8')
        (config/'sessions'/'2.json').write_text(json.dumps({'pid':2,'sessionId':'busy','name':'Usage tracking','updatedAt':9}),encoding='utf-8')
        (config/'sessions'/'3.json').write_text('{not json',encoding='utf-8')
        rows=[{'id':'busy','name':'busy','used':1,'updated_at':time.time()-5},
              {'id':'nova','name':'nova','used':2,'updated_at':time.time()-200}]
        home=Path(self.tmp.name)/'hud4';home.mkdir()
        with patch.dict(s.os.environ,{'CLAUDE_CONFIG_DIR':str(config),'TOKENMAXXING_HOME':str(home)}), patch.object(s,'file_sessions',return_value=rows):
            self.assertEqual(s.claude_titles(),{'nova':'Pull latest','busy':'Usage tracking'})
            shown=s.get_sessions('claude',follow='active',active_title='Pull latest')
            self.assertEqual(shown['chat']['id'],'nova')
            self.assertEqual(shown['follow_source'],'open window')
            self.assertEqual(shown['chat']['name'],'Pull latest · nova')
            self.assertEqual([c['name'] for c in shown['chats']],['Usage tracking · busy','Pull latest · nova'])
            s._cache.clear()
            unknown=s.get_sessions('claude',follow='active',active_title='Never seen')
            self.assertEqual(unknown['chat']['id'],'busy')
            self.assertEqual(unknown['follow_source'],'recent log')
            s._cache.clear()
            pointer_wins=s.get_sessions('claude',follow='latest',active_title='Pull latest')
            self.assertEqual(pointer_wins['chat']['id'],'busy')
            self.assertIsNone(pointer_wins['follow_source'])

    def test_selected_claude_cost_is_estimated_from_its_log(self):
        self.path.write_text(json.dumps({'type':'assistant','sessionId':'c1','requestId':'r1','cwd':'/w/proj','message':{'model':'claude-opus-5',
            'usage':{'input_tokens':1_000_000,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'output_tokens':0}}})+'\n',encoding='utf-8')
        rows=[{'id':'c1','name':'One','used':1,'updated_at':time.time(),'_path':str(self.path)}]
        with patch.object(s,'file_sessions',return_value=rows):
            result=s.get_sessions('claude',follow='latest')
        self.assertAlmostEqual(result['chat']['cost']['cents'],500)
        self.assertNotIn('_path',result['chat'])

    def test_unchanged_log_is_not_read_again_and_only_the_selection_is_detailed(self):
        self.path.write_text(json.dumps({'type':'assistant','sessionId':'abc','cwd':'C:/work/p',
            'message':{'model':'claude','usage':{'input_tokens':1,'cache_read_input_tokens':2,
            'cache_creation_input_tokens':3,'output_tokens':4}}})+'\n',encoding='utf-8')
        root=Path(self.tmp.name)/'projects'/'p';root.mkdir(parents=True)
        target=root/'abc.jsonl';target.write_bytes(self.path.read_bytes())
        s._parsed.clear()
        with patch.dict(s.os.environ,{'CLAUDE_CONFIG_DIR':self.tmp.name}), patch.object(s,'parse_session',wraps=s.parse_session) as parse:
            first=s.get_sessions('claude')
            s._cache.clear()
            second=s.get_sessions('claude')
        self.assertEqual(first['chat']['used'],6)
        self.assertEqual(second['chat']['used'],6)
        # One head read for the list, one tail read for the selection; the
        # second poll re-reads nothing because the log did not change.
        self.assertEqual(parse.call_count,2)

    def test_listed_sessions_show_known_totals_and_queue_the_rest(self):
        rows=[{'id':'a','name':'A','updated_at':1,'_path':'a.jsonl'},{'id':'b','name':'B','updated_at':2,'_path':'b.jsonl'}]
        known={'cents':150,'priced_requests':3,'model':'claude-opus-5','partial':False}
        with patch.object(s.session_cost,'cached_summary',side_effect=lambda path,provider: known if path=='a.jsonl' else None), \
             patch.object(s,'_summarize_later') as later:
            listed=s._chat_rows('claude',rows)
        self.assertEqual(listed[0],{'id':'a','name':'A','updated_at':1,'cents':150,'requests':3,'model':'claude-opus-5','partial':False})
        self.assertEqual(listed[1],{'id':'b','name':'B','updated_at':2})
        later.assert_called_once_with([('b.jsonl','claude')])

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
