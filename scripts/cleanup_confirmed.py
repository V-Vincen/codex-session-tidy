"""已授权的挂载与残留整理；不删除会话、索引行、正文或磁盘项目目录。

仅用于本地、已适配的 SQLite 结构。prepare 只读，apply 复核当前关联后由迁移
执行器离线备份并事务写入；重复执行只验证，失败使用 recovery.json 受保护恢复。
"""
import copy
import json
from pathlib import Path

import session_tidy as core


KIND = 'confirmed-cleanup-v1'
HISTORY_TABLES = ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items')
# 这些字段保存聊天或执行经过，其文字不是当前引用。其他结构化字段保守核对。
TEXT_FIELDS = {'text', 'input', 'output', 'content', 'message', 'command', 'commands',
               'aggregatedOutput', 'aggregated_output', 'guardian_history', 'commandActions',
               'parsed_cmd', 'changes', 'arguments', 'first_user_message', 'preview', 'title', 'name'}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def refs(value, candidates):
    """只识别结构化 ID；历史输出、文件名子串不能充当有效引用。"""
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in TEXT_FIELDS:
                if key in candidates:
                    result.add(key)
                result.update(refs(item, candidates))
    elif isinstance(value, list):
        for item in value:
            result.update(refs(item, candidates))
    elif isinstance(value, str):
        if value in candidates:
            result.add(value)
        elif value.startswith(('{', '[')):
            try:
                result.update(refs(json.loads(value), candidates))
            except ValueError:
                # 未知、无法解析且含目标 ID 的结构不按普通文字放行。
                result.update(i for i in candidates if i in value)
    elif isinstance(value, bytes):
        result.update(i for i in candidates if i.encode() in value)
    return result


def schema_hash(db):
    return core.digest([tuple(r) for r in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')])


def history_rows(db, ids):
    result = {}
    for table in HISTORY_TABLES:
        columns = {r['name'] for r in db.execute('PRAGMA table_info(' + quote(table) + ')')}
        core.require('thread_id' in columns, '历史清理结构不兼容：' + table)
        rows = []
        for tid in sorted(ids):
            rows.extend(dict(r) for r in db.execute('SELECT * FROM ' + quote(table) + ' WHERE thread_id=?', (tid,)))
        result[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return result


def body_inventory(home, candidates):
    """解码全部正文，检查首条身份及后续结构关联；只保存摘要与引用，不保存消息。"""
    evidence, linked = [], set()
    for folder in ('sessions', 'archived_sessions'):
        for path in sorted((home / folder).rglob('*')):
            if not (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.zst')):
                continue
            core.require(path.is_file() and not path.is_symlink(), '正文不是支持的普通文件')
            opener = open
            if path.name.endswith('.zst'):
                try:
                    from compression import zstd
                except ImportError:
                    raise core.Blocked('压缩正文检查需要 Python 3.14+ 的 compression.zstd，尚未写入数据')
                opener = zstd.open
            with opener(path, 'rb') as stream:
                meta = json.loads(stream.readline())
                core.require(meta.get('type') == 'session_meta' and isinstance(meta.get('payload', {}).get('id'), str),
                             '正文首条身份无法核验')
                linked.update(refs(meta, candidates))
                for line in stream:
                    if line.strip():
                        linked.update(refs(json.loads(line), candidates))
            # 当前任务正文可继续追加；保护语义引用与首条身份，不锁死历史字节长度。
            evidence.append({'path': str(path), 'id': meta['payload']['id'], 'header': core.digest(meta)})
    return {'files': evidence, 'linked': sorted(linked)}


def reference_check(home, project_ids, history_ids):
    """覆盖全部本地数据库与顶层配置，排除待删对象自身及纯历史文字。"""
    candidates = set(project_ids) | set(history_ids)
    blocked = {}
    for path in sorted(home.glob('*.sqlite')):
        core.require(not path.is_symlink(), '数据库为软链接，停止残留清理')
        with core.connect(path) as db:
            db.execute('BEGIN')
            tables = [r['name'] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                columns = [r['name'] for r in db.execute('PRAGMA table_info(' + quote(table) + ')')]
                # 诊断日志记录的是发生过的事件，thread_id 不是可继续会话的依赖。
                # 保留日志原文，不因诊断日志提到已删会话而阻止历史残留清理。
                if (path.name == 'logs_2.sqlite' and table == 'logs' and
                        {'ts', 'level', 'target', 'feedback_log_body', 'thread_id'} <= set(columns)):
                    continue
                conditions = ' OR '.join('CAST(' + quote(c) + ' AS TEXT) LIKE ?' for c in columns)
                for target in candidates:
                    query = 'SELECT * FROM ' + quote(table) + ' WHERE ' + conditions
                    for row in db.execute(query, tuple('%' + target + '%' for _ in columns)):
                        own_project = (path.name == 'state_5.sqlite' and
                                       table in ('projects', 'project_roots', 'project_idempotency_keys') and
                                       row['id' if table == 'projects' else 'project_id'] == target and
                                       target in project_ids)
                        own_history = (path.name == 'thread_history_1.sqlite' and table in HISTORY_TABLES and
                                       row['thread_id'] == target and target in history_ids)
                        identity = ('id' if table == 'projects' else 'project_id') if own_project else (
                            'thread_id' if own_history else None)
                        for col in columns:
                            if col == identity or col in TEXT_FIELDS:
                                continue
                            if target in refs(row[col], {target}):
                                blocked.setdefault(target, set()).add(path.name + ':' + table + '.' + col)
    for pattern in ('*.json', '*.jsonl', '*.toml'):
        for path in sorted(home.glob(pattern)):
            core.require(not path.is_symlink(), '顶层配置为软链接，停止残留清理')
            if path.suffix == '.toml':
                import tomllib
                data = tomllib.loads(path.read_text())
            elif path.suffix == '.jsonl':
                data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            else:
                data = core.load(path)
            for target in refs(data, candidates):
                blocked.setdefault(target, set()).add(path.name)
    inventory = body_inventory(home, candidates)
    for target in inventory['linked']:
        blocked.setdefault(target, set()).add('正文身份或结构关联')
    return {i: sorted(locations) for i, locations in blocked.items()}, inventory


def project_rows(db, ids):
    result = {}
    for table, column in (('projects', 'id'), ('project_roots', 'project_id'), ('project_idempotency_keys', 'project_id')):
        rows = []
        for pid in sorted(ids):
            rows.extend(dict(r) for r in db.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (pid,)))
        result[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return result


def discover(home, base):
    """重新计算本批对象，不沿用旧 ID；有有效关联的候选保留并记明依据。"""
    import migrate_confirmed as migration
    state, g = core.snapshot(home), core.load(home / migration.GLOBAL)
    core.require(core.digest(state['schema']) in core.load(
        Path(__file__).parents[1] / 'references/supported-schemas.json').values(), '数据库结构不在白名单中')
    mappings = g['app-server-project-id-by-legacy-project-id-by-host']
    core.require(len(mappings) == 1, '多主机项目映射不在本批适配范围')
    mapping = next(iter(mappings.values()))
    used_projects = {t['project_id'] for t in state['threads']}
    # 任何主机映射或侧栏使用中的空项目都保留，避免误删刚建的项目。
    protected = set(mapping.values()) | set(g['local-projects'])
    empty = [r for r in state['projects'] if r['id'] not in used_projects | protected]
    valid_ids = {t['id'] for t in state['threads']}
    with core.connect(home / 'thread_history_1.sqlite') as db:
        hs = schema_hash(db)
        core.require(hs in core.load(Path(__file__).parents[1] / 'references/supported-schemas.json').values(),
                     '历史库结构不在白名单中')
        orphan_ids = set()
        for table in HISTORY_TABLES:
            orphan_ids.update(r[0] for r in db.execute('SELECT DISTINCT thread_id FROM ' + table))
        orphan_ids -= valid_ids
    blocked, inventory = reference_check(home, {r['id'] for r in empty}, orphan_ids)
    core.require(valid_ids <= {r['id'] for r in inventory['files']},
                 '有有效会话尚未找到身份一致的普通或压缩正文，停止残留清理')
    empty = [r for r in empty if r['id'] not in blocked]
    orphan_ids -= set(blocked)
    roots, sidebar, hints = [], [], []
    for name, primary, extra in (
            ('devops', base / 'devops', base / 'cm_bird'),
            ('fullstack-quality-kit', base / 'my_skill/fullstack-quality-kit', base),
            ('lkb', base / 'lkb', base),
            ('codex-session-tidy', base / 'my_skill/codex-session-tidy', base / 'temp')):
        matches = [(legacy, item) for legacy, item in g['local-projects'].items() if item['name'] == name]
        if not matches:
            continue
        core.require(len(matches) == 1, '项目重名：' + name)
        legacy, item = matches[0]
        pid = mapping.get(legacy)
        core.require(primary.is_dir() and pid in {r['id'] for r in state['projects']}, '主目录或映射无效：' + name)
        core.require(item['rootPaths'] in ([str(primary)], [str(primary), str(extra)]), '侧栏目录前提变化：' + name)
        core.require(all(t['cwd'] == str(primary) for t in state['threads'] if t['project_id'] == pid),
                     '项目内工作目录尚未统一：' + name)
        db_roots = [r for r in state['project_roots'] if r['project_id'] == pid]
        core.require(any(r['path'] == str(primary) for r in db_roots), '数据库缺少主目录：' + name)
        core.require(all(r['path'] in (str(primary), str(extra)) for r in db_roots), '数据库有未适配的挂载：' + name)
        roots.extend(r for r in db_roots if r['path'] == str(extra))
        if item['rootPaths'] != [str(primary)]:
            sidebar.append({'legacy': legacy, 'name': name, 'before': item['rootPaths'], 'after': [str(primary)]})
        for key, values in g.items():
            if not any(part in key for part in ('thread-writable-roots:', 'thread-workspace-root-hints:')):
                continue
            core.require(isinstance(values, dict), '未知会话目录提示结构')
            for tid, value in values.items():
                if tid not in valid_ids or g['thread-project-assignments'].get(tid, {}).get('projectId') != legacy:
                    continue
                core.require(isinstance(value, str) or (isinstance(value, list) and all(isinstance(x, str) for x in value)),
                             '未知会话目录提示值')
                after = str(primary) if isinstance(value, str) else [str(primary)]
                if value != after:
                    hints.append({'key': key, 'id': tid, 'before': value, 'after': after})
    with core.connect(home / 'state_5.sqlite') as db:
        projects = project_rows(db, {r['id'] for r in empty})
    with core.connect(home / 'thread_history_1.sqlite') as db:
        history = history_rows(db, orphan_ids)
    return {'projects': projects, 'history_ids': sorted(orphan_ids), 'history_rows': history,
            'history_schema': hs, 'root_removals': roots, 'sidebar': sidebar, 'hints': hints, 'blocked': blocked,
            'inventory': inventory, 'guard': migration.guard(state, g)}


def build(home, base):
    import migrate_confirmed as migration
    p = migration.build(home, base)
    core.require(not p['changes'], '先完成会话目录迁移，再生成残留清理计划')
    cleanup = discover(home, base)
    core.require(cleanup['guard'] == p['guard'], '检查期间项目或归属发生变化')
    p.update(kind=KIND, cleanup=cleanup, root_removals=cleanup['root_removals'],
             scope='四项已确认挂载及无关联旧项目、历史残留；保留会话、索引与正文')
    p['deletions'] = {'roots': p['root_removals'], 'projects': cleanup['projects'],
                      'history_ids': cleanup['history_ids'],
                      'history_rows': {k: len(v) for k, v in cleanup['history_rows'].items()}}
    return p


def preflight(p):
    current = discover(Path(p['home']), Path(p['base']))
    # history 明细全量校验、正文身份与有效引用重新发现；允许无关活跃会话追加消息。
    core.require(core.digest(current) == core.digest(p['cleanup']), '清理对象、引用或正文身份变化，需重新生成计划')


def transform(g, p):
    g = copy.deepcopy(g)
    for item in p['cleanup']['sidebar']:
        roots = g['local-projects'][item['legacy']]['rootPaths']
        core.require(roots in (item['before'], item['after']), '侧栏挂载发生变化')
        g['local-projects'][item['legacy']]['rootPaths'] = item['after']
    for item in p['cleanup']['hints']:
        core.require(g[item['key']].get(item['id']) in (item['before'], item['after']), '会话目录提示发生变化')
        g[item['key']][item['id']] = item['after']
    return g


def mutate_state(db, p):
    projects = p['cleanup']['projects']
    ids = {r['id'] for r in projects['projects']}
    core.require(project_rows(db, ids) == projects, '旧项目或创建去重记录变化')
    for pid in sorted(ids):
        core.require(db.execute('SELECT COUNT(*) FROM threads WHERE project_id=?', (pid,)).fetchone()[0] == 0,
                     '旧项目出现有效会话')
        db.execute('DELETE FROM project_idempotency_keys WHERE project_id=?', (pid,))
        db.execute('DELETE FROM project_roots WHERE project_id=?', (pid,))
        core.require(db.execute('DELETE FROM projects WHERE id=?', (pid,)).rowcount == 1, '旧项目删除行数不符')


def mutate_history(db, p):
    cleanup = p['cleanup']
    core.require(schema_hash(db) == cleanup['history_schema'] and
                 history_rows(db, cleanup['history_ids']) == cleanup['history_rows'], '历史残留结构或记录变化')
    for table in HISTORY_TABLES:
        count = 0
        for tid in cleanup['history_ids']:
            count += db.execute('DELETE FROM ' + table + ' WHERE thread_id=?', (tid,)).rowcount
        core.require(count == len(cleanup['history_rows'][table]), '历史残留删除行数不符')


def validate(p):
    import migrate_confirmed as migration
    home = Path(p['home'])
    cleanup = p['cleanup']
    g = core.load(home / migration.GLOBAL)
    for item in cleanup['sidebar']:
        core.require(g['local-projects'][item['legacy']]['rootPaths'] == item['after'], '侧栏额外挂载仍存在')
    for item in cleanup['hints']:
        core.require(g[item['key']].get(item['id']) == item['after'], '会话目录提示尚未同步')
    with core.connect(home / 'state_5.sqlite') as db:
        ids = {r['id'] for r in cleanup['projects']['projects']}
        core.require(all(not v for v in project_rows(db, ids).values()), '旧项目或创建去重记录残留')
    with core.connect(home / 'thread_history_1.sqlite') as db:
        core.require(schema_hash(db) == cleanup['history_schema'], '历史库结构变化')
        core.require(all(not v for v in history_rows(db, cleanup['history_ids']).values()), '历史明细残留')
        migration.integrity(db)
    return {'projects_removed': len(ids), 'history_threads_removed': len(cleanup['history_ids']),
            'history_rows_removed': sum(len(v) for v in cleanup['history_rows'].values()),
            'root_registrations_removed': len(p['root_removals']), 'sidebar_projects_updated': len(cleanup['sidebar']),
            'thread_directory_hints_updated': len(cleanup['hints'])}
