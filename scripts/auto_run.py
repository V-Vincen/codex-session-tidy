#!/usr/bin/env python3
"""自动编排：独立进程离线修正、数据复查及可恢复的交接记录。"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import session_tidy as core
import migrate_confirmed as migration
import cleanup_confirmed as cleanup

APPS = {'Codex.app', 'ChatGPT.app'}
DEFAULT_RUNTIME = 120
REPORT_TIMEOUT = 45
NOTICE_TIMEOUT = 30


class RunTimeout(core.Blocked):
    """整体时限到达后走原执行器的异常恢复，再统一反馈并重开 App。"""


@contextlib.contextmanager
def runtime_limit(seconds):
    def expired(signum, frame):
        raise RunTimeout('自动任务超过整体时限（' + str(seconds) + ' 秒），已停止后续步骤')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def result_message(run, state):
    """异常提示窗口摘要；任务时间戳始终来自实际任务目录。"""
    phases = {'blocked': '执行阻塞', 'timed_out': '整体超时', 'complete': '执行完成',
              'restart_failed': 'App 重开失败', 'launch_failed': '启动失败',
              'preflight_blocked': '预检阻塞', 'report_pending': '准备输出报告',
              'reporting': '当前会话输出结果', 'report_failed': '会话输出失败',
              'worker_failed': '执行器异常', 'recovery_review': '等待恢复核对'}
    lines = ['任务：' + run.name, '状态：' + state['status'],
            '阶段：' + phases.get(state['phase'], '执行检查'),
            '原因：' + safe_error(state.get('reason') or state.get('restart_error') or
                                 state.get('report_error') or '自动流程未完成，请检查任务记录'),
            '数据验证：' + state.get('data_verification', '未执行'),
            '结果输出：' + state.get('report_delivery', '未执行'),
            'App：' + ('已重开' if state.get('apps_reopened') else '重开未成功或没有可重开的 App'),
            '下一步：' + state.get('next_action', '让助手读取任务记录并检查原因，不自动重试')]
    return '\n'.join(lines)


def show_notice(run):
    """异常收尾提示；只显示本批摘要和“关闭”按钮。"""
    state = core.load(run / 'handoff.json')
    script = '''on run argv
set chosen to display dialog (item 1 of argv) with title "codex-session-tidy" buttons {"关闭"} default button "关闭" giving up after 30
if gave up of chosen then
return "超时关闭"
end if
return button returned of chosen
end run'''
    try:
        response = subprocess.run(['osascript', '-e', script, result_message(run, state)],
                                  check=True, capture_output=True, text=True, timeout=NOTICE_TIMEOUT + 5)
        choice = response.stdout.strip()
        result = {'choice': choice, 'task': run.name}
        core.save(run / 'notice-result.json', result)
    except Exception as exc:
        core.save(run / 'notice-result.json', {'task': run.name, 'error': safe_error(exc)})


def finish(run, apps, normal, app_closed=False, request=None):
    """写入/恢复调用已返回后统一收尾；异常也按用户约定尝试重开原 App。"""
    try:
        if apps and app_closed:
            reopen_apps(apps)
            state = core.load(run / 'auto-status.json')
            update(run, state['phase'], state['status'], apps_reopened=True)
    except Exception as exc:
        normal = False
        update(run, 'restart_failed', '阻塞', apps_reopened=False,
               restart_error=safe_error(exc), requires_user_action=True,
               next_action='手动打开 App，按任务时间戳检查结果；不自动重试')
    state = core.load(run / 'auto-status.json')
    core.save(run / 'handoff.json', state)
    if request and app_closed and state.get('apps_reopened'):
        try:
            # 离线写入和恢复已全部返回；此后仅追加最终说明，不再重验或修正数据。
            report = final_report(run)
            with runtime_limit(request.get('report_timeout', REPORT_TIMEOUT)):
                publish_report(request, run, report)
            state = core.load(run / 'auto-status.json')
            update(run, 'complete' if normal else report['phase'],
                   '已完成' if normal else report['status'], report_delivery='已完成',
                   requires_user_action=not normal,
                   next_action='任务结束；用户可自行确认侧栏、历史内容、工作目录和归档状态'
                   if normal else report['next_action'])
        except Exception as exc:
            normal = False
            update(run, 'report_failed', '阻塞', report_delivery='未完成',
                   report_error=safe_error(exc), requires_user_action=True,
                   next_action='在当前会话提供任务时间戳读取报告；不要重新执行修正')
        core.save(run / 'handoff.json', core.load(run / 'auto-status.json'))
    try:
        if not app_closed:
            return
        if not normal:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--notice', str(run)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception as exc:
        core.save(run / 'notice-result.json', {'task': run.name, 'error': safe_error(exc)})


def safe_error(exc):
    """记录协议失败原因，但不保存完整配置、正文或凭据。"""
    text = str(exc)[:1500]
    text = re.sub(r'https?://\S+', '[地址已隐藏]', text)
    return re.sub(r'(?i)(bearer\s+|sk-)[\w.\-]+', '[凭据已隐藏]', text)


def update(run, phase, status='正在处理', **fields):
    path = run / 'auto-status.json'
    state = core.load(path) if path.exists() else {}
    state.update(phase=phase, status=status, updated_at=core.run_id(), **fields)
    core.save(path, state)
    with (run / 'auto-events.jsonl').open('a') as out:
        os.chmod(out.name, 0o600)
        out.write(json.dumps({'at': state['updated_at'], 'phase': phase,
                              'status': status}, ensure_ascii=False) + '\n')
        out.flush()
        os.fsync(out.fileno())
    return state


def running_apps():
    """仅记录当前已运行的目标 App，重启时不额外启动其他应用。"""
    output = subprocess.run(['ps', '-axo', 'comm='], check=True,
                            capture_output=True, text=True).stdout
    found = {}
    for line in output.splitlines():
        match = re.match(r'(.+?\.app)/', line.strip())
        if not match:
            continue
        path = Path(match[1])
        if path.name not in APPS:
            continue
        with (path / 'Contents/Info.plist').open('rb') as src:
            bundle = plistlib.load(src)['CFBundleIdentifier']
        found[str(path)] = {'path': str(path), 'bundle': bundle}
    return list(found.values())


def quit_apps(apps, timeout, run=None):
    for app in apps:
        # AppleScript 正常退出；超时或用户拒绝时不杀进程。
        try:
            subprocess.run(['osascript', '-e', 'tell application id ' +
                            json.dumps(app['bundle']) + ' to quit'], check=True,
                           capture_output=True, timeout=min(timeout, 20))
            if run:
                core.save(run / 'quit-result.json', {'app': app['path'], 'request_accepted': True})
        except (subprocess.SubprocessError, OSError) as exc:
            if run:
                core.save(run / 'quit-result.json', {'app': app['path'], 'request_accepted': False,
                          'error': safe_error(exc), 'remaining_processes': core.active_processes()})
            raise
    deadline = time.monotonic() + timeout
    while core.active():
        if time.monotonic() >= deadline and run:
            core.save(run / 'remaining-processes.json', core.active_processes())
        core.require(time.monotonic() < deadline,
                     '等待目标写入进程退出超时；未强制关闭，请退出后重新检查')
        time.sleep(0.5)


def reopen_apps(apps):
    for app in apps:
        subprocess.run(['open', '-a', app['path']], check=True,
                       capture_output=True, timeout=20)


def plan_needed(plan):
    return bool(plan['changes'] or plan.get('root_removals') or
                (plan.get('kind') == cleanup.KIND and
                 (any(plan['cleanup']['projects'].values()) or plan['cleanup']['history_ids']
                  or plan['cleanup']['sidebar'] or plan['cleanup']['hints'])))


def prepare_stage(home, base, stage):
    if stage == 'migration':
        return migration.build(home, base)
    if stage == 'body':
        return migration.build_body_repair(home)
    return cleanup.build(home, base)


def inspect(home, base):
    """预检所有可执行阶段；后续阶段在前阶段完成后重新规划，避免复用过期计划。"""
    report = core.audit(home)
    core.require(report['status'] == '已完成', '检查期间数据变化，请重新检查')
    for issue in report['issues']:
        if issue.get('object') == 'session_index.jsonl':
            core.require(not issue.get('missing_ids') and not issue.get('orphan_ids'),
                         '存在索引差异，需由助手核实后再自动执行')
        core.require(issue.get('issue') in (None, '重复正文', '正文当前目录与数据库不一致'),
                     '存在未适配的正文或配置问题，需由助手核实')
    # 普通+压缩两份正文按既有规则保留，不因重复自动清理。
    first = prepare_stage(home, base, 'migration')
    if plan_needed(first):
        return report, True
    # 先移除已确认的额外挂载，再让正文目录适配器要求唯一主目录。
    clean = prepare_stage(home, base, 'cleanup')
    if plan_needed(clean):
        return report, True
    return report, plan_needed(prepare_stage(home, base, 'body'))


class AppServer:
    """持续排空 stdout，防止历史或通知填满管道；不持久化消息正文。"""
    def __init__(self, executable, home):
        env = {**os.environ, 'CODEX_HOME': str(home)}
        self.process = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True,
                                        start_new_session=True, env=env)
        self.messages = queue.Queue(maxsize=2048)
        self.turn_id = None
        self.thread_id = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def next(self, deadline):
        core.require(time.monotonic() < deadline, '程序接口等待超时')
        try:
            item = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise core.Blocked('程序接口等待超时') from None
        core.require(item is not None, '程序接口进程提前退出')
        if 'method' in item and 'id' in item:
            self.send({'id': item['id'], 'error': {'code': -32601,
                       'message': '结果输出不接受工具或审批请求'}})
            raise core.Blocked('结果输出出现非预期服务请求，已停止')
        return item

    def call(self, number, method, params, timeout):
        self.send({'id': number, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            item = self.next(deadline)
            if item.get('id') == number:
                core.require('error' not in item, method + '：' +
                             safe_error(item.get('error', {}).get('message', '协议错误')))
                return item['result']

    def close(self):
        if self.turn_id and self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.send({'id': 99, 'method': 'turn/interrupt',
                           'params': {'threadId': self.thread_id, 'turnId': self.turn_id}})
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # 仅回收本脚本创建的独立服务组，绝不强杀桌面 App。
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process.stdout.close()


def acceptance_evidence(request, run):
    """只读重新检查各阶段；某阶段有歧义仍收集其他阶段，完整记录后再汇总。"""
    home, base = Path(request['home']), Path(request['base'])
    report = core.audit(home)
    core.save(run / 'acceptance-audit.json', report)
    stages, findings = {}, {}
    for stage in ('migration', 'cleanup', 'body'):
        try:
            plan = prepare_stage(home, base, stage)
            stages[stage] = '存在待处理差异' if plan_needed(plan) else '通过'
            findings[stage] = {key: plan.get(key, []) for key in ('changes', 'root_removals')}
            if stage == 'cleanup':
                findings[stage]['cleanup'] = plan.get('cleanup', {})
        except RunTimeout:
            raise
        except Exception as exc:
            stages[stage] = '阻塞：' + safe_error(exc)
    # inspect 复用现有索引、配置及正文保护，不能把正常重复索引当成失败。
    try:
        _, needed = inspect(home, base)
        preflight = '存在待处理差异' if needed else '通过'
    except RunTimeout:
        raise
    except Exception as exc:
        preflight = '阻塞：' + safe_error(exc)
    core.save(run / 'acceptance-findings.json', findings)
    evidence = {'run_id': run.name, 'quality': 'B',
                'scope': '本机脚本只读复核；不包含 App 界面及继续会话实际目录',
                'counts': {'threads': len(report.get('threads', [])),
                           'archived': sum(bool(t['archived']) for t in report.get('threads', [])),
                           'projects': len(report.get('projects', [])),
                           'roots': len(report.get('roots', []))},
                'audit_status': report['status'], 'stages': stages, 'preflight': preflight,
                'data_check': '通过' if report['status'] == '已完成' and
                preflight == '通过' and all(x == '通过' for x in stages.values()) else '未通过',
                'app_verification': '待完成'}
    evidence['evidence_sha256'] = core.digest(evidence)
    core.save(run / 'acceptance-evidence.json', evidence)
    return evidence


def final_report(run):
    """只读取落盘报告；数据结论来自原检查，模型不承担二次验收。"""
    state = core.load(run / 'auto-status.json')
    report = {key: state[key] for key in ('phase', 'status', 'data_verification',
              'acceptance', 'app_verification', 'apps_reopened', 'next_action') if key in state}
    report.update(run_id=run.name, quality='B',
                  user_checks=['侧栏归属和挂载', '历史内容', '继续会话工作目录', '归档状态'])
    for key in ('reason', 'restart_error'):
        if state.get(key):
            report[key] = safe_error(state[key])
    path = run / 'acceptance-evidence.json'
    if path.exists():
        evidence = core.load(path)
        checksum = evidence.pop('evidence_sha256', None)
        core.require(checksum == core.digest(evidence) and evidence['run_id'] == run.name,
                     '验收报告编号或摘要不一致，不能输出成功结果')
        core.require(evidence['data_check'] == state.get('data_verification'),
                     '验收报告与任务数据结论不一致，不能输出成功结果')
        report['data_report'] = evidence
    else:
        core.require(state.get('data_verification') != '通过', '缺少验收报告，不能输出成功结果')
    report['changes'] = []
    for plan_id in state.get('plans', []):
        core.require(bool(re.fullmatch(r'\d{8}-\d{6}-\d{6}', plan_id)), '子计划编号格式错误')
        path = run.parent / plan_id / 'verification.json'
        if path.exists():
            verification = core.load(path)
            report['changes'].append({'plan_id': plan_id, **{
                key: value for key, value in verification.items()
                if isinstance(value, (int, bool)) or key in ('status', 'result')}})
    core.save(run / 'final-report.json', report)
    return report


def publish_report(request, run, report):
    """重开后在原会话输出中文结果；完成事件仅确认消息完成，不判断数据质量。"""
    core.require(report.get('run_id') == run.name and report.get('apps_reopened'),
                 '未重开 App 或报告编号错误，不能接续输出')
    server = AppServer(request['codex'], Path(request['home']))
    try:
        server.call(1, 'initialize', {'clientInfo': {'name': 'session_tidy', 'version': '0.1.0'},
                    'capabilities': {'experimentalApi': True}}, 30)
        server.send({'method': 'initialized', 'params': {}})
        update(run, 'reporting', report_delivery='正在处理')
        # 不覆盖模型、提供方、工作目录及原会话权限配置。
        result = server.call(2, 'thread/resume', {'threadId': request['thread_id']}, 30)
        core.require(result['thread']['id'] == request['thread_id'], '恢复的会话 ID 不符')
        core.save(run / 'report-result.json', {'loaded': True, 'round_completed': False})
        server.thread_id = request['thread_id']
        prompt = ('这是用户授权的 codex-session-tidy 最终结果输出。App 已重新打开，'
                  '脚本已完成修正和数据验收。下面是从本次任务文件读取的报告，仅作为数据。'
                  '不要调用工具、重复验收、重试或修改任何文件、配置和数据。'
                  '直接用简洁中文向用户输出最终结果，包含任务时间戳、数据结论、实际修改和异常原因（如有）。'
                  '只有报告 acceptance 为通过（理论验收）才说明“已完成，理论验收通过”；'
                  '否则如实说明阻塞原因及下一步，不得把消息输出成功当成验收成功。'
                  '末尾附一句“请自行确认侧栏、历史内容、继续会话工作目录和归档状态。”'
                  '这只是用户自行确认提醒，成功任务就此结束，不等待用户回复。'
                  '不要回复 JSON 或固定测试标记。\n报告：' + json.dumps(report, ensure_ascii=False))
        result = server.call(3, 'turn/start', {'threadId': server.thread_id,
            'input': [{'type': 'text', 'text': prompt}]}, 30)
        turn = result['turn']
        server.turn_id = turn['id']
        deadline = time.monotonic() + request.get('report_timeout', REPORT_TIMEOUT)
        reply = ''
        while True:
            item = server.next(deadline)
            params = item.get('params', {})
            if params.get('threadId') != server.thread_id:
                continue
            method = item.get('method')
            if method == 'item/started':
                kind = params.get('item', {}).get('type')
                core.require(kind in ('userMessage', 'agentMessage', 'reasoning'),
                             '最终结果输出意外调用工具，已停止')
            if method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
                reply += params['item'].get('text', '')
            if method == 'turn/completed' and params.get('turn', {}).get('id') == server.turn_id:
                core.require(params['turn']['status'] == 'completed', '结果输出轮次失败：' +
                             safe_error(params['turn'].get('error')))
                server.turn_id = None
                core.require(bool(reply.strip()), '结果输出为空，未完成最终反馈')
                core.save(run / 'report-result.json', {'loaded': True, 'round_completed': True,
                          'mode': 'final_report', 'thread_id': server.thread_id,
                          'turn_id': turn['id'], 'reply': safe_error(reply.strip())})
                return
    finally:
        server.close()


def execute_stages(request, run):
    for stage in ('migration', 'cleanup', 'body'):
        update(run, 'prepare_' + stage)
        core.offline()
        p = prepare_stage(Path(request['home']), Path(request['base']), stage)
        if not plan_needed(p):
            continue
        path = migration.store_plan(p)
        state = core.load(run / 'auto-status.json')
        update(run, 'apply_' + stage, plans=state.get('plans', []) + [p['id']])
        migration.execute(p, path.parent, Path.home() / '.codex/backups', timeout=0)
        migration.validate(p)
    _, needed = inspect(Path(request['home']), Path(request['base']))
    core.require(not needed, '执行后仍有规则内差异，停止并交由助手检查')
    update(run, 'data_verified', data_verification='通过')


def worker(run):
    run = run.resolve()
    request = core.load(run / 'auto-request.json')
    core.require(core.digest(request) == core.load(run / 'auto-checksum.json')['sha256'], '自动任务参数变化')
    home = Path(request['home'])
    with (home / 'session-tidy-auto.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise core.Blocked('已有自动整理任务，禁止并发执行') from None
        core.require(not (run / 'worker-ready.json').exists(), '自动任务不能重复执行；先查看原记录')
        update(run, 'scheduled', worker_pid=os.getpid(), plans=[])
        core.save(run / 'worker-ready.json', {'pid': os.getpid()})
        time.sleep(request['start_delay'])
        # 父进程必须收到启动回执；启动握手失败时不得关闭 App。
        if not (run / 'launch-ack.json').exists():
            update(run, 'launch_failed', '阻塞', reason='启动未确认，未退出 App',
                   requires_user_action=True, next_action='检查启动记录后创建新任务')
            finish(run, [], False, False)
            return
        apps = []
        normal = False
        app_closed = False
        timer = contextlib.ExitStack()
        try:
            timer.enter_context(runtime_limit(request.get('max_runtime', DEFAULT_RUNTIME)))
            apps = running_apps()
            core.save(run / 'apps.json', apps)
            update(run, 'quitting_apps')
            core.require(bool(apps), '未找到运行中的目标 App，未执行修正')
            quit_apps(apps, request['exit_timeout'], run)
            app_closed = True
            execute_stages(request, run)
            core.offline()
            update(run, 'acceptance_check')
            evidence = acceptance_evidence(request, run)
            data_ok = evidence['data_check'] == '通过'
            update(run, 'report_pending', '正在处理' if data_ok else '阻塞',
                   data_verification=evidence['data_check'],
                   acceptance='通过（理论验收）' if data_ok else '未通过',
                   app_verification='用户自行确认', report_delivery='未执行',
                   requires_user_action=not data_ok,
                   reason=None if data_ok else '数据报告存在差异，请查看验收报告的 stages 和 preflight',
                   next_action='重开 App 后自动输出报告，不再重复验收')
            normal = data_ok
        except Exception as exc:
            update(run, 'timed_out' if isinstance(exc, RunTimeout) else 'blocked', '阻塞',
                   reason=safe_error(exc), requires_user_action=True,
                   acceptance='未通过',
                   next_action='按任务时间戳检查报告和子计划恢复记录；不自动重试')
        finally:
            timer.close()
            finish(run, apps, normal, app_closed, request)


def launch(args):
    core.require(sys.platform == 'darwin', '自动退出及重启仅支持 macOS')
    home = args.home.resolve()
    core.require(home == (Path.home() / '.codex').resolve(), '自动生命周期入口仅支持本机默认数据目录')
    core.require(bool(args.thread_id), '缺少当前会话 ID，由助手传 --thread-id 或 CODEX_THREAD_ID')
    core.require(args.max_runtime > 0, '整体时限必须大于零')
    with core.connect(home / 'state_5.sqlite') as db:
        core.require(db.execute('SELECT 1 FROM threads WHERE id=?', (args.thread_id,)).fetchone(), '目标会话不存在')
    executable = shutil.which('codex')
    core.require(executable is not None, '找不到 codex 程序，未启动自动任务')
    base = args.base.resolve() if args.base else Path(__file__).resolve().parents[2].parent
    run = home / 'session-tidy/runs' / core.run_id()
    run.mkdir(parents=True, mode=0o700)
    try:
        report, needed = inspect(home, base)
    except core.Blocked as exc:
        state = update(run, 'preflight_blocked', '阻塞', run_id=run.name,
                       reason=safe_error(exc), requires_user_action=True,
                       next_action='App 未退出，按任务时间戳检查原因；不自动重试')
        finish(run, [], False, False)
        return state
    core.save(run / 'auto-audit.json', report)
    if not needed:
        return update(run, 'complete', '已完成', result='未发现需要修复的问题，无需退出 App',
                      run_id=run.name, requires_user_action=False)
    request = {'home': str(home), 'base': str(base), 'thread_id': args.thread_id,
               'codex': executable, 'start_delay': 30, 'exit_timeout': 30,
               'report_timeout': REPORT_TIMEOUT,
               'max_runtime': args.max_runtime}
    core.save(run / 'auto-request.json', request)
    core.save(run / 'auto-checksum.json', {'sha256': core.digest(request)})
    update(run, 'launching', run_id=run.name, data_verification='未执行',
           app_verification='用户自行确认', report_delivery='未执行')
    try:
        # 脱离当前会话进程组和所有标准流，App 退出不终止任务。
        with (run / 'worker.log').open('x') as log:
            os.chmod(log.name, 0o600)
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(run)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 10
        while not (run / 'worker-ready.json').exists():
            core.require(child.poll() is None and time.monotonic() < deadline,
                         '独立执行器启动失败；未退出 App，检查 ' + str(run / 'worker.log'))
            time.sleep(0.1)
        core.save(run / 'launch-ack.json', {'pid': child.pid})
    except Exception as exc:
        update(run, 'launch_failed', '阻塞', reason=safe_error(exc), run_id=run.name,
               requires_user_action=True, next_action='App 未退出，按任务时间戳检查启动记录')
        finish(run, [], False)
        raise
    return {'status': '正在处理', 'phase': 'scheduled', 'run_id': run.name,
            'record': str(run), 'result': '独立执行器已就绪，30 秒后正常退出目标 App；失败记录留在任务目录'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--base', type=Path)
    parser.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--max-runtime', type=int, default=DEFAULT_RUNTIME,
                        help='退出及离线处理时限，默认 120 秒；重开后结果输出另限 45 秒')
    parser.add_argument('--status', help='只查看指定自动任务，不继续执行')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--notice', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.notice:
            show_notice(args.notice.resolve())
        elif args.worker:
            worker(args.worker)
        elif args.status:
            core.require(bool(re.fullmatch(r'\d{8}-\d{6}-\d{6}', args.status)), '任务编号格式错误')
            print(json.dumps(core.load(args.home / 'session-tidy/runs' / args.status / 'auto-status.json'), ensure_ascii=False))
        else:
            print(json.dumps(launch(args), ensure_ascii=False))
    except Exception as exc:
        if args.worker:
            update(args.worker, 'worker_failed', '阻塞', reason=safe_error(exc),
                   requires_user_action=True, next_action='读取任务记录后检查原因；不自动重试')
        print('阻塞：' + safe_error(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
