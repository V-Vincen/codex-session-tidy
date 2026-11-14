#!/usr/bin/env python3
"""保守的本地会话归属修复器；不修改历史正文或推断删除。"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
import tomllib
from zoneinfo import ZoneInfo


def run_id():
    return datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-%f')


class Blocked(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Blocked(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x') as out:
            os.chmod(tmp, 0o600)
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def connect(path, write=False):
    require(Path(path).is_file(), '数据库不存在')
    db = sqlite3.connect(Path(path).as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        yield db
    finally:
        db.close()


def snapshot(home):
    """完整逻辑快照包含未知字段和结构，避免计划生成后的静默漂移。"""
    with connect(home / 'state_5.sqlite') as db:
        db.execute('BEGIN')
        schema = [tuple(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        tables = {r[1] for r in schema if r[0] == 'table'}
        require({'threads', 'projects', 'project_roots'} <= tables, '未知数据库结构，需新增适配器')
        data = {}
        for name in ('threads', 'projects', 'project_roots'):
            data[name] = [dict(r) for r in db.execute('SELECT * FROM "' + name + '"')]
            data[name].sort(key=lambda r: json.dumps(r, sort_keys=True))
        columns = {n: {r['name'] for r in db.execute('PRAGMA table_info("' + n + '")')} for n in data}
        require({'id', 'cwd', 'project_id', 'archived', 'rollout_path', 'title'} <= columns['threads'], '未知会话结构')
        require({'id', 'name'} <= columns['projects'] and {'project_id', 'path'} <= columns['project_roots'], '未知项目结构')
        return {'schema': schema, **data}


def evidence(home):
    """只保存文件摘要；不把配置内容、消息正文写进报告。"""
    result = {}
    for name in ('.codex-global-state.json', 'session_index.jsonl', 'config.toml'):
        p = home / name
        result[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return result


def recommend(cwd, roots):
    path = Path(cwd).resolve()
    matches = [(len(Path(r['path']).resolve().parts), r['project_id']) for r in roots
               if Path(r['path']).is_dir() and path.is_relative_to(Path(r['path']).resolve())]
    if not matches:
        return None
    best = max(n for n, _ in matches)
    ids = {pid for n, pid in matches if n == best}
    return next(iter(ids)) if len(ids) == 1 else '歧义'


def body_stream(path):
    """统一读取普通及 zstd 正文，供审计与修复复用。"""
    if str(path).endswith('.jsonl.zst'):
        try:
            from compression import zstd
        except ImportError:
            raise Blocked('压缩正文检查或修复需要 Python 3.14+ 的 compression.zstd')
        return zstd.open(path, 'rb')
    require(str(path).endswith('.jsonl'), '未知正文格式')
    return Path(path).open('rb')


def audit(home):
    before = evidence(home)
    state = snapshot(home)
    issues = []
    for name in before:
        p = home / name
        if not p.exists():
            issues.append({'object': name, 'issue': '缺失'})
            continue
        try:
            if name.endswith('.toml'):
                config = tomllib.loads(p.read_text())
                for root in config.get('projects', {}):
                    if not Path(root).is_dir():
                        issues.append({'object': name, 'path': root, 'issue': '信任路径无效'})
            elif name.endswith('.jsonl'):
                entries = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
                ids = [e.get('id') for e in entries]
                known = {t['id'] for t in state['threads']}
                issues.append({'object': name, 'missing_ids': sorted(known - set(ids)),
                               'orphan_ids': sorted(str(x) for x in set(ids) - known),
                               'duplicate_ids': sorted(str(x) for x in set(ids) if ids.count(x) > 1)})
            else:
                require(isinstance(load(p), dict), '全局状态不是对象')
        except (ValueError, Blocked) as exc:
            issues.append({'object': name, 'issue': '解析失败', 'error_type': type(exc).__name__})
    bodies = {}
    for folder in ('sessions', 'archived_sessions'):
        for p in (home / folder).rglob('*'):
            if not p.is_file():
                continue
            if p.suffix == '.gz':
                issues.append({'object': str(p), 'issue': '不支持的压缩正文格式'})
            elif p.suffix == '.jsonl' or p.name.endswith('.jsonl.zst'):
                try:
                    require(not p.is_symlink(), '正文为软链接')
                    with body_stream(p) as stream:
                        entry = json.loads(next(stream))
                    if entry.get('type') == 'session_meta':
                        bodies.setdefault(entry['payload']['id'], []).append(str(p))
                except (ValueError, KeyError, StopIteration, UnicodeError, OSError, Blocked):
                    issues.append({'object': str(p), 'issue': '正文元信息无法识别'})
    for tid, files in bodies.items():
        if len(files) > 1:
            issues.append({'id': tid, 'issue': '重复正文', 'files': files})
    history = home / 'thread_history_1.sqlite'
    if history.exists():
        with connect(history) as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                name = row[0]
                if name in ('thread_turns', 'thread_items', 'thread_realtime_items', 'thread_history_projection_state'):
                    ids = {r[0] for r in db.execute('SELECT DISTINCT thread_id FROM "' + name + '"')}
                    issues.append({'object': name, 'orphan_ids': sorted(ids - {t['id'] for t in state['threads']})})
    threads = []
    for t in state['threads']:
        threads.append({k: t[k] for k in ('id', 'title', 'cwd', 'project_id', 'archived', 'rollout_path')})
        body = Path(t['rollout_path'])
        if not body.exists():
            body = Path(str(body) + '.zst')
        threads[-1].update(suggested_project=recommend(t['cwd'], state['project_roots']),
                           cwd_exists=Path(t['cwd']).is_dir(), body_exists=body.is_file())
        try:
            require(not body.is_symlink(), '正文为软链接')
            with body_stream(body) as stream:
                meta = json.loads(stream.readline())
            require(meta.get('type') == 'session_meta' and meta.get('payload', {}).get('id') == t['id'], '正文身份不一致')
            threads[-1]['body_cwd'] = meta['payload'].get('cwd')
            if meta['payload'].get('cwd') != t['cwd']:
                issues.append({'id': t['id'], 'issue': '正文当前目录与数据库不一致',
                               'body_cwd': meta['payload'].get('cwd'), 'cwd': t['cwd'], 'path': str(body)})
        except (ValueError, KeyError, UnicodeError, OSError, Blocked):
            issues.append({'id': t['id'], 'issue': '正文身份或当前目录无法核验'})
    stable = before == evidence(home) and digest(state) == digest(snapshot(home))
    return {'id': run_id(), 'home': str(home), 'status': '已完成' if stable else '阻塞',
            'reason': None if stable else '状态发生变化，重新审计', 'quality': 'B',
            'verification': '直接读取；App 内验收待完成', 'fingerprint': digest(state),
            'schema': digest(state['schema']), 'files': before, 'threads': threads,
            'projects': state['projects'], 'roots': state['project_roots'], 'issues': issues}


def plan(report, targets):
    require(report['status'] == '已完成', '审计不稳定，重新审计')
    require(isinstance(targets, dict) and targets, '目标必须是非空的会话 ID → 项目 ID 对象')
    threads = {t['id']: t for t in report['threads']}
    projects = {p['id'] for p in report['projects']}
    changes = []
    for tid, pid in targets.items():
        require(tid in threads and pid in projects, '会话或目标项目不存在')
        roots = [r for r in report['roots'] if r['project_id'] == pid and Path(r['path']).is_dir()]
        require(roots, '目标项目没有有效根目录')
        require(all(not any(Path(r['path']).resolve() == Path(x['path']).resolve() and x['project_id'] != pid
                            for x in report['roots']) for r in roots), '项目根目录存在歧义')
        if threads[tid]['project_id'] != pid:
            changes.append({'id': tid, 'old': threads[tid]['project_id'], 'new': pid,
                            'reason': '用户明确指定项目归属', 'file': 'state_5.sqlite'})
    return {'id': run_id(), 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'home': report['home'], 'report_id': report['id'], 'fingerprint': report['fingerprint'],
            'schema': report['schema'], 'files': report['files'], 'changes': changes, 'deletions': [],
            'preserve': {t['id']: {'cwd': t['cwd'], 'archived': t['archived']} for t in report['threads']},
            'preconditions': ['确认本计划摘要', '相关 App 和写入进程退出', '结构与来源数据未变化']}


def active_processes():
    """检查可执行程序路径，不扫描命令参数以免误报本工具或暴露正文。"""
    output = subprocess.run(['ps', '-axo', 'pid=,comm='], check=True, capture_output=True, text=True).stdout
    names = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or int(parts[0]) == os.getpid():
            continue
        executable = parts[1].lower()
        # CC Switch 按用户约定不参与退出、等待或重启，包括其目录内的进程。
        if any(s in executable for s in ('cc switch.app/', 'cc-switch.app/')) or Path(executable).name == 'cc-switch':
            continue
        # 实机退出后会残留崩溃报告进程；只排除已观察到的准确名称和 App 内路径。
        # 不按 Helpers 目录整体排除，Codex 服务及其他辅助写入器仍须退出。
        if (Path(executable).name == 'browser_crashpad_handler'
                and any(s in executable for s in ('codex.app/', 'chatgpt.app/'))):
            continue
        if any(s in executable for s in ('codex.app/', 'chatgpt.app/')) or Path(executable).name == 'codex':
            names.append({'pid': int(parts[0]), 'executable': parts[1]})
    return names


def active():
    return [p['pid'] for p in active_processes()]


def offline():
    require(not active(), '相关 App 或写入进程运行中；退出后再执行')


@contextlib.contextmanager
def locked(home):
    with (home / 'session-tidy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('同一数据目录已有任务') from None
        yield


def check_plan(p):
    profiles = load(Path(__file__).resolve().parents[1] / 'references/supported-schemas.json')
    require(p['schema'] in profiles.values(), '未知数据库结构版本，需审查并新增适配器')
    require(not p['deletions'], '当前适配器不执行删除')
    require(len({c['id'] for c in p['changes']}) == len(p['changes']), '重复修改项')
    require(all(c['file'] == 'state_5.sqlite' for c in p['changes']), '不支持的写入对象')


def verify(p):
    check_plan(p)
    state = snapshot(Path(p['home']))
    threads = {t['id']: t for t in state['threads']}
    require(digest(state['schema']) == p['schema'], '结构发生变化')
    for tid, old in p['preserve'].items():
        require(tid in threads and all(threads[tid][k] == v for k, v in old.items()), '目录或归档状态发生变化')
    for c in p['changes']:
        require(threads[c['id']]['project_id'] == c['new'], '项目归属验证失败')
    with connect(Path(p['home']) / 'state_5.sqlite') as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', '数据库完整性失败')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), '数据库关联检查失败')
    return {'status': '已完成', 'result': '离线数据验证通过，App 内验收待完成'}


def apply(p, run, backup_dir, wait=False, timeout=600):
    check_plan(p)
    home = Path(p['home'])
    with locked(home):
        status_file = run / 'status.json'
        if status_file.exists() and load(status_file)['phase'] == 'complete':
            return verify(p)
        require(not status_file.exists() or load(status_file)['phase'] in ('waiting', 'cancelled', 'blocked'), '上次执行未完成；先核对现场或恢复')
        save(status_file, {'status': '正在处理', 'phase': 'waiting'})
        start = time.monotonic()
        while active():
            if (run / 'cancel.json').exists():
                save(status_file, {'status': '已完成', 'phase': 'cancelled'})
                return {'status': '已完成', 'result': '已取消，源数据未写入'}
            require(wait and time.monotonic() - start < timeout, '等待退出超时或未开启等待；源数据未写入')
            time.sleep(0.2)
        require(not (run / 'cancel.json').exists(), '任务已取消；重新生成计划')
        state = snapshot(home)
        require(digest(state) == p['fingerprint'] and evidence(home) == p['files'], '来源数据变化，重新生成计划')
        projects = {x['id'] for x in state['projects']}
        require(all(c['new'] in projects for c in p['changes']), '目标项目不存在')
        expected = json.loads(json.dumps(state))
        for c in p['changes']:
            row = next(t for t in expected['threads'] if t['id'] == c['id'])
            require(row['project_id'] == c['old'], '原值不符')
            row['project_id'] = c['new']
        expected['threads'].sort(key=lambda r: json.dumps(r, sort_keys=True))
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = backup_dir / ('session-tidy--state_5.sqlite--' + stamp + '-before-' + p['id'] + '.bak')
        require(not backup.exists(), '备份已存在')
        offline()
        with connect(home / 'state_5.sqlite') as source:
            with contextlib.closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        os.chmod(backup, 0o600)
        manifest = {'backup': str(backup), 'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
                    'before': p['fingerprint'], 'after': digest(expected), 'plan': p['id']}
        save(run / 'recovery.json', manifest)
        save(status_file, {'status': '正在处理', 'phase': 'writing'})
        try:
            with connect(home / 'state_5.sqlite', True) as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    for c in p['changes']:
                        offline()
                        require(not (run / 'cancel.json').exists(), '取消请求：事务回滚')
                        cur = db.execute('UPDATE threads SET project_id=? WHERE id=? AND project_id IS ?', (c['new'], c['id'], c['old']))
                        require(cur.rowcount == 1, '修改行数不符')
                    offline()
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            result = verify(p)
            save(run / 'verification.json', result)
            save(status_file, {'status': '已完成', 'phase': 'complete', 'completed_ids': [c['id'] for c in p['changes']]})
            return result
        except BaseException:
            save(status_file, {'status': '阻塞', 'phase': 'failed', 'reason': '执行失败；退出 App 后核对 recovery.json 并恢复'})
            raise


def restore(p, run):
    home = Path(p['home'])
    with locked(home):
        offline()
        manifest = load(run / 'recovery.json')
        require(manifest['plan'] == p['id'], '恢复记录不匹配')
        require(digest(snapshot(home)) in (manifest['before'], manifest['after']), '数据已被其他进程修改，保留现场')
        backup = Path(manifest['backup'])
        require(hashlib.sha256(backup.read_bytes()).hexdigest() == manifest['backup_sha256'], '备份校验失败')
        offline()
        with connect(backup) as source, connect(home / 'state_5.sqlite', True) as target:
            source.backup(target)
        require(digest(snapshot(home)) == manifest['before'], '恢复验证失败')
        save(run / 'status.json', {'status': '已完成', 'phase': 'restored'})
        return {'status': '已完成', 'result': '已恢复备份并核验'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.codex')
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('audit')
    a.add_argument('--output', type=Path)
    a = sub.add_parser('plan')
    a.add_argument('--report', required=True, type=Path)
    a.add_argument('--targets', required=True, type=Path)
    for command in ('apply', 'verify', 'status', 'cancel', 'restore'):
        a = sub.add_parser(command)
        a.add_argument('--plan', required=True, type=Path)
        if command == 'apply':
            a.add_argument('--wait-for-exit', action='store_true')
            a.add_argument('--timeout', type=float, default=600)
            a.add_argument('--confirm', required=True, help='已审核计划的 SHA-256')
        if command == 'restore':
            a.add_argument('--confirm', required=True, help='已审核计划的 SHA-256')
    args = parser.parse_args()
    try:
        if args.command == 'audit':
            result = audit(args.home.expanduser().resolve())
            path = args.output or args.home / 'session-tidy/runs' / result['id'] / 'audit.json'
            save(path, result)
            print(json.dumps({'status': result['status'], 'report': str(path)}, ensure_ascii=False))
            return
        if args.command == 'plan':
            result = plan(load(args.report), load(args.targets))
            path = Path(result['home']) / 'session-tidy/runs' / result['id'] / 'plan.json'
            save(path, result)
            print(json.dumps({'plan': str(path), 'sha256': digest(result)}, ensure_ascii=False))
            return
        p = load(args.plan)
        run = args.plan.resolve().parent
        if args.command in ('apply', 'restore'):
            require(args.confirm == digest(p), '计划摘要不符，重新审核')
        if args.command == 'apply':
            result = apply(p, run, Path.home() / '.codex/backups', args.wait_for_exit, args.timeout)
        elif args.command == 'restore':
            result = restore(p, run)
        elif args.command == 'verify':
            result = verify(p)
            save(run / 'verification.json', result)
        elif args.command == 'status':
            result = load(run / 'status.json') if (run / 'status.json').exists() else {'status': '待确认', 'phase': 'planned'}
        else:
            status = load(run / 'status.json') if (run / 'status.json').exists() else {}
            require(status.get('phase') == 'waiting', '只能取消等待阶段')
            save(run / 'cancel.json', {'requested': True})
            result = {'status': '正在处理', 'result': '已请求取消等待'}
        print(json.dumps(result, ensure_ascii=False))
    except (Blocked, OSError, ValueError, sqlite3.Error, KeyError) as exc:
        print(json.dumps({'status': '阻塞', 'reason': str(exc) if isinstance(exc, Blocked) else type(exc).__name__,
                          '解除条件': '核对现场、修正输入或适配结构后重新审计'}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
