# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**两层结构（定位以 [docs/ARCH-00_项目定位与分层.md](docs/ARCH-00_项目定位与分层.md) 为准）**：

- **知识层（项目主体）**——LLM Wiki 知识平台，基于 Karpathy 编译范式 + Google OKF 规范：入库时把文档编译为结构化 Markdown，而非传统 RAG 每次查询重新检索（详见 `docs/WIKI-00_LLM_Wiki_PRD.md`）。需求唯一来源 WIKI-00。
- **应用层（垂直落地）**——销售客户状态 Agent 生产形态原型（销售洽谈记录 → 证据提取 → 状态建议 → 负责人确认 → 可审计状态事件），配套销售事实澄清 Agent（信息不足时先追问而非猜状态）。需求唯一来源 `docs/SA-01_销售客户状态Agent_业务契约.md`。核心原则：**不让模型直接改客户事实**——Agent 只提建议，`StateEvent` 是事实唯一写入口，`CurrentState` 是可重建投影。
- 应用层复用知识层的 FastAPI/JWT/审计/PG 地基；**知识层不反向依赖应用层**。当前知识库为空骨架（企业业务内容已脱敏移除），知识层作为背景知识保留。

**当前状态**：Phase 2 已交付（SP1 PostgreSQL 迁移 / SP2 FastAPI+JWT 认证 / SP2.5 可观测 / SP3 watcher 全自动编译 / SP4 混合检索 / SP5 健康巡检）；销售 Agent 阶段 0-5 已交付（含 Vue 3 工作台、转人工处置闭环、状态建议规则+模型双引擎）。**pytest 收集 341 个用例** + CI（测试 + Prompt 退化检测 + 检索合成集门禁）+ LLM 输出契约校验。

**快速启动**：`bash init.sh && docker compose up -d`（三容器：`db`=PostgreSQL 16+pgvector、`api`=FastAPI、`web`=Vue 工作台（nginx 托管 + `/api` 反代）；容器启动自愈建目录/表/初始管理员，幂等）。工作台 `:8501`；开发态前端 `cd frontend && npm run dev`（:5173，Vite 代理到 :8000）。知识浏览：工作台「全部条目」页在线预览正文，图谱用 Obsidian 打开 `vault/`。

> ⚠️ **改了代码必须 `docker compose up -d --build`，不是 `up -d`**：`api/`、`core/`、`schema.sql` 与前端构建产物都是 **COPY 进镜像**（非挂载），只重启不会带出新代码——症状是"新接口 404 / 页面没有新按钮"。改 schema 无需手工迁移（`ensure_schema()` 启动时幂等执行 `schema.sql`）；改前端也可用 `npm run dev` 免重建。

**测试**：`python -m pytest tests -q`。需真实 PostgreSQL（`docker compose up -d db`，测试库 `llmwiki_test`）；无 PG 可用 `PYTEST_SKIP_NO_DB=1` 跳过。隔离目录固定为 `tests/_isolated/`（conftest 覆盖 tmp_path，不依赖系统 %TEMP%），在受限沙箱/CI 环境同样可跑。**CI（GitHub Actions，`.github/workflows/ci.yml`）**：push/PR 触发，起 pgvector service 跑全量测试 + `tools/prompt_regression.py` 退化检测。**检索回归门禁分两层**：CI 跑**合成集**（`tests/fixtures/retrieval_kb/` + `tests/fixtures/retrieval_gold.md`，`--check --no-vector`，离线零 key，11 条：精确 6/语义 2/缺口 3，锁缺口判据与 draft 不参与检索）；真实黄金集（docs/VAL-03，基于真实业务内容）是**本地/面试资产、不进公开仓库**，改检索/融合逻辑后本地跑 `python tools/eval_search.py --check`（完整含向量通道；⚠️ 公开仓库的 `vault/` 是空骨架，此命令会明确报「语料为空」而非给出误导性的 Recall=0，公开仓库请跑上面的合成集门禁）。`tests/test_eval_search_gate.py` 另锁"门禁自身真的会拦"（标注写错必须失败）。

## 文档体系（文档驱动开发）

| 文档 | 角色 | 何时读 |
|------|------|--------|
| [docs/ARCH-00_项目定位与分层.md](docs/ARCH-00_项目定位与分层.md) | **项目定位唯一来源**：知识层/应用层分层、层级判据、跨层冲突规则、事实边界、对外表述口径 | 对外介绍、写简历/README、或不确定某功能属于哪一层时 |
| [docs/WIKI-00_LLM_Wiki_PRD.md](docs/WIKI-00_LLM_Wiki_PRD.md) | 知识层需求唯一来源（v1.8） | 知识层需求裁决依据 |
| [docs/WIKI-01_LLM_Wiki_设计文档.md](docs/WIKI-01_LLM_Wiki_设计文档.md) | Demo 详细设计（v0.1） | Demo 机制溯源：目录结构、SQLite DDL、函数签名、触发机制 |
| [docs/WIKI-10_LLM_Wiki_Phase2_路线图.md](docs/WIKI-10_LLM_Wiki_Phase2_路线图.md) | Phase 2 主规划（SP1-SP5） | 进入 Phase 2 工作前 |
| [docs/WIKI-20/30/40/50/60_*_设计文档.md](docs/WIKI-10_LLM_Wiki_Phase2_路线图.md) | 各子项目设计（SP1-SP5） | 对应子项目实现前必读；SP4 含检索评测与缺口判据勘误（v0.1.1） |
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
     ↑ Bash 工具                      ↑ Python
Claude Code（引擎：编译/审核/问答 3 Agent）
watcher（tools/trigger_watcher.py：轮询触发 → headless 唤起 Claude Code，全自动）
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

- **Claude Code 是知识层唯一 LLM 引擎**：通过 Bash 工具直接操作 Vault；watcher 全自动消费触发队列（`claude -p` headless），SessionStart hook `/process-triggers` 为手动兜底。**应用层不依赖它**——销售 Agent 的模型调用走 `core/sales_*` 与 `api/routers/*` 内的调用层（见 SA-13）
- **界面统一为 Vue 3**（`frontend/`，Element Plus）：原 Streamlit 管理台已退役删除；`core/` 只保留被 API 与工具链复用的业务逻辑（数据层/规则/领域模型），不含任何界面代码
- **后端演进**：Demo 期论证"无后端"（单用户、schema 稳定）；SP2 起为认证/审计/向量检索/多用户上 FastAPI REST API + JWT（PyJWT HS256 + argon2），前端经 REST 消费，不直连库
- **PostgreSQL 是缓存不是权威**：YAML Frontmatter 是规范数据源，任何状态变更必须双写（YAML + PG），不一致时文件为准；向量 embedding 同为可重建缓存（backfill 全量重算）。**应用层例外**：销售证据/状态事件的事实权威在 PG（事务 + 状态机约束 + 幂等键），不在 Markdown

## 关键机制

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
- **混合检索（SP4）**：`/search` 双通道 grep+pgvector → 加权融合（0.5/0.3，后续以评测为准）；embedding 故障自动降级 grep-only。**改检索逻辑后必跑 `tools/eval_search.py`**：CI 跑合成集门禁（`--check --no-vector --kb tests/fixtures/retrieval_kb --gold tests/fixtures/retrieval_gold.md`，11 条），本地另跑真实黄金集完整评测（14 条：MRR@10/Recall@10/缺口检出力，含向量通道）
- **LLM 调用可观测（Langfuse 可选）**：应用层模型调用在 `core/model_port.py` 边界上报 Langfuse（`core/llm_observability.py`）——**未配置 `LANGFUSE_*` 时不 import SDK、不发请求**，SDK 异常一律静默（不阻断模型调用）；**默认只上报元数据**（模型/token/延迟/成败），正文外发需显式 `LANGFUSE_SEND_TEXT=1`。`api/trace.py` 每次请求生成 `trace_id` 并写入 `trace_events.trace_id`（此前恒为 NULL），Langfuse trace 与自建 trace 同 id 对账。**知识层编译链路仍是自建埋点**：编译由 headless Claude Code CLI 在外部进程执行，拿不到内部 span，真接需改用 OTel 导出或 API 直调（`docs/WIKI-35` §4.3）
- **SHA256 指纹缓存**：同指纹的 done 记录存在则跳过 LLM 调用，标记 cached
- **LLM 输出契约校验**：prompts 里的 JSON 契约代码化（`core/output_schema.py` + `core/clarification_schema.py`；详见 `docs/VAL-01_LLM_输出校验_设计说明.md`）。质量门禁已进 agent loop：review 写库前 / compile 落盘前先自检（`tools/validate_llm_output.py`，违例重试 1 次、再败不落地）；`/reviews` 响应含 `ai_scores_valid` 标记。**Prompt 退化检测**：`tools/prompt_regression.py`（契约短语存在性 + golden 样例回归，已在 CI）
- **自增长**：搜索缺口写入 search_logs（判据 SP4 v0.1.2 已落地：grep 零命中 且 向量最高相似度 < τ=0.52，τ 由黄金集标定；向量不可用自动退化为 grep 零命中）→ 看板展示缺口 Top 20 → 驱动补文档

## 开发范式（PRD 第八章 + 销售 Agent 实践）

知识层只采用 **SDD**（编译引擎、检索 API、OKF 输出——输入输出可形式化）+ **TDD**（审核确定性规则、去重、健康巡检）；销售层同样 TDD 锁边界（状态转移、预处理拦截、幂等、敏感数值分层、契约 schema）。**不引入** BDD（LLM 输出非确定）和战术 DDD（核心逻辑在 prompt 不在代码）——但销售层采用了**战略侧领域建模**（SA-02 领域对象与事件事实边界，`.claude/skills/domain-modeling` 沉淀该做法）。Harness（Workflow/parallel/pipeline）用于批量编译、六维度并行审核、prompt 退化检测；销售侧评测用 synthetic 回放（`tools/eval_sales_state.py`，36 条）。

## 交流约定

- 与用户中文交流
- 用户偏好文档驱动：先分析再改文档，重要变更记录到文档末尾 changelog
- 所有知识文件名、目录名、Prompt 输出使用中文（企业知识内容）
