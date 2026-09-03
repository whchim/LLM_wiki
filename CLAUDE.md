# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

企业级 LLM Wiki 知识库平台——基于 LLM Wiki 编译范式 + Google OKF 规范的知识流转系统（个人沉淀 → 审核流转 → 企业共享）。核心思想：入库时编译为结构化 Markdown（而非传统 RAG 每次查询重新检索）。

**当前状态**：Demo 已验收通过；Phase 2 主体完成（SP1 PostgreSQL 迁移 / SP2 FastAPI+JWT 认证 / SP2.5 可观测 / SP3 watcher 全自动编译 / SP4 混合检索 / SP5 健康巡检），133 个测试绿 + CI（测试 + 检索回归门禁）+ LLM 输出契约校验。

**快速启动**：`bash init.sh && docker compose up -d`（三容器：`db`=PostgreSQL 16+pgvector、`api`=FastAPI、`streamlit`=管理台；容器启动自愈建目录/表/初始管理员，幂等）。知识浏览用 Obsidian 打开 `vault/`。

**测试**：`python -m pytest tests -q`。需真实 PostgreSQL（`docker compose up -d db`，测试库 `llmwiki_test`）；无 PG 可用 `PYTEST_SKIP_NO_DB=1` 跳过。隔离目录固定为 `tests/_isolated/`（conftest 覆盖 tmp_path，不依赖系统 %TEMP%），在受限沙箱/CI 环境同样可跑。**CI（GitHub Actions，`.github/workflows/ci.yml`）**：push/PR 触发，起 pgvector service 跑全量测试 + `tools/eval_search.py --check` 检索回归门禁（缺口判据 100%、精确召回不回退；无 key 自动 grep 降级模式）。**改检索/融合逻辑后本地必跑 `python tools/eval_search.py --check` 再过 CI。**

## 文档体系（文档驱动开发）

| 文档 | 角色 | 何时读 |
|------|------|--------|
| [docs/LLM_wiki_PRD.md](docs/LLM_wiki_PRD.md) | 需求唯一来源（v1.8） | 一切需求的裁决依据 |
| [docs/LLM_wiki_设计文档.md](docs/LLM_wiki_设计文档.md) | Demo 详细设计（v0.1） | Demo 机制溯源：目录结构、SQLite DDL、函数签名、触发机制 |
| [docs/LLM_wiki_Phase2_路线图.md](docs/LLM_wiki_Phase2_路线图.md) | Phase 2 主规划（SP1-SP5） | 进入 Phase 2 工作前 |
| [docs/LLM_wiki_Phase2_SP\*_设计文档.md](docs/LLM_wiki_Phase2_路线图.md) | 各子项目设计（SP1-SP5） | 对应子项目实现前必读；SP4 含检索评测与缺口判据勘误（v0.1.1） |
| [docs/检索评测_黄金集.md](docs/检索评测_黄金集.md) | 检索离线评测集（14 条） | 改检索/融合逻辑后必跑 `tools/eval_search.py` |
| [prompts/](prompts/) | 3 个 Agent 系统提示词（编译/审核/问答） | 修改 Agent 行为时 |

**规则**：需求冲突时 PRD 为准；设计细节冲突时设计文档为准（其 1.5 节记录与 PRD 的一致性说明）。修改设计前先检查是否影响 PRD，反之亦然。

## 架构大局（Demo 无后端 → Phase 2 加 FastAPI 后端）

```
Obsidian（知识界面+浏览） ←文件系统→ vault/（Markdown，YAML Frontmatter 权威）
     ↑ Bash 工具                      ↑ Python
Claude Code（引擎：编译/审核/问答 3 Agent）
watcher（tools/trigger_watcher.py：轮询触发 → headless 唤起 Claude Code，全自动）
Streamlit（管理台，JWT 登录） ←HTTP→ api/（FastAPI）→ PostgreSQL 16 + pgvector（缓存）
```

- **Claude Code 是唯一 LLM 引擎**：通过 Bash 工具直接操作 Vault；watcher 全自动消费触发队列（`claude -p` headless），SessionStart hook / `/process-triggers` 为手动兜底
- **后端演进**：Demo 期论证"无后端"（单用户、schema 稳定）；SP2 起为认证/审计/向量检索/多用户上 FastAPI REST API + JWT（PyJWT HS256 + argon2），Streamlit 经 `ApiClient` 消费，不再直连库
- **PostgreSQL 是缓存不是权威**：YAML Frontmatter 是规范数据源，任何状态变更必须双写（YAML + PG），不一致时文件为准；向量 embedding 同为可重建缓存（backfill 全量重算）

## 关键机制

- **触发文件信号**：API/Streamlit 写 `vault/_triggers/compile_*.md` / `review_*.md`（原子写：tmp + mv），watcher 轮询消费（headless 唤起 Claude Code），处理后移入 `done/`；失败批处理补偿为 failed，不残留悬挂任务
- **概念页审核流**：编译产物先入 `pending_review/`（status=pending）→ AI 六维度审核（确定性两维正则+代码、模糊四维 LLM）→ 人工在管理台通过/驳回 → 通过后移入 `NEXUS/概念/`（status=active）；资源摘要不过审直接发布
- **混合检索（SP4）**：`/search` 双通道 grep+pgvector → 加权融合（0.5/0.3，后续以评测为准）；embedding 故障自动降级 grep-only。**改检索逻辑后必跑 `tools/eval_search.py`（黄金集 14 条：MRR@10/Recall@10/缺口检出力）**
- **SHA256 指纹缓存**：同指纹的 done 记录存在则跳过 LLM 调用，标记 cached
- **LLM 输出契约校验**：prompts 里的 JSON 契约代码化（`streamlit_app/output_schema.py` 三组校验：审核六维度/编译产物/落盘 frontmatter，含判定一致性；详见 `docs/LLM_输出校验_设计说明.md`）。引擎自检用 `tools/validate_llm_output.py`（review|compile|frontmatter，退出码门禁，utf-8-sig 容错 BOM）；`/reviews` 响应含 `ai_scores_valid` 标记
- **自增长**：搜索缺口写入 search_logs（判据 SP4 v0.1.2 已落地：grep 零命中 且 向量最高相似度 < τ=0.52，τ 由黄金集标定；向量不可用自动退化为 grep 零命中）→ 看板展示缺口 Top 20 → 驱动补文档

## 开发范式（PRD 第八章）

只采用 **SDD**（编译引擎、检索 API、OKF 输出——输入输出可形式化）+ **TDD**（审核确定性规则、去重、健康巡检）。**不引入** BDD（LLM 输出非确定）和战术 DDD（核心逻辑在 prompt 不在代码）。Harness（Workflow/parallel/pipeline）用于批量编译、六维度并行审核、prompt 退化检测。

## 交流约定

- 与用户中文交流
- 用户偏好文档驱动：先分析再改文档，重要变更记录到文档末尾 changelog
- 所有知识文件名、目录名、Prompt 输出使用中文（企业知识内容）
