import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('tidy', Path(__file__).parents[1] / 'scripts/session_tidy.py')
tidy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tidy)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'project'
        (self.root / 'child').mkdir(parents=True)
        self.db = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('''CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE project_roots(project_id TEXT,path TEXT);
                CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,project_id TEXT,
                archived INTEGER,rollout_path TEXT);''')
            db.execute('INSERT INTO projects VALUES (?,?)', ('p', '项目'))
            db.execute('INSERT INTO project_roots VALUES (?,?)', ('p', str(self.root)))
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)',
                       ('t', '测试', str(self.root / 'child'), None, 1, str(self.home / 'missing')))
        self.report = tidy.audit(self.home)
        self.plan = tidy.plan(self.report, {'t': 'p'})
        self.run = self.home / 'run'
        self.run.mkdir()
        self.guard = patch.object(tidy, 'active', return_value=[])
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def apply(self):
        return tidy.apply(self.plan, self.run, self.home / 'backups')

    def test_audit_does_not_change_database(self):
        before = self.db.read_bytes()
        tidy.audit(self.home)
        self.assertEqual(before, self.db.read_bytes())

    def test_apply_repeat_restore_preserves_archived_and_cwd(self):
        self.apply()
        self.assertEqual(self.apply()['status'], '已完成')
        self.assertEqual(tidy.verify(self.plan)['status'], '已完成')
        tidy.restore(self.plan, self.run)
        self.assertEqual(tidy.digest(tidy.snapshot(self.home)), self.report['fingerprint'])

    def test_changed_data_blocks(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE threads SET title='改变'")
        with self.assertRaises(tidy.Blocked):
            self.apply()

    def test_running_app_blocks(self):
        with patch.object(tidy, 'active', return_value=[123]):
            with self.assertRaises(tidy.Blocked):
                self.apply()
        self.assertEqual(tidy.digest(tidy.snapshot(self.home)), self.report['fingerprint'])

    def test_relaunch_rolls_back(self):
        with patch.object(tidy, 'active', side_effect=[[], [], [], [123]]):
            with self.assertRaises(tidy.Blocked):
                self.apply()
        self.assertEqual(tidy.digest(tidy.snapshot(self.home)), self.report['fingerprint'])

    def test_restore_refuses_external_changes(self):
        self.apply()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE threads SET title='改变'")
        with self.assertRaises(tidy.Blocked):
            tidy.restore(self.plan, self.run)

    def test_path_boundary_and_alias_ambiguity(self):
        roots = self.report['roots']
        self.assertEqual(tidy.recommend(str(self.root / 'child'), roots), 'p')
        self.assertIsNone(tidy.recommend(str(self.root) + '-other', roots))
        self.assertEqual(tidy.recommend(str(self.root), roots + [{'path': str(self.root), 'project_id': 'q'}]), '歧义')

    def test_unknown_structure(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DROP TABLE project_roots')
        with self.assertRaises(tidy.Blocked):
            tidy.audit(self.home)

    def test_cancel_prevents_write(self):
        tidy.save(self.run / 'cancel.json', {})
        with self.assertRaises(tidy.Blocked):
            self.apply()
        self.assertEqual(tidy.digest(tidy.snapshot(self.home)), self.report['fingerprint'])


if __name__ == '__main__':
    unittest.main()
