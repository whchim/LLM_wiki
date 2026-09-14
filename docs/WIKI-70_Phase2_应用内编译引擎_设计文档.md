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

### L2 队列服务化（把 `compile_tasks` 变成真队列）

现状实测：`compile_tasks(id, raw_path, nexus_path, fingerprint, status, error_msg, started_at TEXT, completed_at TEXT)`
——**是记录表，不是队列**：无租户、无租约、无尝试次数，时间戳还是 TEXT。

| 新增字段 | 用途 |
|---|---|
| `tenant_id` | 租户隔离 |
| `lease_until` | 可见性超时：worker 崩溃后任务自动回到可认领态 |
| `attempts` / `next_retry_at` | 指数退避重试（不再靠 watcher 的重试循环） |
| `priority` | 交互式上传先于批量导入 |
| `started_at/completed_at → timestamptz` + `(status, next_retry_at)` 索引 | 支持 `SELECT … FOR UPDATE SKIP LOCKED` 并发认领 |

`/uploads` 直接入队（不再写文件纸条），watcher 退化为本地开发可选。

### L3 多租户隔离

- 全表加 `tenant_id` + Postgres **RLS**；JWT 带 tenant claim，API 层统一注入租户过滤（不靠调用方自觉）
- 文件分区：`vault/tenants/<tenant_id>/{RAW,pending_review,NEXUS}/…`（或对象存储前缀）；
  检索、指纹缓存、幂等键、审计、trace 全部带租户维度
- 现有 `KB_ROOT` 作为**进程级根**保留（本地开发/单租户部署），多租户时在其下加租户层

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

**真机暴露并修掉的两个契约问题**（都已补测试）：
① 自由标签违反落盘契约 → `_namespaced_tags` 清洗；
② `source_type` 枚举覆盖不了真实语料 → 扩展 9 类。

---

## 7. 已知取舍与未完成项

1. **标签命名空间偏窄**：清洗后多数条目只剩部门标签，检索价值下降。后续要么扩大 `SCHEMA.md`
   的领域/类型命名空间（需改权威 SCHEMA），要么把模型的自由词另存一个不参与检索的字段。
2. **上传分类仍是 4 类**（`个人_notes/会议/经验/项目`）：`upload_router.CATEGORIES`、前端下拉、
   `init.sh` 目录树未同步扩展；编译侧不受影响（`scan_new_raw` 遍历 RAW 下所有目录），但 UI 体验不一致。
3. **概念页仍需人工放行**：这是设计边界（不让模型改事实），不做"自动发布"。
4. **L2/L3/L4 未实现**：队列租约、RLS、租户模型配置与用量表仍是设计（§5）。
5. **CLI 引擎保留但未在 CI 覆盖**：需要宿主机 CLI 与登录态，属本地开发路径。

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
