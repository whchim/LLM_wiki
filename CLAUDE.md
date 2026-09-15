# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**两层结构（定位以 [docs/ARCH-00_项目定位与分层.md](docs/ARCH-00_项目定位与分层.md) 为准）**：

- **知识层（项目主体）**——LLM Wiki 知识平台，基于 Karpathy 编译范式 + Google OKF 规范：入库时把文档编译为结构化 Markdown，而非传统 RAG 每次查询重新检索（详见 `docs/WIKI-00_LLM_Wiki_PRD.md`）。需求唯一来源 WIKI-00。
- **应用层（垂直落地）**——销售客户状态 Agent 生产形态原型（销售洽谈记录 → 证据提取 → 状态建议 → 负责人确认 → 可审计状态事件），配套销售事实澄清 Agent（信息不足时先追问而非猜状态）。需求唯一来源 `docs/SA-01_销售客户状态Agent_业务契约.md`。核心原则：**不让模型直接改客户事实**——Agent 只提建议，`StateEvent` 是事实唯一写入口，`CurrentState` 是可重建投影。
- 应用层复用知识层的 FastAPI/JWT/审计/PG 地基；**知识层不反向依赖应用层**。当前知识库为空骨架（企业业务内容已脱敏移除），知识层作为背景知识保留。

**当前状态**：Phase 2 已交付（SP1 PostgreSQL 迁移 / SP2 FastAPI+JWT 认证 / SP2.5 可观测 / SP3 watcher 全自动编译 / SP4 混合检索 / SP5 健康巡检）；多租户路线图 L2（队列）/L3（RLS）/L3.5（文件分区）/L4（模型配置与用量）与 **L5 应用内问答（对话窗口）+ 知识图谱** 已交付；销售 Agent 阶段 0-5 已交付（含 Vue 3 工作台、转人工处置闭环、状态建议规则+模型双引擎）。**pytest 收集 447 个用例** + CI（测试 + Prompt 退化检测 + 检索合成集门禁）+ LLM 输出契约校验。

**快速启动**：`bash init.sh && docker compose up -d`（三容器：`db`=PostgreSQL 16+pgvector、`api`=FastAPI、`web`=Vue 工作台（nginx 托管 + `/api` 反代）；容器启动自愈建目录/表/初始管理员，幂等）。工作台 `:8501`；开发态前端 `cd frontend && npm run dev`（:5173，Vite 代理到 :8000）。知识浏览：工作台「全部条目」页在线预览正文，图谱用 Obsidian 打开 `vault/`。

> ⚠️ **改了代码必须 `docker compose up -d --build`，不是 `up -d`**：`api/`、`core/`、`schema.sql` 与前端构建产物都是 **COPY 进镜像**（非挂载），只重启不会带出新代码——症状是"新接口 404 / 页面没有新按钮"。改 schema 无需手工迁移（`ensure_schema()` 启动时幂等执行 `schema.sql`）；改前端也可用 `npm run dev` 免重建。

**测试**：`python -m pytest tests -q`。需真实 PostgreSQL（`docker compose up -d db`，测试库 `llmwiki_test`）；无 PG 可用 `PYTEST_SKIP_NO_DB=1` 跳过。隔离目录固定为 `tests/_isolated/`（conftest 覆盖 tmp_path，不依赖系统 %TEMP%），在受限沙箱/CI 环境同样可跑。**CI（GitHub Actions，`.github/workflows/ci.yml`）**：push/PR 触发，起 pgvector service 跑全量测试 + `tools/prompt_regression.py` 退化检测。**检索回归门禁分两层**：CI 跑**合成集**（`tests/fixtures/retrieval_kb/` + `tests/fixtures/retrieval_gold.md`，`--check --no-vector`，离线零 key，11 条：精确 6/语义 2/缺口 3，锁缺口判据与 draft 不参与检索）；真实黄金集（docs/VAL-03，基于真实业务内容）是**本地/面试资产、不进公开仓库**，改检索/融合逻辑后本地跑 `python tools/eval_search.py --check`（完整含向量通道；⚠️ 公开仓库的 `vault/` 是空骨架，此命令会明确报「语料为空」而非给出误导性的 Recall=0，公开仓库请跑上面的合成集门禁）。`tests/test_eval_search_gate.py` 另锁"门禁自身真的会拦"（标注写错必须失败）。

**知识库根可搬（`KB_ROOT` 端到端）**：`KB_ROOT` 同时作用于 **Python 侧**（watcher / API / tools / tests）与 **Claude 侧**（`workflows/*.md` 与 `.claude/commands/*.md` 统一写 `${KB_ROOT:-vault}/...`，不设时行为不变）。因此可以把**真实语料与其编译产物放在仓库外**、公开仓库继续只保留空骨架：
```
# 1) 仓库外建 vault（init.sh 的目录树 + SCHEMA.md），把语料放进 RAW/<分类>/
# 2) 用 docker-compose.override.yml（.gitignore 内）把 api 的 ./vault 换成该目录
api: { volumes: ["D:/path/to/local_vault:/app/vault"] }   # 容器内路径与 KB_ROOT 都不用改
# 3) 宿主机跑 watcher 时带上 KB_ROOT（claude 子进程继承该环境变量）
KB_ROOT=D:\path\to\local_vault python tools/trigger_watcher.py --once
```
`.gitignore` 另有兜底：`vault/RAW|NEXUS|pending_review|_triggers|_suggestions` 一律不入库（公开仓库的 `vault/` 只保留可重建的 `SCHEMA.md`）。

## 文档体系（文档驱动开发）

| 文档 | 角色 | 何时读 |
|------|------|--------|
| [docs/ARCH-00_项目定位与分层.md](docs/ARCH-00_项目定位与分层.md) | **项目定位唯一来源**：知识层/应用层分层、层级判据、跨层冲突规则、事实边界、对外表述口径 | 对外介绍、写简历/README、或不确定某功能属于哪一层时 |
| [docs/WIKI-00_LLM_Wiki_PRD.md](docs/WIKI-00_LLM_Wiki_PRD.md) | 知识层需求唯一来源（v1.8） | 知识层需求裁决依据 |
| [docs/WIKI-01_LLM_Wiki_设计文档.md](docs/WIKI-01_LLM_Wiki_设计文档.md) | Demo 详细设计（v0.1） | Demo 机制溯源：目录结构、SQLite DDL、函数签名、触发机制 |
| [docs/WIKI-10_LLM_Wiki_Phase2_路线图.md](docs/WIKI-10_LLM_Wiki_Phase2_路线图.md) | Phase 2 主规划（SP1-SP5） | 进入 Phase 2 工作前 |
| [docs/WIKI-20/30/40/50/60_*_设计文档.md](docs/WIKI-10_LLM_Wiki_Phase2_路线图.md) | 各子项目设计（SP1-SP5） | 对应子项目实现前必读；SP4 含检索评测与缺口判据勘误（v0.1.1） |
| [docs/WIKI-70_Phase2_应用内编译引擎_设计文档.md](docs/WIKI-70_Phase2_应用内编译引擎_设计文档.md) | **应用内编译引擎（L1）**：为什么不能靠应用外 CLI、双引擎同一契约、**多租户路线图（L2 队列/L3 RLS/L3.5 文件分区/L4 模型配置与用量进库）** | 动编译引擎、`ModelPort`、模型配置或做云端多租户部署前 |
| [docs/WIKI-80_对话窗口_设计文档.md](docs/WIKI-80_对话窗口_设计文档.md) | **应用内问答（L5）**：门禁先于模型（无依据不答）、引用溯源契约、缺口回流、检索原语抽取 | 动 `/ask`、`core/answer_service.py`、`core/retrieval.py`、答案 prompt 或对话页前 |
| [docs/WIKI-90_知识图谱_设计文档.md](docs/WIKI-90_知识图谱_设计文档.md) | **知识图谱**：从 Markdown 派生节点/边（related_to + wikilink）、**待建页面（红链）作为知识缺口**、自研力导向布局 | 动 `/graph`、`core/graph.py` 或图谱页前 |
| [docs/SA-00_销售客户状态Agent_改造计划.md](docs/SA-00_销售客户状态Agent_改造计划.md) | 销售 Agent 阶段计划（阶段 0-5，含确认点） | 动销售 Agent 任何模块前必读 |
| [docs/SA-01_销售客户状态Agent_业务契约.md](docs/SA-01_销售客户状态Agent_业务契约.md) | **销售域需求唯一来源**：范围边界/角色责任/7 状态集合/状态机/验收指标 | 判断一条输入是否在范围内、一条状态更新是否合格 |
| [docs/SA-02_销售客户状态Agent_领域模型.md](docs/SA-02_销售客户状态Agent_领域模型.md) | 领域对象与知识分层（Customer/Evidence/StateProposal/StateDecision/StateEvent/CurrentState） | 改数据层或状态流转前 |
| [docs/SA-03_销售客户状态Agent_生产形态检查清单.md](docs/SA-03_销售客户状态Agent_生产形态检查清单.md) | 生产形态自检项与已知缺口 | 宣称能力边界前 |
| [docs/SA-04_销售客户状态Agent_合成评测报告.md](docs/SA-04_销售客户状态Agent_合成评测报告.md) | 36 条 synthetic 回放结果（拦截率/契约通过率/token） | 对外讲效果时——**不得说成真实业务准确率** |
| [docs/SA-10_销售事实澄清Agent_迭代计划.md](docs/SA-10_销售事实澄清Agent_迭代计划.md) | 澄清 Agent 迭代计划 | 动澄清流程前 |
| [docs/SA-11_销售事实澄清Agent_事实与追问契约.md](docs/SA-11_销售事实澄清Agent_事实与追问契约.md) | 事实槽位与追问规则契约 | 改澄清 prompt 或槽位定义时 |
| [docs/SA-12_销售事实澄清Agent_传统基线.md](docs/SA-12_销售事实澄清Agent_传统基线.md) | 无 Agent 的传统基线对照 | 论证 Agent 增益时 |
| [docs/SA-13_销售事实澄清Agent_模型调用契约.md](docs/SA-13_销售事实澄清Agent_模型调用契约.md) | 模型调用契约（输入输出 schema） | 改模型调用层时 |
| [docs/SA-14_销售事实澄清Agent_角色化工作台.md](docs/SA-14_销售事实澄清Agent_角色化工作台.md) | 角色化工作台设计（销售/负责人视角） | 改页面交互前 |
| [docs/SA-15_销售事实澄清Agent_Vue3工作台.md](docs/SA-15_销售事实澄清Agent_Vue3工作台.md) | Vue 3 工作台范围与边界 | 改 `frontend/` 前 |
| [docs/VAL-01_LLM_输出校验_设计说明.md](docs/VAL-01_LLM_输出校验_设计说明.md) | LLM 输出契约校验设计（三组 schema + 门禁进 loop） | 改 output_schema / 门禁时 |
| [docs/VAL-04_安全审查与修复记录.md](docs/VAL-04_安全审查与修复记录.md) | 安全审查发现与修复记录 | 安全相关改动前 |
| [docs/INT-04_竞品对比_WeKnora.md](docs/INT-04_竞品对比_WeKnora.md) | 与腾讯 WeKnora 的定位/架构对比 + 迁移决策记录（结论：不替换，选择性借鉴） | 技术选型自证、面试叙事"为什么不用现成 RAG 平台"、Phase 3 借鉴清单 |
| [prompts/](prompts/) | 5 个 Agent 系统提示词（编译/审核/问答/销售澄清/销售状态） | 修改 Agent 行为时 |

**规则**：知识层需求冲突时 WIKI-00 为准、设计细节冲突时设计文档为准；**销售域需求冲突时 SA-01 业务契约为准**。修改设计前先检查是否影响上游契约，反之亦然。


## 架构大局（知识层：Demo 无后端 → Phase 2 加 FastAPI 后端 ｜ 销售层：证据-状态事件链）

```
【知识层】
Obsidian（知识界面+浏览） ←文件系统→ vault/（Markdown，YAML Frontmatter 权威）
     ↑                 ↑ Python（应用内引擎，生产路径：compile_worker → compile_service → ModelPort）
Agent 引擎（二选一）   watcher（tools/trigger_watcher.py：轮询触发 → headless 唤起 Claude Code，本地开发路径）
Vue 工作台（nginx 托管 + /api 反代） ←HTTP→ api/（FastAPI）→ PostgreSQL 16 + pgvector（缓存）

【应用层】Vue 工作台同一前端（知识域 + 销售域）─┐
                                                ├─HTTP→ api/（同一 FastAPI，同一套 JWT/审计）
       澄清会话（clarification_sessions/turns/answers）
                          ↓
   evidence（原始证据，不可覆盖）→ state_proposals（AI 建议，带置信度/证据定位）
                          ↓ 负责人确认
   state_decisions → state_events（事实唯一写入口）→ current_states（可重建投影）
                          ↓
   sensitive_numeric_values（金额等精确值独立受限表，正文只留 [AMOUNT_REF:nv-001] 占位符）
```

- **知识层引擎双路径**：**应用内引擎**（`tools/compile_worker.py` → `core/compile_service.py` → `ModelPort`，容器内可跑、可测、可计量，云端生产走这条）与 **Claude Code CLI**（watcher → `claude -p /process-triggers`，自主性更强但依赖宿主机 CLI/登录态，本地开发与开放探索任务走这条）。两者共用同一份契约（`prompts/compile_prompt.md` + `core/output_schema.py`）。**应用层不依赖任何一个**——销售 Agent 的模型调用走 `core/sales_*` 与 `api/routers/*` 内的调用层（见 SA-13）；引擎分层理由与多租户路线图见 `docs/WIKI-70`
- **界面统一为 Vue 3**（`frontend/`，Element Plus）：原 Streamlit 管理台已退役删除；`core/` 只保留被 API 与工具链复用的业务逻辑（数据层/规则/领域模型），不含任何界面代码
- **后端演进**：Demo 期论证"无后端"（单用户、schema 稳定）；SP2 起为认证/审计/向量检索/多用户上 FastAPI REST API + JWT（PyJWT HS256 + argon2），前端经 REST 消费，不直连库
- **PostgreSQL 是缓存不是权威**：YAML Frontmatter 是规范数据源，任何状态变更必须双写（YAML + PG），不一致时文件为准；向量 embedding 同为可重建缓存（backfill 全量重算）。**应用层例外**：销售证据/状态事件的事实权威在 PG（事务 + 状态机约束 + 幂等键），不在 Markdown

## 关键机制

- **编译引擎（双驱动 + 队列，同一契约）**：`COMPILE_ENGINE=api`（默认，**应用内**：`tools/compile_worker.py` → `core/compile_service.py` → `ModelPort`；容器内可跑、可测、可计量、可租户隔离，**云端生产路径**）｜ `claude_cli`（`tools/trigger_watcher.py` → headless Claude Code 消费 `workflows/*.md`；自主性更强但依赖宿主机 CLI/登录态，**本地开发与开放探索任务**）。两者共用 `prompts/compile_prompt.md` + `output_schema` 校验，产出物与落库结果一致。流水线纪律：**指纹幂等**（同路径同指纹已 done → skipped，**重放 0 token**，队列模式也查历史 done 记录）→ **门禁先于模型**（`blocked` 不送模型）→ 模型 → **契约违例回灌重试** → 落盘（资源 `active` / 概念 `pending`）+ 双写 `knowledge_entries` → `compile_session` trace（detail 带 `engine`）。`_write_markdown` 写前校验 frontmatter，不合契约直接 failed 不落盘。设计见 `docs/WIKI-70`
- **模型配置进库 + 用量配额（L4）**：新增 `tenant_model_configs`（`tenant_id+purpose` 唯一，密钥 **AES-GCM 加密**落库、AAD 绑定 `tenant:purpose`）与 `llm_usage`（每次调用记账，进 RLS）。`model_port.for_tenant(purpose=…)` **先查库、查不到回落环境变量**（精确 purpose → tenant default → env；两者都没有才返回 `None`），用途标签已接：编译 `compile` / 审核 `review` / 状态建议 `state` → "审核用便宜模型、编译用强模型"只是配置问题。`ModelPort.complete()` 前后记账（**失败也记**，统计错误率）并在调用前做**配额拦截**（超额直接报错，不静默降级；记账失败只告警不阻断）。管理接口 `GET/PUT/DELETE /admin/model-configs`（**永不返回密钥明文**，`api_key` 省略=保留原密钥、空串=清空）与 `GET /admin/llm-usage`
- **多租户隔离（L3，数据库强制）**：21 张业务表 + `users` 加 `tenant_id`，默认值**动态取当前租户** `current_setting('app.tenant_id')`（INSERT 不必改写）；每表 `ENABLE`+**`FORCE ROW LEVEL SECURITY`**，策略 `USING/WITH CHECK` 保证跨租户**读不可见、写被拒**。上下文注入：`db.get_conn()` 每次 checkout 设 `app.tenant_id`（连接池复用，必须每次重设），worker/脚本用 `db.bind_tenant()`；HTTP 请求由**中间件**按 JWT 的 `tenant` claim 绑定（**不能写在 FastAPI 依赖里**——同步依赖与同步端点各自在线程池执行、contextvars 不互相传递；同一个坑也修掉了 Langfuse 的 trace_id 关联）。⚠️ **超级用户绕过 RLS**：Docker 默认 `POSTGRES_USER` 是超级用户（`rolbypassrls=t`），生产/验收须用受限角色 `llmwiki_app`（`docker/initdb/20-app-role`，只给 DML 权限）；`users` 表豁免 RLS（登录要先能跨租户查身份）。测试见 `tests/test_tenant_isolation.py`（应用层上下文 + 受限角色下的 RLS 读写隔离；缺角色时明确 skip 而非假绿）
- **文件分区（L3.5，`core/paths.py`）**：库隔离之外文件也要隔离——`kb_root()` 跟随租户上下文，**默认租户 = `KB_ROOT` 本身**（本地无感）、其他租户 = `<KB_ROOT>/tenants/<id>/`；租户 id 过白名单 `^[A-Za-z0-9_-]{1,64}$`（拒 `../` 穿越，非法直接抛错不清洗），空串/None = 默认租户（与 `db.bind_tenant` 同口径）。接入 compile/review 服务、`ops`、上传/检索/审核/管理路由、`db.rebuild_index`。**两个实测抓出的真缺陷**：① 编译去重只按 (raw_path, fingerprint)——两租户同名同内容文件会被误判 `cached` 而**永不编译**，现 `find_done_compile_task`/`latest_compile_task`/`queue_stats`/`list_recent_compile_tasks` 全部显式带 `tenant_id`；② `knowledge_entries` 主键由 `path` 改 **`(tenant_id, path)`**（否则 B 租户同名条目 `ON CONFLICT (path) DO UPDATE` 跨租户覆盖），`contributors` 外键随之改**复合外键**。⚠️ **不靠 RLS 兜底**：`db.rls_enforced()` 诚实报告当前角色是否真受 RLS 约束（超级用户/`BYPASSRLS` → `False`，`ensure_schema()` 启动告警）——本地 Docker 就是超级用户，本地多租户实际只有应用层显式过滤这一层。测试 `tests/test_tenant_paths.py`（14 例，含"两租户同名同内容文件各自编译且落库各一行"）
- **任务队列（L2，可并发认领）**：`compile_tasks` 已升级为真队列——`tenant_id`（默认取**当前租户上下文**）/ `lease_until`+`leased_by`（**租约**：worker 崩溃后过期即自动回到可认领态）/ `attempts`+`next_retry_at`（**指数退避**）/ `priority`（交互式上传 10 先于批量 100），时间戳为 `timestamptz`，认领走 **`FOR UPDATE SKIP LOCKED`**（`db.claim_compile_task`）。`tools/compile_worker.py --queue` 消费（失败 1/2/4 分钟退避，attempts 用尽才落终态）；`/uploads` **直接入队**，**触发纸条只在 `claude_cli` 引擎下写**（两边同时消费会重复编译）；`/uploads/tasks/{id}/retry` 对应用内引擎走 `requeue`。`db.queue_stats()` 给各状态计数与最老待处理年龄
- **审核引擎（同样下沉，规则/模型分工更明确）**：`core/review_service.py` + `tools/review_worker.py`（`REVIEW_ENGINE=api|claude_cli`）——完整性/敏感信息**两维由代码判**（`rules.*`；`blocked` 时不送模型），去重/职务归属/质量/合规**四维交模型**（`prompts/review_prompt.md`），**verdict 由代码按判定逻辑链计算**（模型自报只作对照，不一致时记 concern 并留档 `verdict_model`）；落库形状与契约一致（工作台读 `ai_scores.scores.<维度>`，`review_router` 判 `ai_scores_valid`）。**人工作业不变**：通过/驳回走 `/reviews/{id}/approve|reject` → `ops.approve_entry` 移文件 + 双写，模型永不直接发布条目
- **规则/模型分工**：确定性可断言的部分交程序（完整性/敏感信息正则、金额阈值、状态转移约束、权限），模糊语义交模型（质量/合规/去重/职务归属、事实抽取与追问）
- **销售状态机**：7 状态（`new_lead`/`contacted`/`need_confirmed`/`solution_eval`/`commercial_negotiation`/`won`/`lost_or_paused`），每状态有最低证据要求与默认有效期；证据不足必须输出 `needs_review` 不得猜测；`won` 需强证据；撤回/更正/过期均**追加新事件**，不删除历史
- **澄清优先于猜测**：销售事实澄清 Agent 在信息不足时先按 SA-11 契约追问（槽位 + 追问规则），而不是直接给状态建议
- **转人工闭环**：澄清会话转人工后不再"看得见动不了"——reviewer/admin 可 `POST /clarifications/sessions/{id}/resolve`：`closed`（关闭，原因必填 → `cancelled` + `resolution_note`）或 `reopened`（补充事实后重开继续）。**轮次口径（方案 A）**：`max_rounds` = 最多**追问**轮数（2），用尽后仍允许**一次收尾判定**（只出结论、服务端强制 `conclusion_coerced`），Agent 运行上限 = `max_rounds + 1`；**最后一轮追问生成后会话保持 open（可答，不得"问了不给答"）**；人工**不加轮次**（`round_count > max_rounds` 只能关闭）；人工处置**只动会话与回答，不改客户状态**；另：上一轮问题未答完时 `advance` 不重复推进（防重复触发烧轮次）
- **状态建议生成（最后一公里）**：`POST /clarifications/sessions/{id}/proposal` 从**两种**会话状态生成建议——`ready_for_proposal`（澄清完成）与 `needs_human_review`（转人工，强制 `decision='needs_review'`、置信度 ≤0.5，不把"没判出来"包装成"建议推进"）→ `state_proposals(pending)` → 负责人在「客户状态」确认 → `state_events`。**判定引擎（mode）**：`auto`（默认）/`rules`/`llm`——先跑**确定性规则**（`core/sales_state_rules.py`：命中 SA-01 最低证据 + 状态机**逐级推进不跳跃**；证据不足/信号冲突标 `needs_review`，不猜状态），**规则能给出 `propose` 就不调模型**（省成本），只有判不出才请**状态 Agent 复核**（`core/sales_state_agent.py` + `prompts/sales_state_prompt.md`：模型给的证据偏移不可信，服务端按 quote 重定位；契约失败**把拒绝原因回灌给模型**重试 1 次——真模型实测会在"无当前阶段"时跳跃到 `need_confirmed`，故 prompt 内置**合法下一步对照表**，再败转人工）；`mode=llm` 模型不可用直接 409，`auto` 下失败**回退规则**并在响应标 `used`/`llm_error`。服务层 `core/state_proposal_service.py` 保证幂等（同洽谈已有 pending 则复用）。**只生成建议，绝不改客户状态**；`open`（仍在澄清）拒绝生成；关闭会话≠交付负责人（交接必须靠生成建议）；**交接与结果都可见**：会话详情报 `pending_proposal`（工作台显示"已交负责人"），客户总览 `GET /customer-states/customers`（阶段 + 待确认数，确认后立即体现）
- **提交一次洽谈 = 一个会话（内容级幂等）**：`POST /clarifications/intake` 按"同客户 + 相同正文 sha256"复用原洽谈与会话（响应带 `duplicate`，索引 `idx_evidence_content_hash`）——因为前端每次读文件都换 `idempotency_key`，**幂等键防不了"同一份内容再提交一次"**，不拦就会在同一客户下堆出一堆看起来一样的会话（用户实测问题）；确实要再谈一次须显式 `force_new=true`。只复用自己（或审核角色可见）的记录（内容相同 ≠ 可读他人会话），已归档的不复用。`GET /clarifications/mine` 带 `content_preview`/`source_ref`，工作台列表按「客户（第 N 次洽谈）/ 纪要摘要+来源 / 提交时间 / 状态 / 轮次」展示，让同一客户的多条记录可分辨
- **删除与归档**：澄清会话/状态建议的删除是**软删除（归档）+ 仅管理员**（`DELETE /clarifications/sessions/{id}`、`POST .../restore`；写 `deleted_at`/`deleted_by`，进审计）；归档范围含会话产生的建议，读取路径默认排除已归档。⚠️ **`state_events`/`state_decisions` 永不删除**——客户事实只能追加更正/撤回/过期（SA-02），归档不动事实链（有测试锁定）
- **敏感数值分层**：正文占位符 + `sensitive_numeric_values` 受限表；Prompt/trace/普通日志/向量索引中不得出现精确金额；授权角色在审计下可恢复
- **触发文件信号**：API/工作台写 `vault/_triggers/compile_*.md` / `review_*.md`（原子写：tmp + mv），watcher 轮询消费（headless 唤起 Claude Code），处理后移入 `done/`；失败批处理补偿为 failed，不残留悬挂任务
- **概念页审核流**：编译产物先入 `pending_review/`（status=pending）→ AI 六维度审核（确定性两维正则+代码、模糊四维 LLM）→ 人工在工作台通过/驳回 → 通过后移入 `NEXUS/概念/`（status=active）；资源摘要不过审直接发布
- **混合检索（SP4）**：`/search` 双通道 grep+pgvector → 加权融合（0.5/0.3，后续以评测为准）；embedding 故障自动降级 grep-only。**改检索逻辑后必跑 `tools/eval_search.py`**：CI 跑合成集门禁（`--check --no-vector --kb tests/fixtures/retrieval_kb --gold tests/fixtures/retrieval_gold.md`，11 条），本地另跑真实黄金集完整评测（14 条：MRR@10/Recall@10/缺口检出力，含向量通道）。**检索原语的唯一实现已抽到 `core/retrieval.py`**（`search_router` 保留 `_grep/_vector_search/_fuse` 模块级别名，工具与测试零改动），问答与评测复用同一套
- **应用内问答（L5，对话窗口）**：`POST /ask`（`api/routers/ask_router.py`，登录即可用）→ `core/answer_service.py`。五条纪律：① **门禁先于模型**——检索零命中**一次模型都不调**，直接答"知识库暂无"（最大的可信度风险是模型用预训练知识冒充企业知识）；② **引用可溯源**——`citations[].path` 必须来自本次检索结果、`quote` 必须在条目正文里**逐字出现**（`output_schema.validate_answer_output` 代码判定，不信模型自报）；③ 违例**回灌重试 1 次**；④ 两次违例 → `status=failed` + `contract_ok=false`，**不返回答案**，模型未配置 → **409**（不静默降级、不给占位答案）；⑤ **缺口回流**——没答出来（`no_hits`/`failed`/`insufficient`）记 `search_logs.match_count=0`（`source='ask'`），进自增长看板。模型走 `for_tenant(purpose="answer")`（租户可配便宜模型、受配额拦截），trace 写 `ask` span（可观测页「对话问答」）。前端「对话窗口」页：引用卡片点开原文、检索依据折叠、缺口与失败**照实展示**。设计见 `docs/WIKI-80`
- **知识图谱（`core/graph.py` + `/graph`）**：从已编译 Markdown **派生**（不新增权威表：文件仍权威，图谱是可重建视图）。节点 = `NEXUS/**/*.md`（`include_pending=true` 才带 `pending_review/`），边 = frontmatter `related_to` + 正文 `[[wikilink]]` + `[文字](路径.md)`；解析顺序：目录相对 → 根相对 → 标题/主干 → **类型前缀**（`[[概念-新质生产力]]`）→ 末段兜底（真机语料逼出来的四层，见 `docs/WIKI-90` §7）。**被引用但解析不到的 = 待建页面（红链）**，带引用次数与来源——比"搜不到"更精确的知识缺口，是真机验收直接点出的补文档清单。`index.md`/`log.md`/`SCHEMA.md` 不入图（否则索引会成为度数最高的超级节点并造出假缺口）；外链与自链接不进图；`max_nodes` 安全阀超限时 `truncated=true` **如实标注**；按租户分区。前端「知识图谱」页用**自研 SVG 力导向布局**（无第三方依赖、哈希确定性初值），悬停高亮邻域、点节点看原文、右侧列待建页面与孤立条目
- **LLM 调用可观测（Langfuse 可选）**：**应用内引擎与销售 Agent 的模型调用都走 `core/model_port.py`**，在调用边界上报 Langfuse（`core/llm_observability.py`）——**未配置 `LANGFUSE_*` 时不 import SDK、不发请求**，SDK 异常一律静默（不阻断模型调用）；**默认只上报元数据**（模型/token/延迟/成败），正文外发需显式 `LANGFUSE_SEND_TEXT=1`。`api/main.py` 的**请求中间件**生成 `trace_id`、写入 `trace_events.trace_id`（响应头带 `X-Trace-Id`），Langfuse trace 与自建 trace 同 id 对账。**只有 CLI 驱动那条路**拿不到内部 span（外部进程），仍靠自建 `compile_session` trace（detail 带 `engine` 可区分）；真接需 OTel 导出或改用应用内引擎（`docs/WIKI-35` §4.3、`docs/WIKI-70`）
- **SHA256 指纹缓存**：同指纹的 done 记录存在则跳过 LLM 调用，标记 cached
- **LLM 输出契约校验**：prompts 里的 JSON 契约代码化（`core/output_schema.py` + `core/clarification_schema.py`；详见 `docs/VAL-01_LLM_输出校验_设计说明.md`）。质量门禁已进 agent loop：review 写库前 / compile 落盘前先自检（`tools/validate_llm_output.py`，违例重试 1 次、再败不落地）；`/reviews` 响应含 `ai_scores_valid` 标记。**Prompt 退化检测**：`tools/prompt_regression.py`（契约短语存在性 + golden 样例回归，已在 CI）
- **自增长**：搜索缺口写入 search_logs（判据 SP4 v0.1.2 已落地：grep 零命中 且 向量最高相似度 < τ=0.52，τ 由黄金集标定；向量不可用自动退化为 grep 零命中）→ 看板展示缺口 Top 20 → 驱动补文档

## 开发范式（PRD 第八章 + 销售 Agent 实践）

知识层只采用 **SDD**（编译引擎、检索 API、OKF 输出——输入输出可形式化）+ **TDD**（审核确定性规则、去重、健康巡检）；销售层同样 TDD 锁边界（状态转移、预处理拦截、幂等、敏感数值分层、契约 schema）。**不引入** BDD（LLM 输出非确定）和战术 DDD（核心逻辑在 prompt 不在代码）——但销售层采用了**战略侧领域建模**（SA-02 领域对象与事件事实边界，`.claude/skills/domain-modeling` 沉淀该做法）。Harness（Workflow/parallel/pipeline）用于批量编译、六维度并行审核、prompt 退化检测；销售侧评测用 synthetic 回放（`tools/eval_sales_state.py`，36 条）。

## 交流约定

- 与用户中文交流
- 用户偏好文档驱动：先分析再改文档，重要变更记录到文档末尾 changelog
- 所有知识文件名、目录名、Prompt 输出使用中文（企业知识内容）
