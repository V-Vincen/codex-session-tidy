from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import cleanup_confirmed as cleanup
import migrate_confirmed as migration
import session_tidy as core


class ConfirmedCleanup(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home, self.base = self.root / 'codex', self.root / 'AiCode'
        self.home.mkdir()
        (self.home / 'sessions').mkdir()
        paths = {'full': 'my_skill/fullstack-quality-kit', 'temp': 'temp', 'bird': 'cm_bird',
                 'dev': 'devops', 'lkb': 'lkb', 'tidy': 'my_skill/codex-session-tidy'}
        names = {'full': 'fullstack-quality-kit', 'temp': 'temp', 'bird': 'cm_bird',
                 'dev': 'devops', 'lkb': 'lkb', 'tidy': 'codex-session-tidy'}
        for path in paths.values():
            (self.base / path).mkdir(parents=True)
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.executescript('''CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE project_roots(project_id TEXT REFERENCES projects(id),position INTEGER,path TEXT,
                    PRIMARY KEY(project_id,position));
                CREATE TABLE project_idempotency_keys(key TEXT PRIMARY KEY,project_id TEXT,created_at_ms INTEGER);
                CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,project_id TEXT REFERENCES projects(id),
                    archived INTEGER,rollout_path TEXT,attached_thread_parent_id TEXT);
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);''')
            for pid in paths:
                db.execute('INSERT INTO projects VALUES (?,?)', (pid, names[pid]))
                db.execute('INSERT INTO project_roots VALUES (?,?,?)', (pid, 0, str(self.base / paths[pid])))
                body = self.home / 'sessions' / (pid + '.jsonl')
                body.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': pid, 'cwd': str(self.base / paths[pid])}}) + '\n')
                db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                           (pid, names[pid], str(self.base / paths[pid]), pid, int(pid == 'temp'), str(body), None))
            for pid, extra in (('dev', self.base / 'cm_bird'), ('full', self.base), ('lkb', self.base), ('tidy', self.base / 'temp')):
                db.execute('INSERT INTO project_roots VALUES (?,?,?)', (pid, 1, str(extra)))
            db.execute('INSERT INTO projects VALUES (?,?)', ('old', 'KKAI'))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('old', 0, str(self.base / 'KKAI')))
            db.execute('INSERT INTO project_idempotency_keys VALUES (?,?,?)', ('old-request', 'old', 1))
            db.execute('INSERT INTO project_idempotency_keys VALUES (?,?,?)', ('valid-request', 'tidy', 1))
            db.execute('INSERT INTO projects VALUES (?,?)', ('new-empty', '刚创建'))
        g = {'local-projects': {pid.upper(): {'name': names[pid], 'rootPaths': [str(self.base / paths[pid])]}
                               for pid in paths},
             'app-server-project-id-by-legacy-project-id-by-host': {'local': {pid.upper(): pid for pid in paths}},
             'thread-project-assignments': {pid: {'projectId': pid.upper()} for pid in paths},
             'projectless-thread-ids': [], 'sidebar-project-thread-orders': {}, 'unrelated': {'window': 1},
             'electron-owner-value:thread-writable-roots:test': {'lkb': [str(self.base)],
                                                               'tidy': [str(self.base / paths['tidy']), str(self.base / 'temp')]}}
        g['local-projects']['NEW'] = {'name': '刚创建', 'rootPaths': [str(self.base / 'temp')]}
        g['app-server-project-id-by-legacy-project-id-by-host']['local']['NEW'] = 'new-empty'
        for pid, extra in (('lkb', self.base), ('tidy', self.base / 'temp')):
            g['local-projects'][pid.upper()]['rootPaths'].append(str(extra))
        core.save(self.home / migration.GLOBAL, g)
        with closing(sqlite3.connect(self.home / 'thread_history_1.sqlite')) as db, db:
            db.executescript('''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,PRIMARY KEY(thread_id,turn_id));
                CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,item_json TEXT,PRIMARY KEY(thread_id,item_id));
                CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_ordinal INTEGER);
                CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,item_json TEXT,PRIMARY KEY(thread_id,item_id));''')
            for tid in ('orphan', 'tidy'):
                db.execute('INSERT INTO thread_turns VALUES (?,?)', (tid, 'turn'))
                db.execute('INSERT INTO thread_items VALUES (?,?,?)', (tid, 'item', json.dumps({'text': '历史提到 old、orphan'})))
                db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)', (tid, 1))
        self.index = b'{"id":"tidy","thread_name":"before"}\n{"id":"tidy","thread_name":"after"}\n'
        (self.home / 'session_index.jsonl').write_bytes(self.index)
        real_load = core.load
        with core.connect(self.home / 'thread_history_1.sqlite') as db:
            history_schema = cleanup.schema_hash(db)
        schemas = {'fixture': core.digest(core.snapshot(self.home)['schema']), 'history': history_schema}
        def load(path):
            return schemas if str(path).endswith('supported-schemas.json') else real_load(path)
        self.load_patch = patch.object(core, 'load', side_effect=load)
        self.load_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.process_patch = patch.object(core, 'active', return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def plan(self):
        p = cleanup.build(self.home, self.base)
        path = migration.store_plan(p)
        return p, path.parent

    def execute(self, p, run):
        return migration.execute(p, run, self.root / 'backups', timeout=0)

    def hashes(self):
        result = {}
        for name in ('state_5.sqlite', 'thread_history_1.sqlite'):
            with core.connect(self.home / name) as db:
                result[name] = migration.logical_hash(db)
        result['global'] = migration.hash_file(self.home / migration.GLOBAL)
        return result

    def test_cleanup_repeat_and_full_restore(self):
        before = self.hashes()
        state = core.snapshot(self.home)
        bodies = {str(p): p.read_bytes() for p in (self.home / 'sessions').iterdir()}
        p, run = self.plan()
        self.assertEqual(self.hashes(), before)
        self.assertEqual(len(p['root_removals']), 4)
        self.assertEqual(p['cleanup']['history_ids'], ['orphan'])
        self.assertEqual([r['id'] for r in p['cleanup']['projects']['projects']], ['old'])
        result = self.execute(p, run)
        self.assertEqual(result['projects_removed'], 1)
        self.assertEqual(result['history_rows_removed'], 3)
        self.assertEqual(self.execute(p, run), result)
        self.assertEqual(core.snapshot(self.home)['threads'], state['threads'])
        g = core.load(self.home / migration.GLOBAL)
        self.assertEqual(g['local-projects']['LKB']['rootPaths'], [str(self.base / 'lkb')])
        self.assertEqual(g['electron-owner-value:thread-writable-roots:test']['lkb'], [str(self.base / 'lkb')])
        self.assertEqual(g['unrelated'], {'window': 1})
        self.assertEqual((self.home / 'session_index.jsonl').read_bytes(), self.index)
        self.assertEqual({str(p): p.read_bytes() for p in (self.home / 'sessions').iterdir()}, bodies)
        with core.connect(self.home / 'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM project_idempotency_keys').fetchone()[0], 1)
            self.assertIsNotNone(db.execute('SELECT id FROM projects WHERE id=?', ('new-empty',)).fetchone())
        fresh = cleanup.build(self.home, self.base)
        self.assertFalse(fresh['root_removals'])
        self.assertFalse(fresh['cleanup']['projects']['projects'])
        self.assertFalse(fresh['cleanup']['history_ids'])
        migration.restore(p, run)
        self.assertEqual(self.hashes(), before)

    def test_extra_mount_detection_is_data_driven_for_unlisted_project(self):
        agent = self.base / 'agent-task-watchdog'
        agent.mkdir()
        body = self.home / 'sessions/agent.jsonl'
        body.write_text(json.dumps({'type': 'session_meta', 'payload':
                                    {'id': 'agent', 'cwd': str(agent)}}) + '\n')
        with core.connect(self.home / 'state_5.sqlite', True) as db, db:
            db.execute('INSERT INTO projects VALUES (?,?)', ('agent', 'agent-task-watchdog'))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('agent', 0, str(agent)))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('agent', 1, str(self.base / 'devops')))
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                       ('agent', '监控任务', str(agent), 'agent', 0, str(body), None))
        g = core.load(self.home / migration.GLOBAL)
        g['local-projects']['AGENT'] = {'name': 'agent-task-watchdog',
                                        'rootPaths': [str(agent), str(self.base / 'devops')]}
        g['app-server-project-id-by-legacy-project-id-by-host']['local']['AGENT'] = 'agent'
        g['thread-project-assignments']['agent'] = {'projectId': 'AGENT'}
        core.save(self.home / migration.GLOBAL, g)
        p, _ = self.plan()
        self.assertIn(str(self.base / 'devops'), [r['path'] for r in p['root_removals']])
        agent_item = next(item for item in p['cleanup']['sidebar'] if item['legacy'] == 'AGENT')
        self.assertEqual(agent_item['after'], [str(agent)])
        self.execute(p, _)
        fresh = cleanup.build(self.home, self.base)
        self.assertFalse(any(r['project_id'] == 'agent' for r in fresh['root_removals']))
        self.assertEqual(core.load(self.home / migration.GLOBAL)['local-projects']['AGENT']['rootPaths'], [str(agent)])

    def test_live_parent_reference_protects_history(self):
        with core.connect(self.home / 'state_5.sqlite', True) as db, db:
            db.execute('UPDATE threads SET attached_thread_parent_id=? WHERE id=?', ('orphan', 'tidy'))
        p, _ = self.plan()
        self.assertFalse(p['cleanup']['history_ids'])
        self.assertIn('orphan', p['cleanup']['blocked'])

    def test_structured_body_lineage_protects_history(self):
        path = self.home / 'sessions/tidy.jsonl'
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'session_meta', 'payload': {'forked_from_id': 'orphan'}}) + '\n')
        p, _ = self.plan()
        self.assertFalse(p['cleanup']['history_ids'])
        self.assertIn('orphan', p['cleanup']['blocked'])

    def test_filename_is_not_body_identity(self):
        path = self.home / 'sessions/old_orphan.jsonl'
        path.write_bytes((self.home / 'sessions/tidy.jsonl').read_bytes())
        p, _ = self.plan()
        self.assertEqual(p['cleanup']['history_ids'], ['orphan'])

    def test_archived_thread_is_not_orphan(self):
        with core.connect(self.home / 'thread_history_1.sqlite', True) as db, db:
            db.execute('INSERT INTO thread_turns VALUES (?,?)', ('temp', 'turn'))
        p, _ = self.plan()
        self.assertNotIn('temp', p['cleanup']['history_ids'])

    def test_other_database_reference_protects_project(self):
        with closing(sqlite3.connect(self.home / 'queue_1.sqlite')) as db, db:
            db.execute('CREATE TABLE tasks(project_id TEXT)')
            db.execute('INSERT INTO tasks VALUES (?)', ('old',))
        p, _ = self.plan()
        self.assertFalse(p['cleanup']['projects']['projects'])
        self.assertIn('old', p['cleanup']['blocked'])

    def test_diagnostic_logs_do_not_keep_deleted_history_alive(self):
        with closing(sqlite3.connect(self.home / 'logs_2.sqlite')) as db, db:
            db.execute('CREATE TABLE logs(ts INTEGER,level TEXT,target TEXT,feedback_log_body TEXT,thread_id TEXT)')
            db.execute('INSERT INTO logs VALUES (?,?,?,?,?)', (1, 'INFO', 'history', '旧会话事件', 'orphan'))
        p, run = self.plan()
        self.assertEqual(p['cleanup']['history_ids'], ['orphan'])
        self.execute(p, run)
        with core.connect(self.home / 'logs_2.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 1)

    def test_missing_valid_body_blocks_cleanup(self):
        (self.home / 'sessions/tidy.jsonl').unlink()
        with self.assertRaises(core.Blocked):
            self.plan()

    def test_new_key_after_prepare_blocks_before_backup(self):
        p, run = self.plan()
        with core.connect(self.home / 'state_5.sqlite', True) as db, db:
            db.execute('INSERT INTO project_idempotency_keys VALUES (?,?,?)', ('new-key', 'old', 2))
        with self.assertRaises(core.Blocked):
            self.execute(p, run)
        self.assertFalse((run / 'recovery.json').exists())

    def test_new_lineage_after_prepare_blocks(self):
        p, run = self.plan()
        with (self.home / 'sessions/tidy.jsonl').open('a') as stream:
            stream.write(json.dumps({'type': 'event', 'payload': {'parent_thread_id': 'orphan'}}) + '\n')
        with self.assertRaises(core.Blocked):
            self.execute(p, run)

    def test_history_drift_blocks_before_backup(self):
        p, run = self.plan()
        with core.connect(self.home / 'thread_history_1.sqlite', True) as db, db:
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=?', ('{}', 'orphan'))
        with self.assertRaises(core.Blocked):
            self.execute(p, run)
        self.assertFalse((run / 'recovery.json').exists())

    def test_unknown_history_schema_blocks(self):
        with core.connect(self.home / 'thread_history_1.sqlite', True) as db, db:
            db.execute('ALTER TABLE thread_items ADD COLUMN unknown TEXT')
        with self.assertRaises(core.Blocked):
            self.plan()

    def test_config_replace_failure_restores_both_databases(self):
        p, run = self.plan()
        before = self.hashes()
        original = migration.atomic_bytes
        def fail_once(*args):
            original(*args)
            if migration.atomic_bytes.call_count == 1:
                raise OSError('模拟配置替换后异常')
        with patch.object(migration, 'atomic_bytes', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.execute(p, run)
        self.assertEqual(self.hashes(), before)

    def test_failure_after_commit_restores_both_databases(self):
        p, run = self.plan()
        before = self.hashes()
        with patch.object(migration, 'validate', side_effect=core.Blocked('模拟提交后验收失败')):
            with self.assertRaises(core.Blocked):
                self.execute(p, run)
        self.assertEqual(self.hashes(), before)

    def test_restore_refuses_history_changed_by_app(self):
        p, run = self.plan()
        self.execute(p, run)
        with core.connect(self.home / 'thread_history_1.sqlite', True) as db, db:
            db.execute('INSERT INTO thread_turns VALUES (?,?)', ('tidy', 'new-turn'))
        with self.assertRaises(core.Blocked):
            migration.restore(p, run)

    def test_running_app_blocks_without_changes(self):
        p, run = self.plan()
        before = self.hashes()
        with patch.object(core, 'active', return_value=[999]):
            with self.assertRaises(core.Blocked):
                self.execute(p, run)
        self.assertEqual(self.hashes(), before)

    def test_second_job_cannot_acquire_same_data_lock(self):
        p, run = self.plan()
        before = self.hashes()
        with core.locked(self.home):
            with self.assertRaises(core.Blocked):
                self.execute(p, run)
        self.assertEqual(self.hashes(), before)

    def test_sidebar_drift_blocks_without_changes(self):
        p, run = self.plan()
        g = core.load(self.home / migration.GLOBAL)
        g['local-projects']['LKB']['rootPaths'] = [str(self.base / 'lkb')]
        core.save(self.home / migration.GLOBAL, g)
        before = self.hashes()
        with self.assertRaises(core.Blocked):
            self.execute(p, run)
        self.assertEqual(self.hashes(), before)

    def test_compressed_body_identity_protects_history(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest('压缩正文核验需要 Python 3.14+')
        path = self.home / 'sessions/orphan.jsonl.zst'
        with zstd.open(path, 'wb') as stream:
            stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': 'orphan'}}).encode() + b'\n')
        p, _ = self.plan()
        self.assertFalse(p['cleanup']['history_ids'])


if __name__ == '__main__':
    unittest.main()
