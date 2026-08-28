# LLM Wiki 知识库平台 Phase 2 SP3「增量编译」设计文档

> **版本**：v0.1 ｜ **日期**：2026-08-28 ｜ **状态**：草案（待评审）
>
> **定位**：Phase 2 子项目 SP3 的详细设计。依据 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md) SP3；需求冲突时以 [LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8 为准。
>
> **前置已交付**：触发文件 Watcher（`4ba4304`，消费侧自动化——纸条 → headless Claude Code）已上线并通过端到端实战验证。本 SP 在其上补齐"增量编译"的另外两块：**RAW 直放监听** 与 **断点续跑**。

---

## 1. 目标与范围

**目标**：任何方式进入 `RAW/` 的文档（上传页/API/直接拖文件）都能被自动增量编译；编译链路中断后重跑不重复劳动；compile_tasks 状态机语义完整。

**范围内**：
- watcher 扩展：轮询时同时扫描 `RAW/**/*.md|.txt`，发现**未编译过的新文件**自动生成 compile 纸条（复用现有消费链路）
- 断点续跑（任务级）：`/process-triggers` 消费前按 `compile_tasks` 状态去重——`done/cached` 跳过，`failed/pending` 重编译
- compile_workflow 语义更新（断点续跑规则 + 与 trace 指标衔接）
- 退出标准对齐：RAW ≤ 100 份时单次增量编译 < 30 秒（单文档）；断点续跑不重复执行已完成任务

**范围外**：混合检索（SP4）、watchdog 文件系统事件监听（评估后用轮询，见决策 1）、步骤级断点（收益低）、分布式编译队列（Phase 3）。

## 2. 技术决策

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | RAW 监听方式 | **轮询扫描**（复用 watcher 5s 循环），不引入 watchdog | 零新依赖；轮询 5s 延迟对该业务无感；Windows 文件事件监听有重命名/缓冲坑；一个进程管两类监听，部署简单 |
| 2 | 断点粒度 | **任务级**（compile_tasks 一行 = 一个 RAW 文件一次编译） | 步骤级断点复杂度高收益低；任务级已满足"不重复执行已完成工作"的退出标准 |
| 3 | "新文件"判定 | **左连接 compile_tasks**：RAW 路径在表中无任何记录（或仅 failed）→ 视为待编译 | 不依赖文件 mtime（mtime 会因复制/同步漂移）；指纹缓存仍作为第二道防线 |
| 4 | RAW 直放纸条的合并 | watcher 每轮把**所有**新发现的 RAW 路径合并进**一张** compile 纸条 | 减少 claude 会话次数（一次会话编译一批，摊薄 1-2 分钟的会话冷启动） |

## 3. 增量判定逻辑（watcher 侧）

```
每轮扫描：
  new_files = RAW/**/*.md|.txt
              WHERE path NOT IN (SELECT DISTINCT raw_path FROM compile_tasks)
  → 若 new_files 非空：
      写 compile 纸条（合并全部新文件，source=watcher_scan）
      → 下一轮由既有消费链路唤起 claude 编译
```

**去重铁律**：判定取 compile_tasks **全部状态**（DISTINCT raw_path）——failed/pending/done/cached 都不算"新"，任何进过任务表的路径 watcher 不再自动出纸条。这保证：纸条一旦写出，任务已插入（pending）→ 下轮不再重复；claude 失败（failed）也不会被 watcher 无限重试（**防纸条风暴**），重试由人工在 UI 点击或指纹变化后由消费侧断点逻辑处理。

> 边界情况：文件在"扫描到"与"写纸条"之间被删除 → 纸条里该路径编译时自然 404 → workflow 标 failed（error_msg 记录），不崩溃。

## 4. 断点续跑（消费侧，workflow 修订）

`/process-triggers` 消费 compile 纸条时，对纸条内每个路径：

| compile_tasks 最新状态 | 行为 |
|----------------------|------|
| 无记录 | 全新编译（现状） |
| `done` 且指纹相同 | 跳过（输出"已编译，跳过"） |
| `done` 但指纹不同 | 重编译（文档已变更） |
| `cached` | 跳过 |
| `failed` / `pending` | 重编译（重置 started_at） |
| `processing` | **视为僵尸**：超过 30 分钟无更新 → 重编译；否则跳过（可能有另一会话在跑） |

> `processing` 状态当前代码从未写入（Demo 只用 pending/done/failed/cached）——本 SP 在 workflow 中补全该语义：编译开始时置 `processing`，结束置 `done/failed/cached`。这是"断点续跑"能识别僵尸任务的前提。

## 5. compile_tasks 状态机（SP3 完整版）

```
             ┌─────────┐  消费者取任务      ┌────────────┐
上传/扫描 ──►│ pending │ ─────────────────► │ processing │
             └─────────┘                    └─────┬──────┘
                 ▲  ▲                        指纹命中│   编译结束│
                 │  │              ┌─────────▼┐   ┌─────▼─────┐
                 │  └──(重试)──────│  failed  │   │done/cached│
                 │                 └──────────┘   └───────────┘
             （UI 重试按钮 / 指纹变化重新入队）
```

## 6. 文件变更

```
├── tools/trigger_watcher.py     # 🔄 扫描 RAW 增量 + 合并写纸条（+约 60 行）
├── .claude/commands/process-triggers.md   # 🔄 消费前任务级去重步骤
├── workflows/compile_workflow.md          # 🔄 断点续跑规则 + processing 状态 + 跳过输出
├── tests/test_watcher_scan.py   # 新增：增量判定（新文件/已 done/failed 重编/纸条合并）
└── docs/LLM_wiki_Phase2_SP3_设计文档.md   # 本文档
```

## 7. 测试计划

| 用例 | 断言 |
|------|------|
| 新 RAW 文件被扫描 | 生成一张纸条，含该路径；compile_tasks 出现 pending 行 |
| 已 done 文件不再扫描 | 无新纸条、无新任务行 |
| failed 文件不重复生成纸条 | 仅 UI 重试可再触发（防风暴） |
| 多个新文件合并一张纸条 | 纸条含全部路径（一次会话摊薄冷启动） |
| .tmp_/非 md|txt 忽略 | 不进纸条 |
| 断点续跑（workflow 层，Claude Code 执行时验证） | done+同指纹跳过；done+异指纹重编 |

## 8. 退出标准
- [ ] RAW 直放文件 5-10 秒内被 watcher 发现并自动编译（无人工）
- [ ] 重跑 /process-triggers 不重复执行 done/cached 任务（任务级断点）
- [ ] compile_tasks 状态机含 processing（僵尸判定依据）
- [ ] 测试绿 + 既有 74 用例回归绿

---

## Changelog
- **v0.1（2026-08-28）**：初稿。决策：轮询复用（拒 watchdog）、任务级断点、左连接判定新文件、多文件合并单纸条；补 processing 状态与僵尸判定（30 分钟）。Watcher 消费侧自动化已前置交付（4ba4304）。