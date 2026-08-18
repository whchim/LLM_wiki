# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

企业级 LLM Wiki 知识库平台——基于 LLM Wiki 编译范式 + Google OKF 规范的知识流转系统（个人沉淀 → 审核流转 → 企业共享）。核心思想：入库时编译为结构化 Markdown（而非传统 RAG 每次查询重新检索）。

**当前状态**：Demo 已实现并验收通过（35/35 测试绿）。

**快速启动**：`bash init.sh && docker compose up -d`（初始化数据库并启动 Streamlit 管理台）；知识浏览用 Obsidian 打开 `vault/`。

**测试**：`python -m pytest tests -q`。隔离目录固定为 `tests/_isolated/`（conftest 覆盖 tmp_path，不依赖系统 %TEMP%），在受限沙箱/CI 环境同样可跑。

## 文档体系（文档驱动开发）

| 文档 | 角色 | 何时读 |
|------|------|--------|
| [LLM_wiki_PRD.md](LLM_wiki_PRD.md) | 需求唯一来源（v1.7） | 一切需求的裁决依据 |
| [LLM_wiki_设计文档.md](LLM_wiki_设计文档.md) | 可直接编码的详细设计（v0.1） | 实现前必读，含目录结构、SQLite DDL、函数签名、触发机制 |
| [prompts/](prompts/) | 3 个 Agent 系统提示词（编译/审核/问答） | 修改 Agent 行为时 |

**规则**：需求冲突时 PRD 为准；设计细节冲突时设计文档为准（其 1.5 节记录与 PRD 的一致性说明）。修改设计前先检查是否影响 PRD，反之亦然。

## 架构大局（4 层）

```
Obsidian（知识界面+存储） ←文件系统→ vault/（Markdown + meta.db）
     ↑ Bash 工具                     ↑ Python
Claude Code（引擎：编译/审核/问答 Agent + Harness）  ←HTTP→  Streamlit（管理层：上传/审核/看板）
```

- **Claude Code 是唯一 LLM 引擎**：通过 Bash 工具（grep/cat/sqlite3/文件重定向）直接操作 Vault，不经过任何中间服务
- **无后端服务**：Demo 无 FastAPI/MCP Server；Streamlit 直接读写文件系统和 SQLite
- **SQLite 是缓存不是权威**：YAML Frontmatter 是规范数据源，任何状态变更必须双写（YAML + SQLite），不一致时文件为准

## 关键机制

- **触发文件信号**：Streamlit 写 `vault/_triggers/compile_*.md` / `review_*.md`（原子写：tmp + mv），Claude Code 经 SessionStart hook 或 `/process-triggers` 消费，处理后移入 `done/`
- **概念页审核流**：编译产物先入 `pending_review/`（status=pending）→ AI 六维度审核 → 人工在 Streamlit 通过/驳回 → 通过后移入 `NEXUS/概念/`（status=active）；资源摘要不过审直接发布
- **SHA256 指纹缓存**：同指纹的 done 记录存在则跳过 LLM 调用，标记 cached
- **自增长**：搜索未命中（match_count=0）写入 search_logs → 看板展示缺口 Top 20 → 驱动补文档

## 开发范式（PRD 第八章）

只采用 **SDD**（编译引擎、检索 API、OKF 输出——输入输出可形式化）+ **TDD**（审核确定性规则、去重、健康巡检）。**不引入** BDD（LLM 输出非确定）和战术 DDD（核心逻辑在 prompt 不在代码）。Harness（Workflow/parallel/pipeline）用于批量编译、六维度并行审核、prompt 退化检测。

## 交流约定

- 与用户中文交流
- 用户偏好文档驱动：先分析再改文档，重要变更记录到文档末尾 changelog
- 所有知识文件名、目录名、Prompt 输出使用中文（企业知识内容）
