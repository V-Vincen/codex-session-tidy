#!/usr/bin/env python3
"""执行已确认的目录迁移、挂载与残留整理；准备计划与离线写入分开。"""
import argparse
import contextlib
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import re

import session_tidy as core


GLOBAL = '.codex-global-state.json'
FIELDS = ('id', 'cwd', 'project_id', 'archived', 'archived_at')


def hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def path_map(value, old, new):
    """只映射目录边界内的路径，禁止字符串前缀误匹配。"""
    if isinstance(value, str) and Path(value).is_absolute():
        p = Path(value)
        if p.is_relative_to(Path(old)):
            return str(Path(new) / p.relative_to(old))
    return value


def guard(state, global_state):
    # 关闭 App 可能刷新标题、时间和窗口位置；这些不参与本批修复，读取最新值保留。
    return core.digest({
        'schema': state['schema'], 'projects': state['projects'], 'roots': state['project_roots'],
        'threads': sorted([{k: t.get(k) for k in FIELDS} for t in state['threads']], key=lambda t: t['id']),
        'global': {k: v for k, v in global_state.items() if k in (
            'local-projects', 'app-server-project-id-by-legacy-project-id-by-host',
            'thread-project-assignments', 'projectless-thread-ids', 'sidebar-project-thread-orders',
            'thread-project-membership-host-ids') or 'thread-writable-roots:' in k
            or 'thread-workspace-root-hints:' in k or 'thread-projectless-output-directories:' in k}})


def project_primary_paths(state, g):
    """按项目数据推断唯一主目录，返回 legacy project id 到主目录的映射。"""
    mapping = next(iter(g['app-server-project-id-by-legacy-project-id-by-host'].values()))
    db_projects = {p['id'] for p in state['projects']}
    result = {}
    for legacy, project in g.get('local-projects', {}).items():
        pid = mapping.get(legacy)
        if pid not in db_projects:
            continue
        paths = project.get('rootPaths')
        core.require(isinstance(paths, list) and paths and all(isinstance(x, str) for x in paths),
                     '项目挂载结构未知：' + str(project.get('name')))
        roots = [r for r in state['project_roots'] if r['project_id'] == pid]
        # 侧栏可能先于数据库写入新建空项目；没有数据库根登记时不把它误判为挂载异常。
        if not roots:
            continue
        core.require(set(paths) <= {r['path'] for r in roots}, '侧栏挂载未登记：' + str(project.get('name')))
        cwds = {t['cwd'] for t in state['threads'] if t['project_id'] == pid}
        core.require(cwds <= set(paths), '项目会话工作目录未登记：' + str(project.get('name')))
        candidates = set(paths) & cwds
        if len(candidates) > 1:
            raise core.Blocked('项目主目录不明确：' + str(project.get('name')))
        if not candidates:
            core.require(len(paths) == 1, '项目主目录不明确：' + str(project.get('name')))
            primary = paths[0]
        else:
            primary = next(iter(candidates))
        core.require(Path(primary).is_dir(), '项目主目录无效：' + str(project.get('name')))
        result[legacy] = primary
    return result


def confirmed_root_removals(state, g, base=None):
    """按项目会话工作目录清理所有可确定的额外挂载登记。"""
    mapping = next(iter(g['app-server-project-id-by-legacy-project-id-by-host'].values()))
    primary = project_primary_paths(state, g)
    removals = []
    for legacy, main_path in primary.items():
        pid = mapping[legacy]
        roots = [r for r in state['project_roots'] if r['project_id'] == pid]
        removals.extend(r for r in roots if r['path'] != main_path)
    return removals


def detect_migrations(state, g, base):
    """从侧栏主目录与会话 cwd 的差异识别项目整体迁移，不依赖项目名称。"""
    mapping = next(iter(g['app-server-project-id-by-legacy-project-id-by-host'].values()))
    projects = g.get('local-projects', {})
    db_projects = {p['id'] for p in state['projects']}
    assignments = g.get('thread-project-assignments', {})
    detected = []
    for legacy, project in projects.items():
        pid = mapping.get(legacy)
        if pid not in db_projects:
            continue
        roots = project.get('rootPaths')
        if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], str):
            continue
        target = roots[0]
        db_roots = {r['path'] for r in state['project_roots'] if r['project_id'] == pid}
        old = {path for path in db_roots - {target}
               if Path(path).name == Path(target).name}
        if not old and not Path(target).exists():
            # 仅在共同基准目录下找到唯一同名目录时推断迁移目标。
            candidates = [p for p in base.rglob(Path(target).name) if p.is_dir()]
            if len(candidates) == 1:
                old = {target}
                target = str(candidates[0])
        if len(old) > 1:
            raise core.Blocked('项目迁移来源不明确：' + str(project.get('name')))
        if old:
            source = next(iter(old))
            if Path(source).name != Path(target).name or not Path(target).is_dir():
                raise core.Blocked('项目迁移目标不明确：' + str(project.get('name')))
            detected.append({'legacy': legacy, 'project_id': pid, 'old': source, 'new': target})
    return detected


def temp_target(state, g):
    """查找有效 temp 项目；仅用于归属项目已消失的归档会话兜底。"""
    mapping = next(iter(g['app-server-project-id-by-legacy-project-id-by-host'].values()))
    matches = [(legacy, item) for legacy, item in g.get('local-projects', {}).items()
               if item.get('name') == 'temp' and mapping.get(legacy) in {p['id'] for p in state['projects']}]
    core.require(len(matches) == 1, '有效 temp 项目不存在或重名')
    legacy, item = matches[0]
    core.require(len(item.get('rootPaths', [])) == 1 and Path(item['rootPaths'][0]).is_dir(), 'temp 主目录无效')
    return legacy, mapping[legacy], item['rootPaths'][0]


def build(home, base, prune_roots=False):
    state = core.snapshot(home)
    core.require(core.digest(state['schema']) in core.load(Path(__file__).parents[1] / 'references/supported-schemas.json').values(), '数据库结构不在白名单中')
    g = core.load(home / GLOBAL)
    projects = g['local-projects']
    mappings = g['app-server-project-id-by-legacy-project-id-by-host']
    core.require(len(mappings) == 1, '存在多个主机映射，需单独核验')
    mapping = next(iter(mappings.values()))
    db_projects = {p['id']: p for p in state['projects']}
    migrations = detect_migrations(state, g, base)
    migration_by_old = {item['old']: item for item in migrations}
    temp, temp_pid, temp_root = temp_target(state, g)
    assignment_by_thread = {tid: value.get('projectId') for tid, value in g.get('thread-project-assignments', {}).items()
                            if isinstance(value, dict)}
    result = []
    assignments = g['thread-project-assignments']
    for t in state['threads']:
        tid, cwd = t['id'], t['cwd']
        assignment = assignments.get(tid)
        core.require(assignment is None or isinstance(assignment, dict), '未知会话关联结构')
        legacy = assignment.get('projectId') if assignment else None
        if legacy:
            core.require(legacy in projects and legacy in mapping, '当前关联指向未知项目')
        migration = next((item for item in migrations if legacy == item['legacy']), None)
        source_migration = next((item for old, item in migration_by_old.items()
                                 if Path(cwd).is_relative_to(Path(old))), None)
        primary = projects[legacy]['rootPaths'][0] if legacy and legacy in projects and projects[legacy].get('rootPaths') else None
        moved = bool(legacy and (mapping[legacy] != t['project_id'] or cwd != primary))
        known_project_ids = set(db_projects)
        assignment_missing = legacy is None or legacy not in projects or mapping.get(legacy) not in known_project_ids
        orphan_archived = bool(t['archived'] and assignment_missing and t.get('project_id') not in known_project_ids)
        if not (source_migration or migration or moved or orphan_archived):
            continue
        if source_migration:
            legacy, target_cwd = source_migration['legacy'], path_map(cwd, source_migration['old'], source_migration['new'])
        elif orphan_archived:
            legacy, target_cwd = temp, temp_root
        else:
            target_cwd = primary
        core.require(legacy is not None and Path(target_cwd).is_dir(), '目标工作目录无效，停止本批修复')
        before = {k: t.get(k) for k in FIELDS}
        after = {**before, 'cwd': target_cwd, 'project_id': mapping[legacy]}
        body = Path(t['rollout_path'])
        core.require(body.is_absolute() and not body.is_symlink() and body.is_file()
                     and any(body.resolve().is_relative_to((home / folder).resolve())
                             for folder in ('sessions', 'archived_sessions')), '待改会话正文不在支持的普通文件范围内')
        first, tail = body_parts(body)
        core.require(first.get('type') == 'session_meta' and first.get('payload', {}).get('id') == tid, '正文 ID 或元信息不符')
        result.append({'id': tid, 'before': before, 'after': after, 'legacy_project': legacy,
                       'project_name': projects[legacy]['name'], 'body': str(body),
                       'body_hash': hash_file(body), 'tail_hash': hashlib.sha256(tail).hexdigest()})
        body_bytes(result[-1])
    history = None
    if any(len(body_bytes(c)) != Path(c['body']).stat().st_size for c in result):
        with core.connect(home / 'thread_history_1.sqlite') as db:
            history = {'before': history_guard(db, result), 'after': history_guard(db, result, True)}
    removals = confirmed_root_removals(state, g, base) if prune_roots else []
    orphan_root_paths = sorted({t['cwd'] for t in state['threads']
                                if t['archived'] and not t['project_id'] and not Path(t['cwd']).exists()})
    return {'id': core.run_id(), 'kind': 'confirmed-migration-v1', 'home': str(home), 'history': history,
            'base': str(base), 'path_maps': migrations, 'orphan_root_paths': orphan_root_paths,
            # 兼容旧计划读取；实际处理只使用上面的数据驱动集合。
            'old': migrations[0]['old'] if migrations else '',
            'new': migrations[0]['new'] if migrations else '',
            'kkai': orphan_root_paths[0] if orphan_root_paths else '',
            'guard': guard(state, g), 'schema': core.digest(state['schema']), 'changes': result,
            'archive': {t['id']: [t['archived'], t.get('archived_at')] for t in state['threads']},
            'root_removals': removals,
            'deletions': removals, 'scope': '迁移目录和当前项目关联；仅移除计划列明的已确认额外根登记，保留项目、创建去重记录、历史正文与归档状态'}


def body_stream(path):
    """正文偏移以解压后的字节计；缺少解码能力时拒绝相关操作。"""
    return core.body_stream(path)


def body_parts(path):
    with body_stream(path) as stream:
        first = json.loads(stream.readline())
        tail = stream.read()
    return first, tail


def body_bytes(change):
    path = Path(change['body'])
    core.require(not path.is_symlink() and hash_file(path) == change['body_hash'], '正文在计划后变化，重新规划')
    first, tail = body_parts(path)
    core.require(first.get('type') == 'session_meta' and first.get('payload', {}).get('id') == change['id'], '正文元信息不符')
    first['payload']['cwd'] = change['after']['cwd']
    # 优先保持首行字节长度；长度增加时由历史数据库偏移同步处理。
    with body_stream(path) as stream:
        original_size = len(stream.readline())
    encoded = json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode()
    data = encoded + b' ' * max(0, original_size - len(encoded) - 1) + b'\n' + tail
    if str(path).endswith('.jsonl.zst'):
        from compression import zstd
        return zstd.compress(data)
    return data


def body_delta(change):
    """使用解压后首行差额平移历史位置，不能用压缩文件大小差额。"""
    if 'body_delta' in change:
        return change['body_delta']
    return len(body_bytes(change)) - Path(change['body']).stat().st_size


def assignment_project_id(value):
    """读取会话项目关联的已知结构；未知结构必须阻塞，不能猜测。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        project_id = value.get('projectId')
        core.require(project_id is None or isinstance(project_id, str), '未知会话关联结构')
        return project_id
    raise core.Blocked('未知会话关联结构')


def build_body_repair(home):
    """按明确侧栏主目录修正当前元信息，不改归属、挂载、历史或归档。"""
    state, g = core.snapshot(home), core.load(home / GLOBAL)
    core.require(core.digest(state['schema']) in core.load(
        Path(__file__).parents[1] / 'references/supported-schemas.json').values(), '数据库结构不在白名单中')
    mappings = g['app-server-project-id-by-legacy-project-id-by-host']
    core.require(len(mappings) == 1, '多主机项目映射需单独核实')
    mapping = next(iter(mappings.values()))
    changes = []
    for t in state['threads']:
        body = Path(t['rollout_path'])
        core.require(body.is_absolute() and not body.is_symlink(), '正文路径无效或为软链接')
        if not body.exists():
            body = Path(str(body) + '.zst')
        core.require(body.is_file() and not body.is_symlink() and any(
            body.resolve().is_relative_to((home / folder).resolve())
            for folder in ('sessions', 'archived_sessions')), '正文路径越界或不存在')
        first, tail = body_parts(body)
        core.require(first.get('type') == 'session_meta' and first.get('payload', {}).get('id') == t['id'], '正文身份不符')
        # 无显式侧栏关联时，必须有唯一项目映射；目录以侧栏单一主目录为准。
        candidates = [legacy for legacy, pid in mapping.items() if pid == t['project_id']]
        assignment = assignment_project_id(g['thread-project-assignments'].get(t['id']))
        if not candidates and assignment is None:
            # 无项目归属的普通会话没有可推断的目标目录，保留现状并继续检查其他会话。
            continue
        core.require(len(candidates) == 1 and (assignment is None or assignment == candidates[0]), '正文目标归属不明确')
        project = g['local-projects'][candidates[0]]
        core.require(len(project['rootPaths']) == 1 and Path(project['rootPaths'][0]).is_dir(), '项目主目录不明确')
        target = project['rootPaths'][0]
        if first['payload'].get('cwd') == target and t['cwd'] == target:
            continue
        for line in tail.splitlines():
            if line.strip():
                json.loads(line)  # 不在受损正文上重新编码首条元信息。
        with body_stream(body) as stream:
            header_size = len(stream.readline())
        first['payload']['cwd'] = target
        size = len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode()) + 1
        values = {k: t.get(k) for k in FIELDS}
        changes.append({'id': t['id'], 'before': values, 'after': {**values, 'cwd': target},
                        'legacy_project': candidates[0], 'project_name': project['name'],
                        'body': str(body), 'body_hash': hash_file(body),
                        'tail_hash': hashlib.sha256(tail).hexdigest(),
                        'body_delta': max(0, size - header_size)})
    history = None
    if any(body_delta(c) for c in changes):
        with core.connect(home / 'thread_history_1.sqlite') as db:
            core.require(history_schema(db) in core.load(
                Path(__file__).parents[1] / 'references/supported-schemas.json').values(), '历史库结构不在白名单中')
            history = {'before': history_guard(db, changes), 'after': history_guard(db, changes, True)}
    core.require(guard(state, g) == guard(core.snapshot(home), core.load(home / GLOBAL)), '检查期间归属变化')
    return {'id': core.run_id(), 'kind': 'body-cwd-repair-v1', 'home': str(home),
            'schema': core.digest(state['schema']), 'guard': guard(state, g), 'changes': changes,
            'history': history, 'archive': {t['id']: [t['archived'], t.get('archived_at')] for t in state['threads']},
            'root_removals': [], 'deletions': [], 'scope': '按明确侧栏主目录修正数据库与正文当前目录及必要历史偏移，保留归属、挂载和历史内容'}


def history_schema(db):
    return core.digest([tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')])


HISTORY_OFFSETS = {'thread_turns': ('rollout_byte_offset', 'rollout_end_byte_offset'),
                   'thread_history_projection_state': ('next_rollout_byte_offset',)}


def history_guard(db, changes, shift=False):
    rows = {}
    for table, columns in HISTORY_OFFSETS.items():
        actual = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        core.require({'thread_id', *columns} <= actual, '历史字节偏移结构不兼容')
        values = []
        for c in changes:
            delta = body_delta(c) if shift else 0
            for r in db.execute('SELECT * FROM ' + table + ' WHERE thread_id=?', (c['id'],)):
                value = dict(r)
                if shift:
                    for column in columns:
                        if value[column] is not None and value[column] > 0:
                            value[column] += delta
                values.append(value)
        rows[table] = sorted(values, key=lambda x: json.dumps(x, sort_keys=True))
    return core.digest(rows)


def shift_history(db, changes):
    # 只平移首行之后的字节位置，消息内容、序号和状态保持原样。
    for c in changes:
        delta = body_delta(c)
        for table, columns in HISTORY_OFFSETS.items():
            for column in columns:
                db.execute('UPDATE ' + table + ' SET ' + column + '=' + column + '+? WHERE thread_id=? AND ' + column + '>0', (delta, c['id']))


def transform(g, p):
    if p.get('kind') == 'body-cwd-repair-v1':
        return copy.deepcopy(g)
    if p.get('kind') == 'confirmed-cleanup-v1':
        import cleanup_confirmed
        return cleanup_confirmed.transform(g, p)
    g = copy.deepcopy(g)
    changes = {c['id']: c for c in p['changes']}
    for project in g['local-projects'].values():
        paths = project.get('rootPaths', [])
        for item in p.get('path_maps', []):
            paths = [path_map(r, item['old'], item['new']) for r in paths]
        project['rootPaths'] = list(dict.fromkeys(paths))
    for tid, c in changes.items():
        g['thread-project-assignments'][tid] = {'projectKind': 'local', 'projectId': c['legacy_project']}
        g.setdefault('thread-project-membership-host-ids', {})[tid] = 'local'
        # 同一条会话只能在一个项目排序列表中出现，保留其他会话的相对顺序。
        orders = g.setdefault('sidebar-project-thread-orders', {})
        target = orders.setdefault(c['legacy_project'], {'threadIds': []})
        for pid, order in orders.items():
            core.require(isinstance(order, dict) and isinstance(order.get('threadIds'), list), '未知侧栏排序结构')
            if pid != c['legacy_project']:
                order['threadIds'] = [x for x in order['threadIds'] if x != tid]
        if tid not in target['threadIds']:
            target['threadIds'].append(tid)
        for key in list(g):
            if any(part in key for part in ('thread-writable-roots:', 'thread-workspace-root-hints:', 'thread-projectless-output-directories:')):
                values = g[key]
                core.require(isinstance(values, dict), '未知目录提示结构')
                if tid in values:
                    value = values[tid]
                    # 只更新当前结构化提示；不搜索替换消息、草稿或历史 turn_context。
                    def convert(v):
                        if isinstance(v, list):
                            return list(dict.fromkeys(convert(x) for x in v))
                        core.require(isinstance(v, str), '未知目录提示值')
                        for item in p.get('path_maps', []):
                            v = path_map(v, item['old'], item['new'])
                        for old in p.get('orphan_root_paths', []):
                            v = path_map(v, old, c['after']['cwd'])
                        return v
                    values[tid] = convert(value)
    g['projectless-thread-ids'] = [tid for tid in g.get('projectless-thread-ids', []) if tid not in changes]
    return g


def mutate(db, p):
    for c in p['changes']:
        core.offline()
        a, b = c['after'], c['before']
        count = db.execute('UPDATE threads SET cwd=?,project_id=? WHERE id=? AND cwd=? AND project_id IS ? AND archived=?',
                           (a['cwd'], a['project_id'], c['id'], b['cwd'], b['project_id'], b['archived'])).rowcount
        core.require(count == 1, '会话前置条件变化')
    if p.get('kind') == 'body-cwd-repair-v1':
        return  # 当前目录计划不修改项目归属、挂载登记或历史消息。
    # 三列精确匹配并校验行数，不能按路径误删其他项目的同名目录。
    for row in p.get('root_removals', []):
        count = db.execute('DELETE FROM project_roots WHERE project_id=? AND position=? AND path=?',
                           (row['project_id'], row['position'], row['path'])).rowcount
        core.require(count == 1, '已确认根目录登记变化')
    if p.get('kind') == 'confirmed-cleanup-v1':
        import cleanup_confirmed
        cleanup_confirmed.mutate_state(db, p)
        return  # 清理计划只执行明确清单，不夹带旧迁移路径清理。
    # 删除已不存在的归档来源根目录登记，不删除项目、会话、正文或其他有效根目录。
    for old in p.get('orphan_root_paths', []):
        db.execute('DELETE FROM project_roots WHERE path=?', (old,))
    for row in db.execute('SELECT project_id,position,path FROM project_roots').fetchall():
        target = row['path']
        for item in p.get('path_maps', []):
            target = path_map(target, item['old'], item['new'])
        if target != row['path']:
            db.execute('UPDATE project_roots SET path=? WHERE project_id=? AND position=?',
                       (target, row['project_id'], row['position']))


def logical_hash(db):
    h = hashlib.sha256()
    for line in db.iterdump():
        h.update(line.encode())
        h.update(b'\n')
    return h.hexdigest()


def integrity(db):
    core.require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', '数据库完整性失败')
    core.require(not db.execute('PRAGMA foreign_key_check').fetchall(), '数据库外键检查失败')


def atomic_bytes(path, data, mode):
    fd, temp = tempfile.mkstemp(prefix='.session-tidy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def validate(p):
    home = Path(p['home'])
    state = core.snapshot(home)
    core.require(core.digest(state['schema']) == p['schema'], '结构变化')
    ts = {t['id']: t for t in state['threads']}
    for tid, archived in p['archive'].items():
        core.require(tid in ts and [ts[tid]['archived'], ts[tid].get('archived_at')] == archived, '归档状态或会话数量变化')
    g = core.load(home / GLOBAL)
    for c in p['changes']:
        core.require(all(ts[c['id']].get(k) == v for k, v in c['after'].items()), '会话结果不符')
        if p.get('kind') != 'body-cwd-repair-v1':
            core.require(g['thread-project-assignments'].get(c['id'], {}).get('projectId') == c['legacy_project'], '侧栏归属不符')
        if 'body' in c:
            first, tail = body_parts(c['body'])
            core.require(first.get('type') == 'session_meta' and first['payload'].get('id') == c['id']
                         and first['payload']['cwd'] == c['after']['cwd']
                         and hashlib.sha256(tail).hexdigest() == c['tail_hash'], '正文身份、当前目录或历史内容验证失败')
    if p.get('kind') not in ('confirmed-cleanup-v1', 'body-cwd-repair-v1'):
        for r in state['project_roots']:
            core.require(r['path'] not in p.get('orphan_root_paths', []), '归档来源根目录仍存在')
            core.require(all(path_map(r['path'], item['old'], item['new']) == r['path']
                             for item in p.get('path_maps', [])), '项目根目录仍有旧路径')
    for removed in p.get('root_removals', []):
        core.require(not any(r['project_id'] == removed['project_id'] and r['path'] == removed['path']
                             for r in state['project_roots']), '已确认的额外根目录仍存在')
    core.require(transform(g, p) == g, '全局状态未完成校准')
    with core.connect(home / 'state_5.sqlite') as db:
        integrity(db)
    cleanup_result = {}
    if p.get('kind') == 'confirmed-cleanup-v1':
        import cleanup_confirmed
        cleanup_result = cleanup_confirmed.validate(p)
    if p.get('history'):
        with core.connect(home / 'thread_history_1.sqlite') as db:
            integrity(db)
            core.require(history_guard(db, p['changes']) == p['history']['after'], '历史偏移验证失败')
    return {'status': '已完成', 'result': '离线数据验证通过，App 内验收待完成', 'threads': len(p['changes']), **cleanup_result}


def restore(p, run):
    home = Path(p['home'])
    manifest = core.load(run / 'recovery.json')
    core.require(manifest['plan'] == core.digest(p), '恢复记录不匹配')
    core.offline()
    for name, item in manifest['files'].items():
        core.require(hash_file(item['backup']) == item['backup_hash'], '备份损坏')
        if name.endswith('.sqlite'):
            with core.connect(home / name) as db:
                current = logical_hash(db)
        else:
            current = hash_file(home / name)
        core.require(current in (item['before'], item['after']), '数据已被其他进程修改，保留现场；请重新核验')
    for name, item in manifest['files'].items():
        core.offline()
        if name.endswith('.sqlite'):
            with core.connect(Path(item['backup'])) as src, core.connect(home / name, True) as dst:
                src.backup(dst)
        else:
            atomic_bytes(home / name, Path(item['backup']).read_bytes(), item['mode'])
    for name, item in manifest['files'].items():
        if name.endswith('.sqlite'):
            with core.connect(home / name) as db:
                integrity(db)
                core.require(logical_hash(db) == item['before'], '数据库恢复核验失败')
        else:
            core.require(hash_file(home / name) == item['before'], '配置恢复核验失败')
    core.save(run / 'status.json', {'status': '已完成', 'phase': 'restored'})


def execute(p, run, backup_dir, timeout=600):
    home = Path(p['home'])
    with core.locked(home):
        if (run / 'status.json').exists():
            phase = core.load(run / 'status.json')['phase']
            if phase == 'complete':
                return validate(p)
            core.require(phase in ('waiting', 'blocked'), '上次执行已写入或恢复，先核验现场')
        core.save(run / 'status.json', {'status': '正在处理', 'phase': 'waiting'})
        print('正在处理：等待相关 App 退出，请保持此 Terminal 运行。', flush=True)
        start = time.monotonic()
        while core.active():
            core.require(not (run / 'cancel.json').exists(), '等待已取消')
            core.require(time.monotonic() - start < timeout, '等待退出超时，源数据未写入')
            time.sleep(0.5)
        core.require(not (run / 'cancel.json').exists(), '等待已取消')
        state, g = core.snapshot(home), core.load(home / GLOBAL)
        core.require(core.digest(state['schema']) == p['schema'] and guard(state, g) == p['guard'], '待修复的目录、归属或结构变化，请重新生成计划')
        if p.get('kind') != 'body-cwd-repair-v1':
            for item in p.get('path_maps', []):
                core.require(Path(item['new']).is_dir() and not Path(item['old']).exists(), '目录迁移前提变化')
        for c in p['changes']:
            core.require(Path(c['after']['cwd']).is_dir(), '目标工作目录不存在')
        cleanup = p.get('kind') == 'confirmed-cleanup-v1'
        if cleanup:
            import cleanup_confirmed
            cleanup_confirmed.preflight(p)
        next_g = transform(g, p)
        body_outputs = {c['body']: body_bytes(c) for c in p['changes'] if 'body' in c}
        global_path = home / GLOBAL
        core.require(not global_path.is_symlink() and not (home / 'state_5.sqlite').is_symlink(), '目标文件为软链接，停止写入')
        core.require(core.load(global_path) == g, '读取期间全局状态变化')
        # 只改根登记时保留配置文件原始字节，避免产生无关格式差异。
        after_bytes = (global_path.read_bytes() if next_g == g else
                       (json.dumps(next_g, ensure_ascii=False, indent=2) + '\n').encode())
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        manifest = {'plan': core.digest(p), 'files': {}}
        history_names = ['thread_history_1.sqlite'] if p.get('history') or (cleanup and p['cleanup']['history_ids']) else []
        core.require(all(not (home / name).is_symlink() for name in history_names), '历史库为软链接，停止写入')
        if p.get('history'):
            with core.connect(home / history_names[0]) as db:
                core.require(history_guard(db, p['changes']) == p['history']['before'], '计划后相关历史记录变化')
        for name in ('state_5.sqlite', GLOBAL, *history_names, *body_outputs):
            core.offline()
            src = home / name
            # 正文绝对路径可能超过文件系统单文件名上限，用稳定摘要标识备份目标。
            label = ('body-' + hashlib.sha256(name.encode()).hexdigest()[:24]) if Path(name).is_absolute() else name
            backup = backup_dir / ('session-tidy--' + label + '--' + stamp + '-before-migration-' + p['id'] + '.bak')
            core.require(not backup.exists(), '备份名称冲突')
            if name.endswith('.sqlite'):
                with core.connect(src) as db, contextlib.closing(sqlite3.connect(backup)) as dst:
                    db.backup(dst)
                    before = logical_hash(dst)
            else:
                shutil.copy2(src, backup)
                before = hash_file(backup)
            os.chmod(backup, 0o600)
            manifest['files'][name] = {'backup': str(backup), 'backup_hash': hash_file(backup),
                                      'before': before, 'mode': src.stat().st_mode & 0o777}
        core.save(run / 'status.json', {'status': '正在处理', 'phase': 'writing'})
        try:
            with contextlib.ExitStack() as stack:
                db = stack.enter_context(core.connect(home / 'state_5.sqlite', True))
                history_db = stack.enter_context(core.connect(home / history_names[0], True)) if history_names else None
                if history_db:
                    history_db.execute('BEGIN IMMEDIATE')
                    core.require(logical_hash(history_db) == manifest['files'][history_names[0]]['before'], '备份后历史记录变化')
                    if cleanup:
                        cleanup_confirmed.mutate_history(history_db, p)
                    else:
                        shift_history(history_db, p['changes'])
                    integrity(history_db)
                    manifest['files'][history_names[0]]['after'] = logical_hash(history_db)
                db.execute('BEGIN IMMEDIATE')
                try:
                    core.require(logical_hash(db) == manifest['files']['state_5.sqlite']['before'], '备份后数据库变化')
                    core.require(hash_file(global_path) == manifest['files'][GLOBAL]['before'], '备份后配置变化')
                    mutate(db, p)
                    integrity(db)
                    manifest['files']['state_5.sqlite']['after'] = logical_hash(db)
                    manifest['files'][GLOBAL]['after'] = hashlib.sha256(after_bytes).hexdigest()
                    for name, data in body_outputs.items():
                        core.require(hash_file(name) == manifest['files'][name]['before'], '备份后正文变化')
                        manifest['files'][name]['after'] = hashlib.sha256(data).hexdigest()
                    # 先持久化恢复记录，再替换 JSON 和提交数据库；崩溃也有明确恢复点。
                    core.save(run / 'recovery.json', manifest)
                    core.offline()
                    atomic_bytes(global_path, after_bytes, manifest['files'][GLOBAL]['mode'])
                    for name, data in body_outputs.items():
                        core.offline()
                        atomic_bytes(Path(name), data, manifest['files'][name]['mode'])
                    core.offline()
                    db.commit()
                    if history_db:
                        core.offline()
                        history_db.commit()
                except BaseException:
                    db.rollback()
                    raise
            core.offline()
            result = validate(p)
            core.save(run / 'verification.json', result)
            core.save(run / 'status.json', {**result, 'phase': 'complete'})
            return result
        except BaseException:
            core.save(run / 'status.json', {'status': '阻塞', 'phase': 'failed',
                                           'reason': '执行异常，查看恢复记录；App 重启时不得覆盖备份'})
            if (run / 'recovery.json').exists() and not core.active():
                try:
                    restore(p, run)
                except BaseException:
                    pass  # 保留 failed 状态和现场，不掩盖原始故障。
            raise


def store_plan(p):
    run = Path(p['home']) / 'session-tidy/runs' / p['id']
    run.mkdir(parents=True, exist_ok=False, mode=0o700)
    path = run / 'migration-plan.json'
    core.save(path, p)
    core.save(run / 'plan-checksum.json', {'sha256': core.digest(p)})
    return path


def resolve_run(home, name):
    core.require(bool(re.fullmatch(r'\d{8}-\d{6}-\d{6}', name)), '任务编号必须为时间戳')
    path = home.resolve() / 'session-tidy/runs' / name / 'migration-plan.json'
    core.require(path.resolve().parent.parent == (home.resolve() / 'session-tidy/runs').resolve(), '任务路径越界')
    p = core.load(path)
    core.require(p['id'] == name and Path(p['home']).resolve() == home.resolve(), '任务位置不匹配')
    checksum = core.load(path.parent / 'plan-checksum.json')['sha256']
    core.require(core.digest(p) == checksum, '计划发生变化，请重新审核')
    return path, checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'apply', 'verify', 'restore'))
    parser.add_argument('run', nargs='?', help='任务时间戳，替代长计划路径及摘要参数')
    parser.add_argument('--home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--base', type=Path)
    parser.add_argument('--prune-confirmed-roots', action='store_true', help='按项目主目录清理可确定的额外挂载登记')
    parser.add_argument('--cleanup-confirmed', action='store_true', help='按通用规则清理额外挂载、无关联旧项目和历史残留；保留索引和正文')
    parser.add_argument('--repair-body-cwd', action='store_true', help='按明确侧栏主目录修正数据库与正文当前目录，支持 zstd，无需 --base')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--confirm')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            core.require(sum((args.cleanup_confirmed, args.prune_confirmed_roots, args.repair_body_cwd)) <= 1, '不能混用计划选项')
            core.require(args.repair_body_cwd or args.base is not None, '需要项目父目录')
            if args.repair_body_cwd:
                p = build_body_repair(args.home.resolve())
            elif args.cleanup_confirmed:
                import cleanup_confirmed
                p = cleanup_confirmed.build(args.home.resolve(), args.base.resolve())
            else:
                p = build(args.home.resolve(), args.base.resolve(), args.prune_confirmed_roots)
            path = store_plan(p)
            print(json.dumps({'status': '正在处理' if args.cleanup_confirmed else '待确认', 'phase': 'prepared',
                              'plan': str(path), 'confirm': core.digest(p), 'threads': len(p['changes']),
                              'root_removals': len(p['root_removals'])}, ensure_ascii=False))
        else:
            if args.run:
                core.require(args.plan is None and args.confirm is None, '不能混用任务编号与计划参数')
                args.plan, args.confirm = resolve_run(args.home, args.run)
            core.require(args.plan is not None, '缺少计划路径')
            p = core.load(args.plan)
            core.require(p['kind'] in ('confirmed-migration-v1', 'confirmed-cleanup-v1', 'body-cwd-repair-v1'), '未知计划类型')
            run = args.plan.resolve().parent
            if args.command in ('apply', 'restore'):
                core.require(args.confirm == core.digest(p), '计划摘要不符')
            if args.command == 'apply':
                print(json.dumps(execute(p, run, Path.home() / '.codex/backups'), ensure_ascii=False))
            elif args.command == 'verify':
                print(json.dumps(validate(p), ensure_ascii=False))
            else:
                with core.locked(Path(p['home'])):
                    restore(p, run)
                print('已完成：恢复并核验通过。')
    except (core.Blocked, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        if args.plan and args.plan.exists():
            status = args.plan.parent / 'status.json'
            if not status.exists() or core.load(status).get('phase') == 'waiting':
                core.save(status, {'status': '阻塞', 'phase': 'blocked'})
        print('阻塞：' + (str(exc) if isinstance(exc, core.Blocked) else type(exc).__name__) + '。解除条件：核对现场后重新规划或受保护恢复。', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
