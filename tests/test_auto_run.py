"""自动生命周期使用隔离状态和假 App；不退出真实应用或访问模型。"""
import json
import contextlib
from argparse import Namespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import auto_run as auto
import session_tidy as core
import test_migrate_confirmed as fixtures


def evidence_for(run, result='通过'):
    evidence = {'run_id': run.name, 'quality': 'B', 'data_check': result,
                'app_verification': '待完成'}
    evidence['evidence_sha256'] = core.digest(evidence)
    core.save(run / 'acceptance-evidence.json', evidence)
    return evidence


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.run = self.home / 'runs/test'
        self.run.mkdir(parents=True)
        # 仅替代桌面窗口进程；文件结果、状态流转和重开判断仍执行真实逻辑。
        self.notice = patch.object(auto.subprocess, 'Popen').start()
        self.addCleanup(patch.stopall)
        self.request = {'home': str(self.home), 'base': str(self.home),
                        'start_delay': 0, 'exit_timeout': 1,
                        'thread_id': 'test', 'report_timeout': 1, 'codex': 'codex'}
        self.save_request()
        core.save(self.run / 'launch-ack.json', {})

    def save_request(self):
        core.save(self.run / 'auto-request.json', self.request)
        core.save(self.run / 'auto-checksum.json', {'sha256': core.digest(self.request)})

    def execute(self, report_error=None, stage_error=None, restart_error=None, data_check='通过'):
        apps = [{'path': '/Applications/Codex.app', 'bundle': 'test'}]
        with patch.object(auto, 'running_apps', return_value=apps), \
                patch.object(auto, 'quit_apps'), patch.object(core, 'offline'), \
                patch.object(auto, 'publish_report', side_effect=report_error) as publish, \
                patch.object(auto, 'acceptance_evidence', side_effect=lambda request, run: evidence_for(run, data_check)), \
                patch.object(auto, 'execute_stages', side_effect=stage_error), \
                patch.object(auto, 'reopen_apps', side_effect=restart_error) as reopen:
            auto.worker(self.run)
            return core.load(self.run / 'auto-status.json'), publish, reopen

    def test_success_finishes_with_theoretical_acceptance_and_optional_user_checks(self):
        state, publish, reopen = self.execute()
        self.assertEqual(state['status'], '已完成')
        self.assertEqual(state['acceptance'], '通过（理论验收）')
        self.assertEqual(state['report_delivery'], '已完成')
        self.assertFalse(state['requires_user_action'])
        self.assertEqual(state['app_verification'], '用户自行确认')
        self.assertTrue(state['apps_reopened'])
        self.assertEqual(state['data_verification'], '通过')
        publish.assert_called_once()
        report = publish.call_args.args[2]
        self.assertTrue(report['apps_reopened'])
        self.assertEqual(report['data_report']['data_check'], '通过')
        self.assertEqual(core.load(self.run / 'handoff.json')['status'], '已完成')
        self.assertTrue((self.run / 'handoff.json').exists())
        self.notice.assert_not_called()

    def test_reports_only_after_repair_check_and_reopen(self):
        events = []
        with patch.object(auto, 'running_apps', return_value=[{'path': 'App'}]), \
                patch.object(auto, 'quit_apps', side_effect=lambda *a: events.append('quit')), \
                patch.object(core, 'offline'), \
                patch.object(auto, 'execute_stages', side_effect=lambda *a: events.append('repair')), \
                patch.object(auto, 'acceptance_evidence', side_effect=lambda request, run:
                             (events.append('check'), evidence_for(run))[1]), \
                patch.object(auto, 'reopen_apps', side_effect=lambda *a: events.append('reopen')), \
                patch.object(auto, 'publish_report', side_effect=lambda *a: events.append('report')):
            auto.worker(self.run)
        self.assertEqual(events, ['quit', 'repair', 'check', 'reopen', 'report'])

    def test_data_failure_is_reported_without_claiming_acceptance(self):
        state, publish, reopen = self.execute(data_check='未通过')
        self.assertEqual(state['status'], '阻塞')
        self.assertEqual(state['data_verification'], '未通过')
        self.assertEqual(state['acceptance'], '未通过')
        self.assertEqual(publish.call_args.args[2]['acceptance'], '未通过')
        reopen.assert_called_once()
        self.notice.assert_called_once()

    def test_report_failure_preserves_data_success_and_app_reopened(self):
        state, publish, reopen = self.execute(report_error=core.Blocked('already has an active writer'))
        self.assertEqual(state['status'], '阻塞')
        self.assertIn('active writer', state['report_error'])
        self.assertEqual(state['data_verification'], '通过')
        self.assertTrue(state['apps_reopened'])
        self.notice.assert_called_once()

    def test_report_timeout_prompts_without_repair_retry(self):
        state, publish, reopen = self.execute(report_error=auto.RunTimeout('结果输出超时'))
        self.assertEqual(state['phase'], 'report_failed')
        self.assertEqual(state['data_verification'], '通过')
        self.assertEqual(state['report_delivery'], '未完成')
        self.assertIn('结果输出超时', state['report_error'])
        publish.assert_called_once()
        reopen.assert_called_once()
        self.notice.assert_called_once()

    def test_no_running_app_never_repairs_or_publishes(self):
        with patch.object(auto, 'running_apps', return_value=[]), \
                patch.object(auto, 'execute_stages') as repair, \
                patch.object(auto, 'publish_report') as publish:
            auto.worker(self.run)
        self.assertEqual(core.load(self.run / 'auto-status.json')['status'], '阻塞')
        repair.assert_not_called()
        publish.assert_not_called()
        self.notice.assert_not_called()

    def test_missing_launch_ack_never_quits_apps(self):
        (self.run / 'launch-ack.json').unlink()
        with patch.object(auto, 'quit_apps') as quit_apps:
            auto.worker(self.run)
            quit_apps.assert_not_called()
        self.assertEqual(core.load(self.run / 'auto-status.json')['phase'], 'launch_failed')

    def test_repeat_never_quits_again(self):
        self.execute()
        with patch.object(auto, 'quit_apps') as quit_apps:
            with self.assertRaises(core.Blocked):
                auto.worker(self.run)
            quit_apps.assert_not_called()

    def test_partial_data_failure_reopens_and_keeps_recovery_evidence(self):
        state, publish, reopen = self.execute(stage_error=core.Blocked('模拟数据漂移'))
        self.assertEqual(state['phase'], 'blocked')
        self.assertEqual(publish.call_args.args[2]['acceptance'], '未通过')
        reopen.assert_called_once()
        self.assertTrue((self.run / 'handoff.json').exists())
        self.notice.assert_called_once()

    def test_timeout_reopens_and_reports_task_without_retry(self):
        state, publish, reopen = self.execute(stage_error=auto.RunTimeout('整体时限到达'))
        self.assertEqual(state['phase'], 'timed_out')
        self.assertIn('整体时限', state['reason'])
        self.assertFalse((self.run / 'result.html').exists())
        self.assertIn('任务：test', auto.result_message(self.run, state))
        self.assertEqual(publish.call_args.args[2]['status'], '阻塞')
        reopen.assert_called_once()
        self.notice.assert_called_once()

    def test_restart_failure_preserves_prior_data_error(self):
        state, publish, _ = self.execute(stage_error=core.Blocked('模拟数据漂移'),
                                  restart_error=OSError('重启失败'))
        self.assertEqual(state['phase'], 'restart_failed')
        self.assertEqual(state['reason'], '模拟数据漂移')
        self.assertIn('重启失败', state['restart_error'])
        publish.assert_not_called()

    def test_quit_refused_no_report_or_repair(self):
        with patch.object(auto, 'running_apps', return_value=[{'path': 'Codex'}]), \
                patch.object(auto, 'quit_apps', side_effect=core.Blocked('退出被拒绝')), \
                patch.object(auto, 'publish_report') as publish, \
                patch.object(auto, 'execute_stages') as repair, patch.object(auto, 'reopen_apps'):
            auto.worker(self.run)
            publish.assert_not_called()
            repair.assert_not_called()
        self.assertEqual(core.load(self.run / 'auto-status.json')['status'], '阻塞')

    def test_request_tamper_blocks_before_shutdown(self):
        core.save(self.run / 'auto-checksum.json', {'sha256': 'bad'})
        with self.assertRaises(core.Blocked), patch.object(auto, 'quit_apps') as quit_apps:
            auto.worker(self.run)
        quit_apps.assert_not_called()

    def test_auto_lock_prevents_second_worker(self):
        import fcntl
        with (self.home / 'session-tidy-auto.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(core.Blocked):
                auto.worker(self.run)
        self.assertFalse((self.run / 'worker-ready.json').exists())


class Protocol(unittest.TestCase):
    def test_real_stdio_report_output_and_cleanup(self):
        # 用短生命周期 Python 子进程检验管道协议，不依赖网络或 Codex 账号。
        fake = '''import sys,json
for line in sys.stdin:
 x=json.loads(line);m=x.get('method');i=x.get('id')
 if m=='initialize':r={}
 elif m=='thread/resume':r={'thread':{'id':'test'}}
 elif m=='turn/start':r={'turn':{'id':'turn-1','status':'inProgress'}}
 else:continue
 print(json.dumps({'id':i,'result':r}),flush=True)
 if m=='turn/start':
  print(json.dumps({'method':'item/completed','params':{'threadId':'test','item':{'type':'agentMessage','text':'已完成，理论验收通过。请自行确认界面。'}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':'test','turn':{'id':'turn-1','status':'completed'}}}),flush=True)
'''
        import subprocess
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as td:
            created = []
            def spawn(*args, **kwargs):
                p = real_popen([sys.executable, '-u', '-c', fake], **kwargs)
                created.append(p)
                return p
            with patch.object(auto.subprocess, 'Popen', side_effect=spawn):
                auto.publish_report({'codex': 'fake', 'home': td, 'thread_id': 'test',
                                     'report_timeout': 2}, Path(td),
                                    {'run_id': Path(td).name, 'apps_reopened': True})
            receipt = core.load(Path(td) / 'report-result.json')
            self.assertTrue(receipt['round_completed'])
            self.assertIn('理论验收通过', receipt['reply'])
            self.assertIsNotNone(created[0].poll())

    def test_empty_failed_tool_and_wrong_turn_are_not_success(self):
        success = {'method': 'turn/completed', 'params': {'threadId': 'test',
                   'turn': {'id': 'turn', 'status': 'completed'}}}
        cases = [[success],
                 [{'method': 'turn/completed', 'params': {'threadId': 'test',
                   'turn': {'id': 'turn', 'status': 'failed', 'error': '测试失败'}}}],
                 [{'method': 'item/started', 'params': {'threadId': 'test',
                   'item': {'type': 'commandExecution'}}}],
                 [{'method': 'turn/completed', 'params': {'threadId': 'other',
                   'turn': {'id': 'turn', 'status': 'completed'}}}, core.Blocked('超时')]]
        for events in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as td:
                server = Mock()
                server.call.side_effect = [{}, {'thread': {'id': 'test'}}, {'turn': {'id': 'turn'}}]
                server.next.side_effect = events
                with patch.object(auto, 'AppServer', return_value=server), self.assertRaises(core.Blocked):
                    auto.publish_report({'codex': 'fake', 'home': td, 'thread_id': 'test'}, Path(td),
                                        {'run_id': Path(td).name, 'apps_reopened': True})
                self.assertFalse(core.load(Path(td) / 'report-result.json')['round_completed'])
                server.close.assert_called_once()

    def test_report_tamper_or_missing_success_evidence_blocks_output(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            auto.update(run, 'report_pending', data_verification='通过')
            with self.assertRaisesRegex(core.Blocked, '缺少验收报告'):
                auto.final_report(run)
            evidence = evidence_for(run)
            evidence['data_check'] = '未通过'
            core.save(run / 'acceptance-evidence.json', evidence)
            with self.assertRaisesRegex(core.Blocked, '编号或摘要'):
                auto.final_report(run)

    def test_result_output_uses_report_without_running_tools_or_audit(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            report = {'run_id': run.name, 'apps_reopened': True, 'acceptance': '未通过'}
            server = Mock()
            server.call.side_effect = [{}, {'thread': {'id': 'test'}}, {'turn': {'id': 'turn'}}]
            server.next.side_effect = [
                {'method': 'item/completed', 'params': {'threadId': 'test',
                 'item': {'type': 'agentMessage', 'text': '阻塞：报告未通过。'}}},
                {'method': 'turn/completed', 'params': {'threadId': 'test',
                 'turn': {'id': 'turn', 'status': 'completed'}}}]
            with patch.object(auto, 'AppServer', return_value=server), patch.object(core, 'audit') as audit:
                auto.publish_report({'codex': 'fake', 'home': td, 'thread_id': 'test'}, run, report)
            audit.assert_not_called()
            prompt = server.call.call_args.args[2]['input'][0]['text']
            self.assertIn(json.dumps(report, ensure_ascii=False), prompt)
            self.assertIn('不要调用工具', prompt)
            self.assertNotIn('acceptance-review.json', prompt)

    def test_deadline_and_unexpected_server_request(self):
        server = object.__new__(auto.AppServer)
        with self.assertRaises(core.Blocked):
            server.next(0)
        import queue,time
        server.messages = queue.Queue()
        server.messages.put({'id': 1, 'method': 'approval'})
        with patch.object(server, 'send') as send, self.assertRaises(core.Blocked):
            server.next(time.monotonic() + 1)
        self.assertIn('error', send.call_args.args[0])


class Notice(unittest.TestCase):
    def test_notice_shows_summary_and_only_close_button(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / '20261113-190000-000001'
            run.mkdir()
            core.save(run / 'handoff.json', {'phase': 'blocked', 'status': '阻塞',
                                           'reason': '测试 "引号" <tag> $(touch never)',
                                           'apps_reopened': True})
            with patch.object(auto.subprocess, 'run', return_value=SimpleNamespace(stdout='关闭\n')) as call:
                auto.show_notice(run)
            self.assertIn('任务：' + run.name, call.call_args_list[0].args[0][-1])
            self.assertIn('buttons {"关闭"}', call.call_args_list[0].args[0][2])
            self.assertIn('giving up after 30', call.call_args.args[0][2])
            self.assertEqual(call.call_args.kwargs['timeout'], 35)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(core.load(run / 'notice-result.json')['choice'], '关闭')

    def test_close_timeout_and_window_failure_do_not_open_page(self):
        from types import SimpleNamespace
        for choice in ('关闭', '超时关闭', OSError('窗口不可用')):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as td:
                run = Path(td)
                core.save(run / 'handoff.json', {'phase': 'blocked', 'status': '阻塞'})
                with patch.object(auto.subprocess, 'run',
                                  return_value=SimpleNamespace(stdout=choice),
                                  side_effect=choice if isinstance(choice, Exception) else None) as call:
                    auto.show_notice(run)
                self.assertEqual(call.call_count, 1)
                self.assertTrue((run / 'notice-result.json').exists())

    def test_restart_failure_still_prompts_without_result_page(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            auto.update(run, 'blocked', '阻塞', reason='<script>bad</script> sk-fake')
            with patch.object(auto, 'reopen_apps', side_effect=OSError('无法打开 App')), \
                    patch.object(auto.subprocess, 'Popen') as spawn:
                auto.finish(run, [{'path': '/Codex.app'}], False, app_closed=True)
            state = core.load(run / 'handoff.json')
            self.assertEqual(state['phase'], 'restart_failed')
            self.assertIn('script', state['reason'])
            self.assertFalse((run / 'result.html').exists())
            self.assertIn('阶段：App 重开失败', auto.result_message(run, state))
            spawn.assert_called_once()

    def test_runtime_alarm_raises_and_restores_handler(self):
        import signal,time
        old = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(auto.RunTimeout):
            with auto.runtime_limit(0.01):
                time.sleep(0.05)
        self.assertEqual(signal.getsignal(signal.SIGALRM), old)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_resume_error_and_premature_eof_are_not_success(self):
        import queue
        server = object.__new__(auto.AppServer)
        server.messages = queue.Queue()
        server.messages.put({'id': 2, 'error': {'message': 'already has an active writer'}})
        with patch.object(server, 'send'), self.assertRaisesRegex(core.Blocked, 'active writer'):
            server.call(2, 'thread/resume', {}, 1)
        server.messages.put(None)
        with self.assertRaisesRegex(core.Blocked, '提前退出'):
            import time
            server.next(time.monotonic() + 1)


class Launch(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / '.codex'
        self.home.mkdir()
        with contextlib.closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT)')
            db.execute("INSERT INTO threads VALUES ('test')")
        self.args = Namespace(home=self.home, base=self.root, thread_id='test', max_runtime=120)

    def test_no_changes_returns_without_any_app_or_child_process(self):
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(auto.sys, 'platform', 'darwin'), \
                patch.object(auto.shutil, 'which', return_value='/codex'), \
                patch.object(auto, 'inspect', return_value=({'status': '已完成'}, False)), \
                patch.object(auto.subprocess, 'Popen') as spawn:
            result = auto.launch(self.args)
        self.assertEqual(result['status'], '已完成')
        self.assertFalse(result['requires_user_action'])
        spawn.assert_not_called()
        self.assertTrue((self.home / 'session-tidy/runs' / result['run_id'] / 'auto-audit.json').exists())

    def test_failed_child_start_never_acknowledged_or_quits_apps(self):
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(auto.sys, 'platform', 'darwin'), \
                patch.object(auto.shutil, 'which', return_value='/codex'), \
                patch.object(auto, 'inspect', return_value=({}, True)), \
                patch.object(auto.subprocess, 'Popen') as spawn:
            spawn.return_value.poll.return_value = 2
            with self.assertRaisesRegex(core.Blocked, '启动失败'):
                auto.launch(self.args)
        self.assertFalse(list(self.home.rglob('launch-ack.json')))

    def test_preflight_block_records_timestamp_and_prompts_without_closing(self):
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(auto.sys, 'platform', 'darwin'), \
                patch.object(auto.shutil, 'which', return_value='/codex'), \
                patch.object(auto, 'inspect', side_effect=core.Blocked('项目主目录不明确')), \
                patch.object(auto.subprocess, 'Popen') as spawn, \
                patch.object(auto, 'quit_apps') as quit_apps:
            state = auto.launch(self.args)
        self.assertEqual(state['phase'], 'preflight_blocked')
        run = self.home / 'session-tidy/runs' / state['run_id']
        self.assertFalse((run / 'result.html').exists())
        self.assertTrue((run / 'handoff.json').exists())
        spawn.assert_not_called()
        quit_apps.assert_not_called()

    def test_invalid_runtime_does_not_launch_or_quit(self):
        self.args.max_runtime = 0
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(auto.sys, 'platform', 'darwin'), \
                patch.object(auto.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(core.Blocked, '时限必须大于零'):
                auto.launch(self.args)
        spawn.assert_not_called()

    def test_exit_wait_timeout_never_kills_running_app(self):
        with patch.object(core, 'active', return_value=[123]):
            with self.assertRaisesRegex(core.Blocked, '退出超时'):
                auto.quit_apps([], 0)

    def test_cc_switch_is_ignored_but_codex_writers_still_block(self):
        from types import SimpleNamespace
        output = '100 /Applications/CC Switch.app/Contents/MacOS/cc-switch\n101 /Applications/CC Switch.app/Contents/Resources/codex\n102 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n103 /opt/homebrew/bin/codex\n'
        with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(core.active(), [102, 103])

    def test_exit_timeout_records_remaining_executables(self):
        with patch.object(core, 'active', return_value=[123]), \
                patch.object(core, 'active_processes', return_value=[{'pid': 123, 'executable': 'Codex'}]):
            with self.assertRaises(core.Blocked):
                auto.quit_apps([], 0, self.home)
        self.assertEqual(core.load(self.home / 'remaining-processes.json')[0]['pid'], 123)

    def test_crash_reporters_do_not_block_but_other_helpers_do(self):
        from types import SimpleNamespace
        output = ('110 /Applications/ChatGPT.app/Contents/Frameworks/Helpers/browser_crashpad_handler\n'
                  '111 /Applications/Codex.app/Contents/Helpers/browser_crashpad_handler\n'
                  '112 /Applications/ChatGPT.app/Contents/Helpers/codex\n'
                  '113 /Applications/ChatGPT.app/Contents/Helpers/other_writer\n'
                  '114 /Applications/ChatGPT.app/Contents/Helpers/browser_crashpad_handler-other\n')
        with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(core.active(), [112, 113, 114])

    def test_only_residual_crash_reporter_allows_offline_check(self):
        from types import SimpleNamespace
        output = '110 /Applications/ChatGPT.app/Contents/Helpers/browser_crashpad_handler\n'
        with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
            core.offline()
            auto.quit_apps([], 0)


class IsolatedMigration(unittest.TestCase):
    def test_acceptance_pass_and_unstable_or_unknown_data(self):
        for audit_status, error, expected in (
                ('已完成', None, '通过'), ('阻塞', None, '未通过'),
                ('已完成', core.Blocked('未知结构'), '未通过')):
            with self.subTest(status=audit_status, error=error), tempfile.TemporaryDirectory() as td:
                report = {'status': audit_status}
                with patch.object(core, 'audit', return_value=report), \
                        patch.object(auto, 'prepare_stage', return_value={'changes': []}), \
                        patch.object(auto, 'inspect', return_value=(report, False), side_effect=error):
                    evidence = auto.acceptance_evidence({'home': td, 'base': td}, Path(td))
                self.assertEqual(evidence['data_check'], expected)
                self.assertEqual(evidence['app_verification'], '待完成')

    def test_acceptance_collects_all_stages_without_applying_any_plan(self):
        with tempfile.TemporaryDirectory() as td:
            report = {'status': '已完成', 'threads': [{'archived': 1}], 'projects': [], 'roots': []}
            stages = [{'changes': [{'id': 'one'}]}, core.Blocked('归属有歧义'), {'changes': []}]
            with patch.object(core, 'audit', return_value=report), \
                    patch.object(auto, 'prepare_stage', side_effect=stages) as prepare, \
                    patch.object(auto, 'inspect', return_value=(report, True)), \
                    patch.object(auto.migration, 'execute') as execute:
                result = auto.acceptance_evidence({'home': td, 'base': td}, Path(td))
            self.assertEqual(prepare.call_count, 3)
            execute.assert_not_called()
            self.assertEqual(result['data_check'], '未通过')
            self.assertEqual(result['counts']['archived'], 1)
            self.assertIn('归属有歧义', result['stages']['cleanup'])
            checksum = result.pop('evidence_sha256')
            self.assertEqual(checksum, core.digest(result))

    def test_auto_stage_reuses_real_migration_and_preserves_archive(self):
        # 复用原隔离数据库，真实调用迁移执行器；仅替换后续不匹配该简化结构的阶段。
        fixture = fixtures.MigrationWorkflow('test_migrate_repeat_and_restore')
        fixture.setUp()
        try:
            request = {'home': str(fixture.home), 'base': str(fixture.base)}
            original = auto.prepare_stage
            def prepare(home, base, stage):
                if stage == 'migration':
                    return original(home, base, stage)
                return {'changes': [], 'root_removals': []}
            actual = auto.migration.execute
            def execute(p, run, backup_dir, timeout):
                return actual(p, run, fixture.root / 'backups', timeout=timeout)
            with patch.object(auto, 'prepare_stage', side_effect=prepare), \
                    patch.object(auto.migration, 'execute', side_effect=execute), \
                    patch.object(auto, 'inspect', return_value=({}, False)):
                auto.execute_stages(request, fixture.run)
            plans = core.load(fixture.run / 'auto-status.json')['plans']
            self.assertEqual(len(plans), 1)
            self.assertTrue((fixture.home / 'session-tidy/runs' / plans[0] / 'recovery.json').exists())
            t = {x['id']: x for x in core.snapshot(fixture.home)['threads']}
            self.assertEqual(t['f']['cwd'], str(fixture.base / 'my_skill/fullstack-quality-kit'))
            self.assertEqual(t['a']['archived'], 1)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
