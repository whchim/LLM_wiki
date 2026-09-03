# 企业级 LLM Wiki 知识库平台

> **个人沉淀 → 审核流转 → 企业共享** — 基于 LLM Wiki 编译范式 + Google OKF 规范的企业知识流转系统。
> 入库时把原始文档编译为结构化 Markdown，替代传统 RAG"每次查询重新检索"。

[![tests](https://img.shields.io/badge/tests-66%20passed-green)]()
[![status](https://img.shields.io/badge/status-Phase%202%20(SP1%2FSP2%20done)-brightgreen)]()
[![phase](https://img.shields.io/badge/phase-SP1%20%E6%95%B0%E6%8D%AE%E5%9C%B0%E5%9F%BA%20%2F%20SP2%20API%E4%B8%8E%E5%AE%89%E5%85%A8-blue)]()

---

## 为什么不是又一个 RAG 知识库？

传统 RAG 是**解释器模式**：查询时才理解文档，每次重复检索、产物是不可读的向量 chunk、知识难以流转。

本项目采用 Karpathy 提出的 **LLM Wiki 编译范式**（**编译器模式**）：在**入库时**由 LLM 把原始文档编译为**结构化、可链接、可持续演进**的 Markdown 知识条目，查询时直接读编译产物，并对齐 Google **OKF v0.1** 规范（Just Markdown + YAML Frontmatter + Reserved Files）。

> 不是"不需要 RAG"，而是范式升级——**Compile-time + Run-time RAG**（编译时理解 + 运行时检索生成）。检索物从向量 chunk 变成人类可读、可追溯的结构化知识。

**核心闭环**：`上传文档 → AI 编译 → 审核流转 → 自然语言检索 → 搜索缺口反馈 → 驱动补文档`（自增长）。

---

## 核心架构（Phase 2：三容器 + LLM 引擎）

```
┌─────────────────────────────────────────────────────────────────────┐
│  界面层 · Obsidian Desktop（知识浏览/图谱/wikilink 导航）             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 文件系统直接读写
┌──────────────────────────────▼──────────────────────────────────────┐
│  引擎层 · Claude Code（编译/审核/问答 Agent + Harness）               │
│  经 Bash 工具直操作 Vault（grep/cat/重定向）＋消费触发文件             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ _triggers/ 触发文件（API 写 · Agent 消费）
┌──────────────────────────────▼──────────────────────────────────────┐
│  服务层（Docker Compose 三容器）                                     │
│  ┌──────────┐   HTTP/JWT    ┌──────────┐   psycopg3   ┌──────────┐  │
│  │ Streamlit│ ────────────► │ FastAPI  │ ───────────► │PostgreSQL│  │
│  │   :8501  │   Bearer Token│   :8000  │   连接池     │16+pgvector│  │
│  └──────────┘               └──────────┘              └──────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 共享卷 ./vault 挂载（上传落盘/只读预览）
┌──────────────────────────────▼──────────────────────────────────────┐
│  存储层 · vault/（Markdown + YAML Frontmatter，规范源）+ PG 缓存      │
└─────────────────────────────────────────────────────────────────────┘
```

（📐 **交互式架构图**（Archify 生成，可缩放/主题切换/导出）：[llm-wiki-runtime.html](docs/diagrams/llm-wiki-runtime.html) ｜ 源规范 [llm-wiki-runtime.candidate.json](docs/diagrams/llm-wiki-runtime.candidate.json)）

（PlantUML 源图：[architecture.puml](docs/diagrams/architecture.puml)，知识流转闭环：[flow.puml](docs/diagrams/flow.puml)）

**三个关键设计决策**：

1. **Claude Code 掌 LLM，FastAPI 掌数据** — 编译/审核/问答仍由 Claude Code（Bash 直操作 Vault + `_triggers/` 触发文件消费），**不重造 LLM 调用与 Agent 编排**；FastAPI 接管数据访问层（REST API 化上传/审核/搜索/管理 + JWT 认证 + 审计日志），Streamlit 管理台经 API 消费，不再直连数据库写操作。
2. **PostgreSQL 是缓存，不是权威** — YAML Frontmatter 是规范数据源；任何状态变更双写；不一致时以文件为准；`rebuild_index()` 可从文件全量重建缓存（pgvector 向量列同为可重建缓存，SP4 用）。
3. **触发文件消息队列** — API/Streamlit 写 `vault/_triggers/compile_*.md`（原子写 tmp+mv）作为异步信号；Claude Code 经 SessionStart hook / `/process-triggers` 消费，实现"管理台/API ↔ LLM 引擎"解耦。

### 程序与模型的分工

同一审核流程内，**规则明确的部分交给程序、拿不准的部分交给模型**：

| 维度 | 实现 | 可靠性 |
|------|------|--------|
| 完整性 / 敏感信息 | 确定性规则（`rules.py` 正则 + 测试锁边界） | 100% 可断言 |
| 质量 / 合规 / 去重 / 职务归属 | LLM 六维度评分 | prompt 约束 + 人工复核兜底 |

### 安全（SP2）

- **JWT 认证**：HS256（12h 过期），角色 `admin / reviewer / user`；越权访问 403，未登录 401
- **审计日志**：写操作（上传/审核通过/驳回/重提/重建索引/重试）逐条落 `audit_logs`，失败不阻断主操作
- **密码**：argon2 哈希，防用户枚举（401 统一文案）

---

## 快速开始（clone 后即可运行）

自愈设计：应用启动时自动建表建目录、创建初始管理员，**无需预置数据库**。

```bash
git clone git@github.com:whchim/LLM_wiki.git && cd LLM_wiki

# 方式 A：Docker（推荐）
docker compose up -d          # 启动 PostgreSQL + FastAPI API + Streamlit 管理台
# 打开 http://localhost:8501 → 登录（默认 admin / admin123，建议首次登录后修改）

# 方式 B：本地 Python（可选）
bash init.sh                  # 初始化 Vault 目录树 + 建表（幂等）
pip install -r requirements.txt
uvicorn api.main:app --port 8000 &   # 先起 API（需 PostgreSQL 在 5432）
streamlit run streamlit_app/app.py
```

**服务端口**：Streamlit 管理台 `:8501` ｜ FastAPI REST API `:8000`（交互文档 `/docs`）｜ PostgreSQL `:5432`。

**默认账号**：`admin / admin123`（环境变量 `ADMIN_INIT_USER/ADMIN_INIT_PASS` 可改）。

**知识浏览**：用 [Obsidian](https://obsidian.md/) 打开 `vault/` 目录，即可看到编译产物的图谱、wikilink 导航、反向链接。

> clone 后知识索引初始为空。沿下方"核心闭环"走一遍上传→编译→审核流程，知识库即开始增长；索引当前规模见"真实数据验收"节。

**LLM 引擎**：两种运行方式——

```bash
# 方式 1（推荐）：触发文件 Watcher 常驻后台，上传后全自动编译（零人工）
tools\watcher_start.cmd        # 双击启动；或放入 shell:startup 开机自启
# 原理：轮询 vault/_triggers/，发现纸条自动唤起 `claude -p "/process-triggers"`（headless）

# 方式 2（手动）：在项目根目录运行 Claude Code，键入 /process-triggers 处理队列
```

> Watcher 需要本机已安装 Claude Code（编译必须由 LLM 引擎执行，这是架构原则）；日志见 `tools/watcher.log`。**运行前置**：Claude Code 的 LLM 通道必须可用（如 `ANTHROPIC_BASE_URL` 指向本地代理服务，需保证该服务已启动——watcher 内置预检，通道不通会在唤起前明确报错而不是白跑几分钟）。安全说明：headless 无人值守默认 `--permission-mode bypassPermissions`，个人机可接受；对外部署建议改为 `--allowedTools` 白名单（见 `tools/trigger_watcher.py` 头注）。

> 跑测试：`docker compose up -d db` 后 `python -m pytest tests -q`（无 PG 时设 `PYTEST_SKIP_NO_DB=1` 跳过）

---

## 核心闭环（个人沉淀 → 审核流转 → 企业共享 → 自增长）

```
上传 → 编译（指纹缓存） → 资源直接发布 / 概念进审核 → 六维度审核
     → 人工放行 → 检索问答 → 缺口记录 → 看板 Top 20 → 补文档 → 闭环
```

---

## 真实数据验收（2026-08，历史记录）

用 **5 份真实企业产品文档**完成端到端走查（上传 → AI 编译 → 六维度审核 → 人工放行 → 检索验证）：

| 步骤 | 结果 |
|------|------|
| AI 编译 | 5 份文档 → **5 篇资源摘要 + 21 个概念页** |
| 审核流转 | 确定性规则（完整性/敏感信息）+ LLM 六维度 → 21/21 approved |
| 人工放行 | 全部移入 `NEXUS/概念/`，文件/YAML/PostgreSQL/index.md 四态一致 |
| 知识检索 | 跨文档概念自动互链，Obsidian 图谱可见；检索评测融合 MRR@10=1.00 |

> **2026-09 说明**：上述验收用企业真实业务文档完成，作为系统能力证据后，**企业业务内容已全部移除**（含 Git 历史重写清洗）；知识库现为空骨架（`init.sh` 可重建），待注入客户自有/脱敏内容。检索评测黄金集转为**本地面试资产**（不进公开仓库，本地配合 `tools/eval_search.py` 使用）。

---

## 工程实践（文档驱动 + SDD/TDD）

这不是"代码写完了补文档"——需求、设计、实施三件套先行（全部在 `docs/`）：

| 文档 | 内容 |
|------|------|
| [`docs/LLM_wiki_PRD.md`](docs/LLM_wiki_PRD.md) | 需求唯一来源 v1.8：4 类角色 / 6 大模块 / 迭代路线图 / 错误 UX 文案 |
| [`docs/LLM_wiki_设计文档.md`](docs/LLM_wiki_设计文档.md) | Demo 详细设计 v0.1：目录结构 / SQLite DDL / 函数签名 / Agent 契约 / 触发机制 |
| [`docs/LLM_wiki_Phase2_路线图.md`](docs/LLM_wiki_Phase2_路线图.md) | Phase 2 主规划：SP1-SP5 拆分 / 排期 / 架构决策 / 退出标准 |
| [`docs/LLM_wiki_Phase2_SP1_设计文档.md`](docs/LLM_wiki_Phase2_SP1_设计文档.md) | SP1 数据地基：PostgreSQL 迁移 + pgvector（已交付）|
| [`docs/LLM_wiki_Phase2_SP2_设计文档.md`](docs/LLM_wiki_Phase2_SP2_设计文档.md) | SP2 API 与安全：FastAPI + JWT + 审计（已交付）|

- **发现并修正 3 处 PRD 内部不一致**（架构层数、MCP Server 取舍、编译触发机制）
- **开发范式收敛**：SDD（编译产物/检索，输入输出可形式化）+ TDD（审核确定性规则/数据层/API）；LLM 输出非确定部分明确不做 BDD
- **66 个 pytest 用例**：DDL 幂等、双写一致性、审核规则边界（中文紧邻漏报/金额阈值）、上传批处理补偿、驳回重提流程、索引重建鲁棒性、JWT 鉴权与越权、审计落库、搜索缺口聚合、启动自愈

---

## 目录结构

```
├── api/                  # FastAPI 后端（main/auth/audit/schemas + routers/）
├── streamlit_app/        # 管理台（app/upload/review/growth + login/api_client + db/ops/rules）
├── vault/                # Obsidian 知识库根目录（Markdown 权威存储）
│   ├── RAW/              # 原始文档（个人_notes/会议/经验/项目）
│   ├── pending_review/   # 待审核概念页
│   ├── NEXUS/            # 编译产物（资源摘要/概念页/研究 + index/log）
│   └── _triggers/        # 触发文件消息队列（+ done/ 归档）
├── docs/                 # 文档体系（PRD/设计/实施/Phase2 规划 + diagrams）
├── workflows/            # 3 个 Agent 编排（compile/review/growth）
├── prompts/              # 3 个 Agent 系统提示词
├── tools/                # 一次性数据迁移脚本（migrate_to_pg.py）
├── .claude/              # hook + /process-triggers、/ask 命令 + skills/
├── schema.sql            # PostgreSQL DDL（幂等：7 业务表 + pgvector + users）
├── init.sh               # 幂等初始化（目录树 + 建表 + SCHEMA.md）
└── tests/                # 66 个 pytest 用例（连真实 PostgreSQL 隔离库）
```

---

## 演进状态（Phase 2 路线图）

| 子项目 | 内容 | 状态 |
|--------|------|------|
| **SP1 数据地基** | SQLite → PostgreSQL 16 + pgvector，7 表 + users，迁移脚本 | ✅ 已交付（`886b2f6`） |
| **SP2 API 与安全** | FastAPI REST + JWT 认证 + 审计日志，Streamlit 接入 | ✅ 已交付（`d174fa1`/`2cd5881`） |
| SP3 增量编译 | file watcher 监听 RAW 变更 + compile_tasks 断点续跑 | ⏳ 待开发 |
| SP4 混合检索 | grep 精确 + pgvector 向量双通道 + 权重 re-rank（P99 < 3s @ 1 万条） | ⏳ 待开发 |
| SP5 知识智能 | 健康巡检 + 周报、知识演进（版本自增）、关联涌现、缺口判据重定义 | ⏳ 待开发 |

- **Phase 3 规划**：知识图谱（实体/关系抽取）、多租户 RBAC、外部源感知、分布式编译、生产级部署（详见 PRD）

---

## 相关资源

- [Karpathy · LLM Wiki Gist](https://gist.github.com/karpathy/90f50cd5cbf126f36bde3a39d67d2431) — LLM Wiki 编译范式原始理念
- **Google OKF（Open Knowledge Format）v0.1** — 知识文件标准化规范（Just Markdown + YAML Frontmatter + Reserved Files + 容错消费），本项目严格对齐