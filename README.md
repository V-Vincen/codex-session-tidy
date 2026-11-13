# codex-session-tidy

检查并整理本机 Codex 会话的项目归属、挂载目录及已确认的无效残留，保留有效会话和归档状态。

## 使用前了解

支持 macOS 本地 Codex 数据。Python 3.11+，压缩正文核验与当前目录修复需要 Python 3.14+；缺少能力时停止相关清理。

当前可处理已适配的目录迁移、四项额外挂载、普通及压缩正文当前目录不一致、无有效关联残留，并提供备份、验证和恢复。通用索引补齐、TOML 自动修复尚未实现；App 实际操作仍需验收。具体范围见 [DESIGN.md](DESIGN.md) 第一章。

保留有效会话、归档状态、磁盘项目目录和正文；同一 ID 的正常多条索引记录保留。

归档数量包含主会话派生的子代理任务记录，可能与 App 列表数量不同。助手会按来源拆分统计，核对父会话、正文及单独读取结果；正常记录保留，不因列表未显示而清理。判断规则和案例见 [DESIGN.md](DESIGN.md) 第二章第 11 条。

## 日常操作

1. 发起整理

   在会话中输入：

   ```text
   使用 codex-session-tidy，检查并整理会话。
   ```

   助手检查目录、侧栏、数据库、正文、索引和配置，准备本批方案；无需你整理 ID 或编辑 JSON。

2. 查看摘要

   已核实且符合规则的列入本批方案，用简短的“项目 → 拟处理结果”展示；只有归属不明确、有效内容冲突等规则无法解决的问题才请你确认。

3. 执行一条命令，再退出 App

   在独立的 macOS Terminal 进入本项目目录，复制助手提供的完整命令。命令形式如下，时间戳由助手填写：

   ```bash
   python3 tidy.py apply <本次任务时间戳>
   ```

   启动后退出相关 Codex、ChatGPT、CC Switch，保持 Terminal 运行。工具等待退出后自动备份、修复和检查；看到“离线数据验证通过”提示再重启 App。出现阻塞时把结果交给助手核对，不强制跳过保护。

4. 重启后验收

   回到会话输入：

   ```text
   验收上一次修复。
   ```

   助手复查数据，只将无法自动核验的实际操作交给你，例如打开历史会话检查内容、继续会话检查工作目录。

没有需要修复的问题时，助手直接报告结果，无需退出 App。`restore` 仅用于确认需要回退时，不是日常流程的最后一步。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| [README.md](README.md) | 日常操作、文件用途和当前支持范围。 |
| [DESIGN.md](DESIGN.md) | 目标与范围、判断原则及示例、整理流程、命令说明、执行保护和验收标准。 |
| [SKILL.md](SKILL.md) | 助手执行检查、展示摘要、准备方案和组织验收的说明。 |
| [tidy.py](tidy.py) | 日常短命令入口，直接调用 `migrate_confirmed.py` 的 `main()`。 |
| [scripts/migrate_confirmed.py](scripts/migrate_confirmed.py) | 实现 `prepare/apply/verify/restore`，负责迁移计划、离线执行、备份、验证和恢复，并调度清理模块。 |
| [scripts/session_tidy.py](scripts/session_tidy.py) | 基础检查工具，提供数据读取、快照、摘要、进程检查和锁等公共能力，也保留内部命令入口。 |
| [scripts/cleanup_confirmed.py](scripts/cleanup_confirmed.py) | 核实并清理已确认的额外挂载、旧空项目、创建去重记录和无有效关联的历史明细。 |
| [references/supported-schemas.json](references/supported-schemas.json) | 允许写入的数据库结构摘要清单；结构不匹配时停止写入。 |
| [tests/test_session_tidy.py](tests/test_session_tidy.py) | 基础检查能力的隔离测试。 |
| [tests/test_migrate_confirmed.py](tests/test_migrate_confirmed.py) | 迁移、验证和恢复的隔离测试。 |
| [tests/test_cleanup_confirmed.py](tests/test_cleanup_confirmed.py) | 挂载与残留清理的隔离测试。 |

主要调用关系：

```text
tidy.py → scripts/migrate_confirmed.py
           ├─ scripts/session_tidy.py（复用基础能力）
           └─ scripts/cleanup_confirmed.py（执行已确认的清理）
```

日常按助手提供的 `tidy.py` 命令操作即可。所有命令及调用示例见 [DESIGN.md](DESIGN.md) 第四章。

任务记录位于 `~/.codex/session-tidy/runs/<时间戳>/`，备份位于 `~/.codex/backups/`。恢复依赖本批任务记录及备份，验收前保留完整记录。
