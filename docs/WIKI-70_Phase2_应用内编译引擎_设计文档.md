# 应用内编译引擎（L1）设计文档 v0.1

> **定位**：把知识层的 LLM 引擎从**应用外**（宿主机 Claude Code CLI）下沉到**应用内**（`ModelPort` 直调），
> 让编译在容器里可跑、可测、可计量、可租户隔离——这是"云端多租户部署"的前置改造（L1）。
> **需求上游**：`docs/WIKI-00_LLM_Wiki_PRD.md`（知识层需求唯一来源）、`docs/WIKI-01`（产物格式）、
> `docs/WIKI-50`（检索）、`docs/VAL-01`（输出契约校验）。
> **对外定位口径**：`docs/ARCH-00_项目定位与分层.md`。

---

## 1. 问题：为什么不能靠"应用外的 CLI 引擎"

原先知识层的编译/审核/问答都由 **watcher + headless Claude Code**（`claude -p /process-triggers`）
消费 `workflows/*.md` 执行。单机内网用得好，一放到**云端多租户**就露底：

| # | 问题 | 具体后果 |
|---|---|---|
| 1 | **不可容器化** | CLI 需要 Node 运行时、交互式登录态、可写 HOME；塞进镜像不干净、凭证无法按租户下发 |
| 2 | **无并发无队列** | watcher 串行"一次一个 claude 进程"；130 篇语料要串行跑很久，且无法横向扩展 |
| 3 | **没有租户上下文** | CLI 一份全局配置，没有 tenant 概念；多租户只能"一租户一实例"，运维不可持续 |
| 4 | **权限过宽** | `--permission-mode acceptEdits` 让 Agent 直接写宿主机文件系统；多租户下等于给了跨租户越权的可能 |
| 5 | **拿不到计量与配额** | 无法按租户统计 token/成本、无法限流；`trace_events` 只记延迟 |
| 6 | **不可测** | workflow 是自然语言文档，逻辑写不了单测——实测踩坑：端点不可达时整条链路空转 400 秒才发现 |

**关键判断**：编译是"**单文档 → 结构化产物**"的**确定性流水线**，不需要 Agent 的自主性；
而自主性正是 CLI 路线唯一的、也是昂贵的优势。所以正确的做法不是"抛弃 Agent"，
而是**按任务性质分层**：确定性任务走应用内引擎，开放探索任务（问答/研究/跨文档推理）保留 Agent runtime。

---

## 2. 目标架构：双引擎 + 同一份契约

```
                    ┌──────────────────── 同一份契约 ────────────────────┐
                    │ prompts/compile_prompt.md（输出 JSON 契约）        │
                    │ core/output_schema.py     （契约代码化校验）        │
                    └───────────────────────────────────────────────────┘
                                     ▲                        ▲
        ┌────────────────────────────┴──────┐   ┌─────────────┴────────────────────┐
        │ L1 应用内引擎（生产路径）          │   │ CLI 引擎（本地开发/开放任务）      │
        │ tools/compile_worker.py            │   │ tools/trigger_watcher.py           │
        │   → core/compile_service.py        │   │   → claude -p /process-triggers    │
        │   → core/model_port.py（任意        │   │   → workflows/compile_workflow.md  │
        │     OpenAI 兼容端点）              │   │                                    │
        └───────────────────────────────────┘   └────────────────────────────────────┘
```

切换方式：`COMPILE_ENGINE=api`（默认，应用内）| `claude_cli`（写触发纸条，交 watcher 消费）。
**两者产出物与落库结果一致**，所以可以按环境切换而不影响下游（审核页、检索、看板）。

---

## 3. 应用内引擎设计（`core/compile_service.py`）

### 3.0 审核引擎同样下沉（`core/review_service.py`）

审核阶段原先也只有 CLI 驱动（`review_workflow.md`）。同一套理由让它一并下沉，分工反而更清晰：

| 维度 | 由谁判定 | 依据 |
|---|---|---|
| 一 完整性 | **代码** | `rules.check_completeness`（四字段 + 正文 ≥100 中文字符） |
| 五 敏感信息 | **代码** | `rules.check_sensitive`（命中 `blocked` 时**不送模型**，一票否决） |
| 二 去重 / 三 职务归属 / 四 质量 / 六 合规 | **模型** | `prompts/review_prompt.md`（一次调用出四维） |
| **verdict** | **代码** | `review_prompt.md` §判定逻辑链；模型自报的 verdict 只作对照，不一致时记 concern 并留档 `verdict_model` |

落库 JSON 形状与 `review_prompt.md` 输出契约一致（`verdict / department / scores{...} / duplicates / concerns / summary`）——
因为 `review_router._to_out` 会拿它跑 `validate_review_output` 判 `ai_scores_valid`，工作台也按 `ai_scores.scores.<维度>` 取值。
**人工作业不变**：通过/驳回仍走 `/reviews/{id}/approve|reject` → `ops.approve_entry` 移文件 + 双写；模型永不直接发布条目。
驱动：`tools/review_worker.py`（`REVIEW_ENGINE=api|claude_cli`）。

### 3.1 流水线（把 workflow 的步骤代码化）

| 步 | 动作 | 代码位置 | 说明 |
|---|---|---|---|
| 1 | 断点续跑判定 | `db.latest_compile_task` | 同路径同指纹且 `done/cached` → **skipped**（不烧 token） |
| 2 | 门禁（**先于模型**） | `rules.check_sensitive` | `blocked`（身份证/密钥/明文密码/内部标记）→ `failed`，**模型零调用** |
| 3 | 建任务并置 processing | `db.insert_compile_task` / `update_compile_task` | 失败也留记录（可观测、可重试） |
| 4 | 调模型 | `model_port.default_port()` | `prompts/compile_prompt.md` 作 system prompt；超长正文按 `MAX_INPUT_CHARS` 截断并记录 |
| 5 | 契约校验 + 回灌重试 | `output_schema.validate_compile_output` | 违例清单**回灌进 prompt** 重试 1 次（盲重试几乎必然再错） |
| 6 | 落盘 + 双写 | `_write_products` | 资源摘要 → `NEXUS/资源/<标题>.md`（`status=active`）；概念页 → `pending_review/<标题>.md`（`status=pending`）；同写 `knowledge_entries`、刷新 `index.md` 资源节 |
| 7 | 记 trace | `_record_session_trace` | 每批次 1 条 `trace_events(span_type='compile_session')`，detail 含 `engine=api` |

### 3.2 三条不可退让的纪律

1. **永不越过人工**：概念页一律进 `pending_review/`（`status=pending`），人工在工作台放行后才移入 `NEXUS/概念/`；
   资源摘要按事实摘要策略直接 `active` 发布。文件与 PG 状态始终一一对应。
2. **契约不通过不落盘**：`_write_markdown` **写前**跑 `output_schema.validate_entry_frontmatter`，
   违例直接抛错 → 上层标 `failed` + 留 `error_msg`，绝不产出不合规条目。
3. **门禁在模型之前**：命中 `blocked` 的文档不送模型（敏感内容不外发），这与提交侧门禁同源。

### 3.3 标签清洗（真实数据暴露的第一个坑）

编译 prompt 对 `tags` 只是"优先使用预定义标签"的**软约束**，而落盘 frontmatter 的
`tags` 必须落在 `SCHEMA.md` 的三类命名空间内（硬约束）。实测模型会输出
`政策规划`/`新质生产力` 这类"好听但不合规"的标签 → 落盘即违反契约。

处理：`_namespaced_tags()` 做**确定性清洗**（丢弃域外标签、部门标签补首位），**不额外烧 token**；
模型的领域词已经体现在 `description` 与正文里。取舍：标签数量偏少（多数条目只剩部门标签），
见 §7 未完成项。

### 3.4 指纹幂等

`compile_tasks.fingerprint` 存正文 SHA256：内容没变就跳过（含失败的"见过即不重扫"），
内容变了（`done` 但指纹不同）则重编译。这是"不重复烧 token"的唯一闸门，也是重试风暴的防火墙。

---

## 4. 契约变更：来源类型枚举扩展到真实语料

`core/output_schema.SOURCE_TYPES` 原为 4 类（`个人_notes/会议/经验/项目`，Demo 期口径）。
接入真实企业语料后，**政策类文档连续两轮被判非法**（模型坚持输出"政策规划"，回灌重试仍不通过）。

处理：补齐为 9 类，同步 `prompts/compile_prompt.md` 的判定表与 `docs/WIKI-00` §5.1 的来源分类映射：

```
个人_notes | 会议 | 经验 | 项目 | 政策 | 行业研究 | 网络文摘 | 论文 | 客户沟通
```

> 这正是"契约要跟着真实数据长"的例子：Demo 期 4 类够用，真实语料一进来就露馅；
> 修法必须落在**契约层**（枚举 + prompt + 文档 + 测试），不能靠"让模型猜"。

---

## 5. 多租户路线图（L2 → L4）

L1 只解决"引擎能进容器"。要在云端做多租户，还差三层：

### L2 队列服务化（把 `compile_tasks` 变成真队列）—— ✅ 已实现

改造前实测：`compile_tasks(id, raw_path, nexus_path, fingerprint, status, error_msg, started_at TEXT, completed_at TEXT)`
——**是记录表，不是队列**：无租户、无租约、无尝试次数，时间戳还是 TEXT，多 worker 会抢同一任务、崩溃即卡死。

**schema 变更**（`schema.sql`，全部幂等 `ADD COLUMN IF NOT EXISTS` + `ALTER … TYPE … USING`）：

| 字段 | 用途 |
|---|---|
| `tenant_id`（默认 `default`） | 租户隔离（L3 的前置） |
| `lease_until` / `leased_by` | **可见性超时**：worker 崩溃后租约过期，任务自动回到可认领态（自愈） |
| `attempts` / `next_retry_at` | 指数退避重试（认领即 +1；退避期间不可认领） |
| `priority`（默认 100） | 交互式上传 10 先于批量导入 100 |
| `started_at/completed_at → TIMESTAMPTZ` | 时间算术（原为 TEXT） |
| `idx_tasks_claim(status, next_retry_at, priority, id)` | 支撑认领扫描 |

**队列原语**（`core/db.py`）：`enqueue_compile_task` → `claim_compile_task`（`FOR UPDATE SKIP LOCKED` 单条原子认领）
→ `finish_compile_task`（终态 / 带 `retry_backoff_seconds` 的退避重排）→ `requeue_compile_task`（人工重试，
清空 attempts 与退避）→ `queue_stats`（各状态计数 + 最老待处理年龄，供看板）。

**消费侧**：`tools/compile_worker.py --queue` 认领并编译，失败按 1/2/4 分钟退避，`attempts` 用尽才落终态 `failed`；
`compile_service.compile_one(..., task_id=…)` 在"任务已被认领"时**不碰任务状态**（生命周期归 worker），
因此多个 worker 进程/容器可并发跑同一队列。

**入口侧**：`/uploads` 直接入队（`priority=10`）；**触发纸条只在 `COMPILE_ENGINE=claude_cli` 时写**——
应用内引擎由队列消费，两边同时消费会重复编译。`/uploads/tasks/{id}/retry` 对应用内引擎走 `requeue`（不再依赖纸条）。

> 仍未做：Stalled 任务的定时巡检（目前靠"下次认领时租约已过期"自愈）、跨机时钟漂移（同一 PG 时钟，暂无风险）。

### L3 多租户隔离 —— ✅ 已实现

**目标**：租户边界由**数据库**强制，而不是靠应用自觉。

| 层 | 做法 |
|---|---|
| 数据 | 21 张业务表 + `users` 全部加 `tenant_id`；**默认值动态取当前租户** `current_setting('app.tenant_id')`——应用代码里的 INSERT 不必逐个改写就自动落在当前租户（历史行回填 `default`） |
| 隔离 | 每张表 `ENABLE` + **`FORCE ROW LEVEL SECURITY`**，策略 `USING/WITH CHECK (tenant_id = current_setting('app.tenant_id', true))`：跨租户**读不可见、写被拒** |
| 上下文 | `core/db.get_conn()` 每次 checkout 注入 `app.tenant_id`（连接池复用，必须每次重设）；后台 worker/脚本用 `db.bind_tenant()` |
| 请求 | JWT 增加 `tenant` claim（登录时取自 `users.tenant_id`）；**HTTP 中间件**（`api/main.py::request_context`）按 claim 绑定租户 + trace_id |

#### 三个实测踩到的关键点（都已写进代码注释与测试）

1. **超级用户绕过 RLS**：Docker 官方镜像的 `POSTGRES_USER`（本项目 `llmwiki`）是超级用户，
   `rolbypassrls=t`——只加策略不做角色分离，**本地"看起来配好了"其实隔离是失效的**。
   解决：新增受限角色 `llmwiki_app`（`docker/initdb/20-app-role`，`NOSUPERUSER` + 只给 DML 权限），
   应用以它连接；测试里专门用受限角色连接来验收 RLS，缺角色时**明确 skip 而不是假绿**。
2. **租户绑定必须写在中间件，不能写在 FastAPI 依赖**：同步依赖与同步端点各自在线程池里执行，
   `contextvars` 的修改**不会互相传递**（依赖里 set 的变量端点读不到）。
   中间件在事件循环里、`call_next` 之前设置，各线程池调用会复制当前上下文。
   同一个坑此前也影响了 Langfuse 的 trace_id 关联（上一版写在 trace 依赖里），一并修到中间件。
3. **默认值 fail-closed**：`current_setting('app.tenant_id', true)` 未设置时是 `NULL`，
   被 `NOT NULL` 拦下（迁移工具用裸连接时就撞上了）——失败得很响，好过静默写进错租户。
   任何不走 `db.get_conn()` 的连接都要自己 `set_config`。
4. **`users` 表豁免 RLS**：登录发生在"还不知道租户"之前，按用户名查身份必须跨租户可见；
   用户归属由 `users.tenant_id` 在应用层判定（token 里的 tenant 不参与鉴权，只确定数据边界）。

> 未做：**文件分区**（`vault/tenants/<id>/…`）。当前 `KB_ROOT` 仍是进程级根，多租户部署时
> 每租户一个 worker + 一份挂载即可；若要单实例服务多租户，需要把 compile/review 服务的路径解析
> 接到租户（设计见 §5.4 待补）。JWT 里的 tenant 在用户被迁移租户后会短暂过期（需重新登录）。

### L4 模型配置进库（当前是进程级单例）

实测现状：`MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME / MODEL_TIMEOUT /
DASHSCOPE_API_KEY（embedding 共用）/ SENSITIVE_FIELD_KEY` 全部走环境变量，
一份配置服务所有租户，改配置要重启，密钥明文在 env，**没有按租户的用量与配额**。

| 新表 | 关键字段 | 作用 |
|---|---|---|
| `tenant_model_configs` | `tenant_id, provider, base_url, model, api_key_ciphertext, max_tokens, temperature, daily_token_quota, enabled, updated_by` | 租户自带模型与密钥；`api_key_ciphertext` 复用现成的 AES-GCM `sensitive_cipher` 加密；`model_port.for_tenant()` **先查库、查不到回退环境变量**（本地开发零影响） |
| `llm_usage` | `tenant_id, purpose(compile/review/answer/clarification/state), model, input_tokens, output_tokens, latency_ms, cost, trace_id` | 配额拦截 + 账单 + 成本看板 |

密钥轮换沿用已有机制（`SENSITIVE_FIELD_KEY=v1:…,v2:…` + `key_version` 落库）；
`core/llm_observability.py` 的上报已就位，加 `tenant_id` 标签即可按租户看 Langfuse。

---

## 6. 测试与验收

**单测（`tests/test_compile_service.py`，11 例）**：合法输出落盘 + 双写 + trace；frontmatter 过契约；
违例回灌重试；两次违例 → `failed` 且**不落盘**；指纹幂等（同内容跳过、改内容重编）；
门禁 `blocked` 模型零调用；RAW 增量扫描；`COMPILE_ENGINE` 切换；frontmatter 清洗回归；
写前契约拒绝；RAW 缺失干净失败。

**真实语料验收**（本地 vault，130 篇企业语料中的前几篇，容器内运行）：

| 项 | 结果 |
|---|---|
| 引擎 | `engine=api`（容器内，无 Claude Code、无宿主机代理） |
| 前两篇 | 2/2 编译成功；第二篇**第 1 次违例（`source_type` 非法）→ 回灌后第 2 次通过** |
| 政策类文档 | 补枚举后 `source_type=政策` 编译成功（1/1，5 个概念页） |
| 产物 | 13 篇条目全部通过 `validate_entry_frontmatter`（0 违例） |
| 落库 | `knowledge_entries`：资源 `active` / 概念 `pending` 与文件状态一致；`compile_tasks` 全 `done`；`compile_session` trace 带 `engine=api` |

**真机暴露并修掉的三个问题**（都已补测试）：
① 自由标签违反落盘契约 → `_namespaced_tags` 清洗；
② `source_type` 枚举覆盖不了真实语料 → 扩展 9 类；
③ **队列模式下重复编译**（L2 实测）：同一文件被重复入队时，`latest_compile_task` 只看到"自己这条 pending"，
漏掉历史 `done` → 重复编译并产出 `xxx-2.md` 重复条目 → 新增 `db.find_done_compile_task(raw_path, fingerprint, exclude_id)`
作为去重判据（两种模式都生效），真机复验重放 **0 token 且 `skipped`**。

**人工作业闭环验收**（工作台按钮背后的 API）：
9 条待审 → AI 判定（8 通过 / 1 转人工：`dedup=similar` + concerns≥3 被判定链正确降级）→
人工放行 2 条 → 文件移入 `NEXUS/概念/` + frontmatter `status=active` + `index.md` 统计刷新（`资源 2 篇 · 概念 2 个`）
→ `/entries` 显示 active/pending 分明 → `/search` 命中具体条目（grep 通道；向量通道待回填 embedding）。

---

## 7. 已知取舍与未完成项

1. **标签命名空间偏窄**：清洗后多数条目只剩部门标签，检索价值下降。后续要么扩大 `SCHEMA.md`
   的领域/类型命名空间（需改权威 SCHEMA），要么把模型的自由词另存一个不参与检索的字段。
2. **上传分类仍是 4 类**（`个人_notes/会议/经验/项目`）：`upload_router.CATEGORIES`、前端下拉、
   `init.sh` 目录树未同步扩展；编译侧不受影响（`scan_new_raw` 遍历 RAW 下所有目录），但 UI 体验不一致。
3. **概念页仍需人工放行**：这是设计边界（不让模型改事实），不做"自动发布"。
4. **L3/L4 未实现**：RLS 隔离、租户模型配置与用量表仍是设计（§5）。
5. **CLI 引擎保留但未在 CI 覆盖**：需要宿主机 CLI 与登录态，属本地开发路径。
6. **向量通道未回填**：新编译条目的 `knowledge_entries.embedding` 为空，检索目前只命中 grep 通道，
   需跑一次 embedding backfill 才是真正的双通道（属 SP4 既有能力，不在本设计范围）。

---

## Changelog

- **v0.1（2026-09-14）**：初版并落地 L1。新增 `core/compile_service.py`（把
  `workflows/compile_workflow.md` 代码化：指纹幂等 → 门禁 → 模型 → 契约校验（违例回灌重试）→
  落盘双写 → trace）与 `tools/compile_worker.py`（`COMPILE_ENGINE=api|claude_cli` 双驱动）。
  同一批改动：`output_schema.SOURCE_TYPES` 由 4 类扩到 9 类（真实语料需要）、
  `prompts/compile_prompt.md` 判定表同步、`ops.append_index(section, line)` 抽成公共函数、
  `db.latest_compile_task()` 新增、`KB_ROOT` 打通到 Claude 侧（`${KB_ROOT:-vault}`）以便
  真实语料与产物落在**仓库外**。新增 `tests/test_compile_service.py`（11 例）与
  `test_output_schema.test_compile_source_type_covers_real_corpus`。
  真机验收：容器内编译真实企业语料成功，13 篇条目 0 契约违例。
  多租户路线图（L2 队列 / L3 RLS 隔离 / L4 模型配置与用量进库）见 §5。
- **v0.2（2026-09-14）**：① **审核引擎同样下沉**（`core/review_service.py` + `tools/review_worker.py`）：
   完整性/敏感信息两维由代码判定、模糊四维交模型、**verdict 由代码按判定逻辑链计算**
   （模型自报 verdict 只作对照，不一致时记 concern 并留档 `verdict_model`），落库形状与
   `review_prompt.md` 契约一致（工作台按 `ai_scores.scores.<维度>` 取值、`review_router` 判 `ai_scores_valid`）；
   新增 `tests/test_review_service.py`（18 例）。② **L2 队列服务化落地**：`compile_tasks` 加
   `tenant_id/lease_until/leased_by/attempts/next_retry_at/priority` + 时间戳转 `timestamptz` + 认领索引；
   `db` 新增 `enqueue/claim/finish/requeue/find_done/queue_stats` 队列原语（`FOR UPDATE SKIP LOCKED`）；
   `compile_worker --queue` 并发认领 + 指数退避 + 租约自愈；`/uploads` 直接入队（优先级 10）、
   **触发纸条只在 CLI 引擎下写**；`/uploads/tasks/{id}/retry` 对应用内引擎走 requeue；
   新增 `tests/test_compile_queue.py`（9 例：认领互斥、租约自愈、退避与终态、优先级、租户过滤、
   终态不可再认领、人工 requeue、重复入队不重复编译、上传入队不写纸条）。③ 真机暴露并修掉
   "队列模式重复编译产出 `-2` 重复条目"（`db.find_done_compile_task`，复验重放 0 token）。
   ④ 完整演示动线跑通：编译 → 审核 → 人工放行 → 浏览/检索（详见 §6）。
- **v0.3（2026-09-14）**：**L3 多租户隔离落地**（数据库强制，不靠应用自觉）。① 21 张业务表 + `users` 加
   `tenant_id`，默认值**动态取当前租户** `current_setting('app.tenant_id')`；每表 `ENABLE`+`FORCE ROW LEVEL SECURITY`，
   策略 `USING/WITH CHECK` 保证跨租户读不可见、写被拒；② 上下文注入：`db.get_conn()` 每次 checkout 设
   `app.tenant_id`（连接池复用必须每次重设）、`db.bind_tenant()` 供 worker/脚本使用；③ JWT 增加 `tenant` claim，
   **HTTP 中间件**（`api/main.py::request_context`）按 claim 绑定租户 + trace_id；④ 新增受限角色
   `llmwiki_app`（`docker/initdb/20-app-role`）——实测发现 Docker 默认 POSTGRES_USER 是超级用户、会**绕过 RLS**，
   只加策略不做角色分离等于假隔离；⑤ 修掉"同步依赖绑定 contextvar 传不到同步端点"的坑（同时修好 Langfuse
   trace_id 关联）；⑥ 修掉队列原语把租户写死 'default' 的 bug（单测抓到）；⑦ 迁移工具的裸连接需自设租户
   （默认值 NULL 被 NOT NULL 拦下，fail-closed）。新增 `tests/test_tenant_isolation.py`（应用层上下文 + 受限角色
   下的 RLS 读写隔离，缺角色时明确 skip）。
