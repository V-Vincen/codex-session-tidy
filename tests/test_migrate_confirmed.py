import copy
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import migrate_confirmed as migration
import session_tidy as core


class MigrationWorkflow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.base = self.root / 'AiCode'
        for name in ('my_skill/fullstack-quality-kit', 'temp', 'cm_bird', 'lkb'):
            (self.base / name).mkdir(parents=True)
        self.db = self.home / 'state_5.sqlite'
        self.old = str(self.base / 'fullstack-quality-kit')
        self.kkai = str(self.base / 'KKAI')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('''CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE project_roots(project_id TEXT,path TEXT);
                CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,project_id TEXT,
                archived INTEGER,rollout_path TEXT);''')
            # position 是真实适配器必需字段；本隔离结构通过测试专用白名单注入。
            db.execute('ALTER TABLE project_roots ADD COLUMN position INTEGER')
            for pid, name, path in [('full', 'fullstack-quality-kit', self.old), ('temp', 'temp', str(self.base / 'temp')),
                                     ('bird', 'cm_bird', str(self.base / 'cm_bird')), ('lkb', 'lkb', str(self.base / 'lkb'))]:
                db.execute('INSERT INTO projects VALUES (?,?)', (pid, name))
                db.execute('INSERT INTO project_roots VALUES (?,?,?)', (pid, path, 0))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('temp', self.kkai, 1))
            for row in [('f', '工具', self.old, 'full', 0), ('k', '快递', self.kkai, None, 0),
                        ('a', '旧归档', self.kkai, None, 1), ('m', '已移动', str(self.base / 'lkb'), None, 0),
                        ('u', '未涉及', str(self.base / 'temp'), 'temp', 0)]:
                db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)', (*row, 'body.jsonl'))
        (self.home / 'sessions').mkdir()
        with core.connect(self.db, True) as db, db:
            for row in db.execute('SELECT id,cwd FROM threads').fetchall():
                body = self.home / 'sessions' / (row['id'] + '.jsonl')
                import json
                body.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': row['id'], 'cwd': row['cwd']}}) + ' ' * 64 + '\n' + json.dumps({'type': 'turn_context', 'payload': {'cwd': row['cwd']}}) + '\n')
                db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(body), row['id']))
        self.g = {'local-projects': {pid: {'id': pid, 'name': name, 'rootPaths': [path]} for pid, name, path in
                  [('F', 'fullstack-quality-kit', self.old), ('T', 'temp', str(self.base / 'temp')),
                   ('B', 'cm_bird', str(self.base / 'cm_bird')), ('L', 'lkb', str(self.base / 'lkb'))]},
                  'app-server-project-id-by-legacy-project-id-by-host': {'local': {'F': 'full', 'T': 'temp', 'B': 'bird', 'L': 'lkb'}},
                  'thread-project-assignments': {'f': {'projectId': 'F'}, 'k': {'projectId': 'B'}, 'm': {'projectId': 'L'}},
                  'projectless-thread-ids': ['a'], 'sidebar-project-thread-orders': {},
                  'electron-owner-value:thread-writable-roots:test': {'k': [self.kkai], 'f': [self.old]},
                  'unrelated': {'history': self.kkai}}
        core.save(self.home / migration.GLOBAL, self.g)
        self.real_load = core.load
        def load(path):
            if str(path).endswith('supported-schemas.json'):
                return {'fixture': core.digest(core.snapshot(self.home)['schema'])}
            return self.real_load(path)
        self.patch = patch.object(core, 'load', side_effect=load)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.process = patch.object(core, 'active', return_value=[])
        self.process.start()
        self.addCleanup(self.process.stop)
        self.p = migration.build(self.home, self.base)
        self.run = self.home / 'run'
        self.run.mkdir()
        self.before = core.digest(core.snapshot(self.home))

    def execute(self):
        return migration.execute(self.p, self.run, self.root / 'backups', timeout=0)

    def test_migrate_repeat_and_restore(self):
        self.execute()
        self.assertEqual(self.execute()['status'], '已完成')
        ts = {t['id']: t for t in core.snapshot(self.home)['threads']}
        self.assertEqual(ts['f']['cwd'], str(self.base / 'my_skill/fullstack-quality-kit'))
        self.assertEqual(ts['f']['project_id'], 'full')
        self.assertEqual(ts['k']['project_id'], 'bird')
        self.assertEqual(ts['a']['project_id'], 'temp')
        self.assertEqual(ts['a']['archived'], 1)
        self.assertEqual(core.load(self.home / migration.GLOBAL)['unrelated'], self.g['unrelated'])
        migration.restore(self.p, self.run)
        self.assertEqual(core.digest(core.snapshot(self.home)), self.before)
        self.assertEqual(core.load(self.home / migration.GLOBAL), self.g)

    def test_title_change_allowed_and_preserved(self):
        with core.connect(self.db, True) as db, db:
            db.execute("UPDATE threads SET title='新标题' WHERE id='m'")
        self.execute()
        self.assertEqual(next(t['title'] for t in core.snapshot(self.home)['threads'] if t['id'] == 'm'), '新标题')

    def test_cwd_change_blocks(self):
        with core.connect(self.db, True) as db, db:
            db.execute("UPDATE threads SET cwd=? WHERE id='m'", (str(self.base),))
        with self.assertRaises(core.Blocked):
            self.execute()
        self.assertFalse((self.run / 'recovery.json').exists())

    def test_running_app_blocks_without_backup(self):
        with patch.object(core, 'active', return_value=[999]):
            with self.assertRaises(core.Blocked):
                self.execute()
        self.assertEqual(core.digest(core.snapshot(self.home)), self.before)

    def test_json_failure_rolls_back_db_and_restores_json(self):
        original = migration.atomic_bytes
        calls = []
        def fail_after_replace(*args):
            original(*args)
            calls.append(1)
            if len(calls) == 1:
                raise OSError('模拟替换后故障')
        with patch.object(migration, 'atomic_bytes', side_effect=fail_after_replace):
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual(core.digest(core.snapshot(self.home)), self.before)
        self.assertEqual(core.load(self.home / migration.GLOBAL), self.g)

    def test_relaunch_blocks_restore_and_preserves_recovery(self):
        original = migration.atomic_bytes
        def restart(*args):
            original(*args)
            core.active.return_value = [999]
        with patch.object(migration, 'atomic_bytes', side_effect=restart):
            with self.assertRaises(core.Blocked):
                self.execute()
        self.assertEqual(core.digest(core.snapshot(self.home)), self.before)
        self.assertTrue((self.run / 'recovery.json').exists())
        self.assertEqual(core.load(self.run / 'status.json')['phase'], 'failed')

    def test_restore_refuses_new_data(self):
        self.execute()
        with core.connect(self.db, True) as db, db:
            db.execute("UPDATE threads SET title='外部写入' WHERE id='u'")
        with self.assertRaises(core.Blocked):
            migration.restore(self.p, self.run)

    def test_path_boundary(self):
        self.assertEqual(migration.path_map(self.old + '-other', self.old, 'new'), self.old + '-other')

    def prepare_roots(self):
        self.execute()
        (self.base / 'devops').mkdir()
        with core.connect(self.db, True) as db, db:
            db.execute('INSERT INTO projects VALUES (?,?)', ('dev', 'devops'))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('dev', str(self.base / 'devops'), 0))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('dev', str(self.base / 'cm_bird'), 1))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)', ('full', str(self.base), 1))
            db.execute('CREATE TABLE project_idempotency_keys(key TEXT PRIMARY KEY,project_id TEXT,created_at_ms INTEGER)')
            db.execute('INSERT INTO project_idempotency_keys VALUES (?,?,?)', ('test-create-request', 'dev', 1))
        g = core.load(self.home / migration.GLOBAL)
        g['local-projects']['D'] = {'name': 'devops', 'rootPaths': [str(self.base / 'devops')]}
        g['app-server-project-id-by-legacy-project-id-by-host']['local']['D'] = 'dev'
        core.save(self.home / migration.GLOBAL, g)
        self.p = migration.build(self.home, self.base, True)
        self.run = self.home / 'roots-run'
        self.run.mkdir()

    def test_confirmed_roots_only_repeat_and_restore(self):
        self.prepare_roots()
        before = core.snapshot(self.home)
        original_json = (self.home / migration.GLOBAL).read_bytes()
        with core.connect(self.db) as db:
            original_db = migration.logical_hash(db)
        self.assertEqual(len(self.p['root_removals']), 2)
        self.assertEqual(self.p['changes'], [])
        self.execute()
        self.assertEqual(self.execute()['status'], '已完成')
        after = core.snapshot(self.home)
        self.assertEqual(after['threads'], before['threads'])
        self.assertEqual(after['projects'], before['projects'])
        self.assertEqual(after['project_roots'], [r for r in before['project_roots'] if r not in self.p['root_removals']])
        self.assertEqual((self.home / migration.GLOBAL).read_bytes(), original_json)
        with core.connect(self.db) as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM project_idempotency_keys').fetchone()), ('test-create-request', 'dev', 1))
        migration.restore(self.p, self.run)
        with core.connect(self.db) as db:
            self.assertEqual(migration.logical_hash(db), original_db)

    def test_sidebar_extra_root_blocks_cleanup(self):
        self.prepare_roots()
        g = core.load(self.home / migration.GLOBAL)
        g['local-projects']['D']['rootPaths'].append(str(self.base / 'cm_bird'))
        core.save(self.home / migration.GLOBAL, g)
        with self.assertRaises(core.Blocked):
            migration.build(self.home, self.base, True)

    def test_root_drift_blocks_before_backup(self):
        self.prepare_roots()
        with core.connect(self.db, True) as db, db:
            db.execute('DELETE FROM project_roots WHERE project_id=? AND position=?', ('dev', 1))
        with self.assertRaises(core.Blocked):
            self.execute()
        self.assertFalse((self.run / 'recovery.json').exists())

    def test_already_clean_roots_prepare_empty_plan(self):
        self.prepare_roots()
        self.execute()
        self.assertEqual(migration.build(self.home, self.base, True)['root_removals'], [])

    def test_timestamp_short_entry_and_tamper_protection(self):
        path = migration.store_plan(self.p)
        resolved, checksum = migration.resolve_run(self.home, self.p['id'])
        self.assertEqual(resolved, path.resolve())
        self.assertEqual(checksum, core.digest(self.p))
        changed = copy.deepcopy(self.p)
        changed['scope'] = '改变计划'
        core.save(path, changed)
        with self.assertRaises(core.Blocked):
            migration.resolve_run(self.home, self.p['id'])

    def test_run_path_traversal_rejected(self):
        with self.assertRaises(core.Blocked):
            migration.resolve_run(self.home, '../other')

    def test_same_timestamp_never_overwrites(self):
        path = migration.store_plan(self.p)
        with self.assertRaises(FileExistsError):
            migration.store_plan(self.p)
        self.assertEqual(core.load(path), self.p)

    def test_header_growth_shifts_offsets_and_restore_preserves_history(self):
        body = self.home / 'sessions/f.jsonl'
        first, tail = migration.body_parts(body)
        import json
        header = json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
        body.write_bytes(header + tail)
        history = self.home / 'thread_history_1.sqlite'
        with closing(sqlite3.connect(history)) as db, db:
            db.executescript('''CREATE TABLE thread_turns(thread_id TEXT, rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER);
                CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER);''')
            db.execute('INSERT INTO thread_turns VALUES (?,?,?)', ('f', len(header), len(header) + len(tail)))
            db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)', ('f', len(header) + len(tail)))
        self.p = migration.build(self.home, self.base)
        self.execute()
        after_header = body.read_bytes().split(b'\n', 1)[0] + b'\n'
        with core.connect(history) as db:
            self.assertEqual(db.execute('SELECT rollout_byte_offset FROM thread_turns').fetchone()[0], len(after_header))
        self.assertEqual(migration.body_parts(body)[1], tail)
        migration.restore(self.p, self.run)
        self.assertEqual(body.read_bytes(), header + tail)
        with core.connect(history) as db:
            self.assertEqual(db.execute('SELECT rollout_byte_offset FROM thread_turns').fetchone()[0], len(header))


class BodyDirectoryRepair(unittest.TestCase):
    """数据库已迁移的正文独有差异，使用真实文件和 SQLite 验证恢复。"""

    setUp = MigrationWorkflow.setUp
    execute = MigrationWorkflow.execute

    def prepare_body(self, compressed=False, missing_plain=False, grow=False):
        import json
        self.execute()
        self.run = self.home / 'body-run'
        self.run.mkdir()
        plain = self.home / 'sessions/f.jsonl'
        first, tail = migration.body_parts(plain)
        first['payload']['cwd'] = '/old'
        header = json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
        if not grow:
            header = header[:-1] + b' ' * 256 + b'\n'
        self.original_body = header + tail
        self.body = plain
        if compressed:
            from compression import zstd
            self.body = Path(str(plain) + '.zst')
            self.body.write_bytes(zstd.compress(self.original_body))
            plain.unlink()
            if not missing_plain:
                with core.connect(self.db, True) as db, db:
                    db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(self.body), 'f'))
        else:
            plain.write_bytes(self.original_body)
        if grow:
            with closing(sqlite3.connect(self.home / 'thread_history_1.sqlite')) as db, db:
                db.executescript('CREATE TABLE thread_turns(thread_id TEXT,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER);'
                                 'CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER);')
                db.execute('INSERT INTO thread_turns VALUES (?,?,?)', ('f', len(header), len(header) + len(tail)))
                db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)', ('f', len(header) + len(tail)))
            self.patch.stop()
            def load(path):
                if str(path).endswith('supported-schemas.json'):
                    with core.connect(self.home / 'thread_history_1.sqlite') as db:
                        return {'state': core.digest(core.snapshot(self.home)['schema']), 'history': migration.history_schema(db)}
                return self.real_load(path)
            self.patch = patch.object(core, 'load', side_effect=load)
            self.patch.start()
            self.addCleanup(self.patch.stop)
        self.body_before = self.body.read_bytes()
        self.state_before = core.snapshot(self.home)
        self.global_before = (self.home / migration.GLOBAL).read_bytes()
        self.p = migration.build_body_repair(self.home)

    def test_body_only_repeat_and_exact_restore(self):
        self.prepare_body()
        self.assertEqual([c['id'] for c in self.p['changes']], ['f'])
        self.execute()
        self.assertEqual(core.snapshot(self.home), self.state_before)
        self.assertEqual((self.home / migration.GLOBAL).read_bytes(), self.global_before)
        self.assertEqual(migration.body_parts(self.body)[1], self.original_body.split(b'\n', 1)[1])
        self.assertEqual(self.execute()['status'], '已完成')
        self.assertEqual(migration.build_body_repair(self.home)['changes'], [])
        migration.restore(self.p, self.run)
        self.assertEqual(self.body.read_bytes(), self.body_before)

    def test_compressed_fallback_offset_shift_and_restore(self):
        self.prepare_body(compressed=True, missing_plain=True, grow=True)
        self.assertGreater(self.p['changes'][0]['body_delta'], 0)
        with migration.body_stream(self.body) as f:
            before_header = len(f.readline())
        self.execute()
        first, tail = migration.body_parts(self.body)
        self.assertEqual(first['payload']['cwd'], str(self.base / 'my_skill/fullstack-quality-kit'))
        self.assertEqual(tail, self.original_body.split(b'\n', 1)[1])
        with migration.body_stream(self.body) as f:
            after_header = len(f.readline())
        with core.connect(self.home / 'thread_history_1.sqlite') as db:
            self.assertEqual(db.execute('SELECT rollout_byte_offset FROM thread_turns').fetchone()[0], after_header)
        self.assertEqual(self.execute()['status'], '已完成')
        migration.restore(self.p, self.run)
        self.assertEqual(self.body.read_bytes(), self.body_before)
        with core.connect(self.home / 'thread_history_1.sqlite') as db:
            self.assertEqual(db.execute('SELECT rollout_byte_offset FROM thread_turns').fetchone()[0], before_header)

    def test_compressed_no_offset_change(self):
        self.prepare_body(compressed=True)
        self.assertIsNone(self.p['history'])
        self.execute()
        self.assertEqual(migration.body_parts(self.body)[1], self.original_body.split(b'\n', 1)[1])

    def test_body_drift_blocks_before_backup(self):
        self.prepare_body(compressed=True)
        self.body.write_bytes(self.body_before + b'changed')
        with self.assertRaises(core.Blocked):
            self.execute()
        self.assertFalse((self.run / 'recovery.json').exists())

    def test_partial_body_failure_restores_original(self):
        self.prepare_body(compressed=True)
        original = migration.atomic_bytes
        def fail(path, *args):
            original(path, *args)
            if path == self.body:
                raise OSError('模拟正文替换后故障')
        with patch.object(migration, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual(self.body.read_bytes(), self.body_before)
        self.assertEqual(core.snapshot(self.home), self.state_before)

    def test_ambiguous_assignment_blocks(self):
        self.prepare_body()
        g = core.load(self.home / migration.GLOBAL)
        g['thread-project-assignments']['f'] = {'projectId': 'B'}
        core.save(self.home / migration.GLOBAL, g)
        with self.assertRaises(core.Blocked):
            migration.build_body_repair(self.home)

    def test_assignment_string_is_supported(self):
        self.prepare_body()
        g = core.load(self.home / migration.GLOBAL)
        g['thread-project-assignments']['f'] = 'F'
        core.save(self.home / migration.GLOBAL, g)
        self.assertEqual([c['id'] for c in migration.build_body_repair(self.home)['changes']], ['f'])

    def test_assignment_object_with_kind_is_supported(self):
        self.prepare_body()
        g = core.load(self.home / migration.GLOBAL)
        g['thread-project-assignments']['f'] = {'projectKind': 'local', 'projectId': 'F'}
        core.save(self.home / migration.GLOBAL, g)
        self.assertEqual([c['id'] for c in migration.build_body_repair(self.home)['changes']], ['f'])

    def test_assignment_missing_is_supported(self):
        self.prepare_body()
        g = core.load(self.home / migration.GLOBAL)
        del g['thread-project-assignments']['f']
        core.save(self.home / migration.GLOBAL, g)
        self.assertEqual([c['id'] for c in migration.build_body_repair(self.home)['changes']], ['f'])

    def test_assignment_unknown_shape_blocks(self):
        self.prepare_body()
        g = core.load(self.home / migration.GLOBAL)
        g['thread-project-assignments']['f'] = ['F']
        core.save(self.home / migration.GLOBAL, g)
        with self.assertRaises(core.Blocked):
            migration.build_body_repair(self.home)

    def test_audit_reports_compressed_cwd_difference(self):
        self.prepare_body(compressed=True, missing_plain=True)
        report = core.audit(self.home)
        row = next(t for t in report['threads'] if t['id'] == 'f')
        self.assertTrue(row['body_exists'])
        self.assertEqual(row['body_cwd'], '/old')
        self.assertTrue(any(i.get('id') == 'f' and i.get('issue') == '正文当前目录与数据库不一致' for i in report['issues']))

    def test_app_reset_database_cwd_follows_sidebar(self):
        self.prepare_body()
        with core.connect(self.db, True) as db, db:
            db.execute('UPDATE threads SET cwd=? WHERE id=?', (self.old, 'f'))
        self.p = migration.build_body_repair(self.home)
        self.execute()
        row = next(t for t in core.snapshot(self.home)['threads'] if t['id'] == 'f')
        self.assertEqual(row['cwd'], str(self.base / 'my_skill/fullstack-quality-kit'))
        self.assertEqual(migration.build_body_repair(self.home)['changes'], [])

    def test_body_identity_and_symlink_rejected(self):
        self.prepare_body()
        first, tail = migration.body_parts(self.body)
        import json
        first['payload']['id'] = 'other'
        self.body.write_bytes(json.dumps(first).encode() + b'\n' + tail)
        with self.assertRaises(core.Blocked):
            migration.build_body_repair(self.home)
        target = self.body.with_suffix('.saved')
        self.body.rename(target)
        self.body.symlink_to(target)
        with self.assertRaises(core.Blocked):
            migration.build_body_repair(self.home)

    def test_restore_refuses_external_body_change(self):
        self.prepare_body(compressed=True)
        self.execute()
        self.body.write_bytes(self.body.read_bytes() + b'external')
        with self.assertRaises(core.Blocked):
            migration.restore(self.p, self.run)

    def test_missing_compression_support_blocks(self):
        self.prepare_body(compressed=True)
        with patch.dict(sys.modules, {'compression': None}):
            with self.assertRaises(core.Blocked):
                migration.build_body_repair(self.home)
        self.assertEqual(self.body.read_bytes(), self.body_before)

    def test_offset_history_drift_blocks_before_backup(self):
        self.prepare_body(compressed=True, grow=True)
        with core.connect(self.home / 'thread_history_1.sqlite', True) as db, db:
            db.execute('UPDATE thread_turns SET rollout_end_byte_offset=rollout_end_byte_offset+1')
        with self.assertRaises(core.Blocked):
            self.execute()
        self.assertFalse((self.run / 'recovery.json').exists())
        self.assertEqual(self.body.read_bytes(), self.body_before)


if __name__ == '__main__':
    unittest.main()
