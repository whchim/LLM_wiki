# LLM Wiki 知识平台（编译范式 + OKF 规范）

> **上传文档 → AI 编译为结构化知识 → 六维审核 → 人工放行 → 混合检索 → 缺口自增长**。
> 主体是通用的知识编译与治理层；其上落地了一个垂直应用——**销售客户状态 Agent**（见下方"垂直落地案例"节）。
> 项目对外定位与分层规则以 [`docs/ARCH-00_项目定位与分层.md`](docs/ARCH-00_项目定位与分层.md) 为准。

> 本项目是**生产形态原型**，不宣称已经上线企业生产环境。已验证的是人工门禁、事件历史、敏感数值分层、幂等、过期/撤回/更正和 synthetic 回放；真实业务准确率、并发容量和合规仍需试点验证。

[![tests](https://img.shields.io/badge/tests-334%20passed-green)]()
[![status](https://img.shields.io/badge/status-Phase%202%20%E4%B8%BB%E4%BD%93%E5%AE%8C%E6%88%90-brightgreen)]()
[![phase](https://img.shields.io/badge/phase-SP1%7ESP5%20%E5%B7%B2%E4%BA%A4%E4%BB%98-blue)]()


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
│  │ Vue 3    │ ────────────► │ FastAPI  │ ───────────► │PostgreSQL│  │
│  │ 工作台    │  Bearer Token │   :8000  │   连接池     │16+pgvector│  │
│  │ (nginx)  │               │          │              │          │  │
│  └──────────┘               └──────────┘              └──────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 共享卷 ./vault 挂载（上传落盘/只读预览）
┌──────────────────────────────▼──────────────────────────────────────┐
│  存储层 · vault/（Markdown + YAML Frontmatter，规范源）+ PG 缓存      │
└─────────────────────────────────────────────────────────────────────┘
```

（📐 **交互式架构图**（Archify 生成，可缩放/主题切换/导出）：[llm-wiki-runtime.html](docs/diagrams/llm-wiki-runtime.html) ｜ 源规范 [llm-wiki-runtime.candidate.json](docs/diagrams/llm-wiki-runtime.candidate.json)）

（PlantUML 源图：[architecture.puml](docs/diagrams/architecture.puml)，知识流转闭环：[flow.puml](docs/diagrams/flow.puml)）

> 上图为**知识层（项目主体）**。其上另有一个垂直应用，复用同一套 FastAPI / JWT / 审计 / PostgreSQL 地基，见下方"垂直落地案例"。

**三个关键设计决策**：

1. **Claude Code 掌 LLM，FastAPI 掌数据** — 编译/审核/问答仍由 Claude Code（Bash 直操作 Vault + `_triggers/` 触发文件消费），**不重造 LLM 调用与 Agent 编排**；FastAPI 接管数据访问层（REST API 化上传/审核/搜索/管理 + JWT 认证 + 审计日志），Vue 工作台经 API 消费，不直连数据库。
2. **PostgreSQL 是缓存，不是权威** — YAML Frontmatter 是规范数据源；任何状态变更双写；不一致时以文件为准；`rebuild_index()` 可从文件全量重建缓存（pgvector 向量列同为可重建缓存，SP4 用）。
3. **触发文件消息队列** — API/工作台写 `vault/_triggers/compile_*.md`（原子写 tmp+mv）作为异步信号；Claude Code 经 SessionStart hook / `/process-triggers` 消费，实现"工作台/API ↔ LLM 引擎"解耦。

---

## 垂直落地案例：销售客户状态 Agent


知识层证明"编译式知识工程可行"，这个应用证明**同一套底座能长出带状态机、审计与权限边界的真实业务应用**。

> **销售洽谈记录 → 证据提取 → 状态建议 → 负责人确认 → 可审计状态事件**

- **核心原则**：不让模型直接改客户事实——Agent 只提建议，`StateEvent` 是事实唯一写入口，`CurrentState` 是可重建投影；证据不可覆盖；撤回/更正/过期一律**追加新事件**，不删历史。
- **领域建模**：7 状态有限状态机（`new_lead`→`contacted`→`need_confirmed`→`solution_eval`→`commercial_negotiation`→`won`/`lost_or_paused`），每状态有最低证据要求与默认有效期，证据不足必须输出 `needs_review`。
- **澄清优先于猜测**：信息不足时先按事实与追问契约追问，而不是直接给状态建议。
- **敏感数值分层**：正文只留占位符（`[AMOUNT_REF:nv-001]`），精确值写入独立受限表，授权角色在审计下恢复；Prompt / trace / 日志 / 向量索引中不出现精确金额。

**该应用的事实边界**（不得夸大）：只有 **36 条 synthetic 回放**验证拦截率、契约通过率与 token 估算，**不是真实业务准确率**；真实试点前仍需脱敏业务数据、人工标注集、成本基线与并发/合规验证。

| 设计文档 | 内容 |
|---|---|
| [SA-00](docs/SA-00_销售客户状态Agent_改造计划.md) / [SA-01](docs/SA-01_销售客户状态Agent_业务契约.md) | 阶段计划 / **业务契约（销售域需求唯一来源）** |
| [SA-02](docs/SA-02_销售客户状态Agent_领域模型.md) / [SA-03](docs/SA-03_销售客户状态Agent_生产形态检查清单.md) / [SA-04](docs/SA-04_销售客户状态Agent_合成评测报告.md) | 领域模型 / 生产形态自检 / 合成评测报告 |
| [SA-10](docs/SA-10_销售事实澄清Agent_迭代计划.md) ~ [SA-15](docs/SA-15_销售事实澄清Agent_Vue3工作台.md) | 澄清 Agent 契约族与 Vue 3 工作台 |

**Vue 3 工作台**（`frontend/`，Vue 3 + Element Plus + Vite）是**唯一界面**，覆盖知识域（总览 / 我的知识库 / 全部条目 / 上传 / 审核）与销售域（销售澄清 / 客户状态）；生产由 nginx 托管构建产物并反代 `/api`，开发态走 Vite 代理，共用同一套 FastAPI 权限、审计与状态机边界。

---

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
docker compose up -d          # 启动 PostgreSQL + FastAPI + Vue 工作台（nginx）
# 打开 http://localhost:8501 → 登录（默认 admin / admin123，建议首次登录后修改）

# 方式 B：本地开发（前端热更新）
bash init.sh                  # 初始化 Vault 目录树 + 建表（幂等）
pip install -r requirements.txt
uvicorn api.main:app --port 8000 &   # 先起 API（需 PostgreSQL 在 5432）
cd frontend && npm install && npm run dev   # 前端 :5173，/api 代理到 :8000
```

**服务端口**：**Vue 3 工作台 `:8501`**（容器内由 nginx 托管静态产物并反代 `/api`）｜ 开发态前端 `:5173` ｜ FastAPI REST API `:8000`（交互文档 `/docs`）｜ PostgreSQL `:5432`。

**默认账号**：`admin / admin123`（环境变量 `ADMIN_INIT_USER/ADMIN_INIT_PASS` 可改）。

**知识浏览**：工作台的「全部条目」页可在线预览 Markdown 正文；要看图谱与反向链接，用 [Obsidian](https://obsidian.md/) 打开 `vault/` 目录。


> clone 后知识索引初始为空。沿下方"核心闭环"走一遍上传→编译→审核流程，知识库即开始增长；索引当前规模见"真实数据验收"节。

**LLM 引擎**：两种运行方式——

```bash
# 方式 1（推荐）：触发文件 Watcher 常驻后台，上传后全自动编译（零人工）
tools\watcher_start.cmd        # 双击启动；或放入 shell:startup 开机自启
# 原理：轮询 vault/_triggers/，发现纸条自动唤起 `claude -p "/process-triggers"`（headless）

# 方式 2（手动）：在项目根目录运行 Claude Code，键入 /process-triggers 处理队列
```

> Watcher 需要本机已安装 Claude Code（编译必须由 LLM 引擎执行，这是架构原则）；日志见 `tools/watcher.log`。**运行前置**：Claude Code 的 LLM 通道必须可用（如 `ANTHROPIC_BASE_URL` 指向本地代理服务，需保证该服务已启动——watcher 内置预检，通道不通会在唤起前明确报错而不是白跑几分钟）。安全说明：headless 无人值守默认使用受限的 `acceptEdits` 权限模式；只有显式设置 `WATCHER_PERMISSION_MODE=bypassPermissions` 才会放开权限。对外部署仍应叠加 Claude Code allowedTools 白名单、容器沙箱和最小文件权限。

**环境变量**（从仓库根目录 `.env` 读取，compose 内置行为；`docker compose config` 可校验插值）：

| 变量 | 作用 | 缺省 |
|---|---|---|
| `JWT_SECRET` / `ADMIN_INIT_USER` / `ADMIN_INIT_PASS` | 认证与初始管理员 | 仅本地开发默认值 |
| `DASHSCOPE_API_KEY` | **SP4 向量通道**；未配置时 `/search` 静默降级 grep-only | 空 |
| `LANGFUSE_*` | SP2.5 可观测性探针（可选，零侵入） | 空 |
| `APP_ENV` | `production` 时强制强密钥与强密码 | `development` |

> ⚠️ **容器内不含 LLM 引擎**：编译/审核/问答由**宿主机的 Claude Code** 消费 `vault/_triggers/` 完成，容器只提供 API / 管理台 / 数据层。因此 `prompts/`、`workflows/`、`.claude/` 在宿主机运行，不在容器内执行。

**生产部署要求**：设置 `APP_ENV=production`、随机且至少 32 字符的 `JWT_SECRET`，以及至少 12 字符且非 `admin123` 的 `ADMIN_INIT_PASS`；生产环境不要直接暴露 PostgreSQL 5432，应通过反向代理提供 HTTPS 并限制 8000/8501 的公网访问。默认账号和默认密钥仅用于本地开发/demo。

**跑测试**（两种都可；`db` 服务每次启动会幂等创建测试库 `llmwiki_test`）：

```bash
# 宿主机（需 docker compose up -d db）
python -m pytest tests -q

# 容器内（镜像已含 tests/ 与 pytest）
docker compose run --rm api pytest tests -q
```

> 无 PG 时设 `PYTEST_SKIP_NO_DB=1` 跳过数据库相关用例。国内网络构建镜像时 `pip` 已默认走清华源（可用 `--build-arg PIP_INDEX_URL=...` 覆盖）。

---

## 知识层核心闭环（个人沉淀 → 审核流转 → 企业共享 → 自增长）

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
| [`docs/ARCH-00_项目定位与分层.md`](docs/ARCH-00_项目定位与分层.md) | **项目定位唯一来源**：知识层/应用层分层、判据、跨层冲突规则、事实边界 |
| [`docs/WIKI-00_LLM_Wiki_PRD.md`](docs/WIKI-00_LLM_Wiki_PRD.md) | 知识层需求唯一来源 v1.8：4 类角色 / 6 大模块 / 迭代路线图 / 错误 UX 文案 |
| [`docs/WIKI-01_LLM_Wiki_设计文档.md`](docs/WIKI-01_LLM_Wiki_设计文档.md) | Demo 详细设计 v0.1：目录结构 / SQLite DDL / 函数签名 / Agent 契约 / 触发机制 |
| [`docs/WIKI-10_LLM_Wiki_Phase2_路线图.md`](docs/WIKI-10_LLM_Wiki_Phase2_路线图.md) | Phase 2 主规划：SP1-SP5 拆分 / 排期 / 架构决策 / 退出标准 |
| [`docs/WIKI-20_Phase2_SP1_数据地基_设计文档.md`](docs/WIKI-20_Phase2_SP1_数据地基_设计文档.md) | SP1 数据地基：PostgreSQL 迁移 + pgvector（已交付）|
| [`docs/WIKI-30_Phase2_SP2_API与安全_设计文档.md`](docs/WIKI-30_Phase2_SP2_API与安全_设计文档.md) | SP2 API 与安全：FastAPI + JWT + 审计（已交付）|
| [`docs/WIKI-50_Phase2_SP4_混合检索_设计文档.md`](docs/WIKI-50_Phase2_SP4_混合检索_设计文档.md) | SP4 混合检索：双通道融合 / 缺口判据 τ 标定 / 评测结论与勘误 |
| [`docs/VAL-01_LLM_输出校验_设计说明.md`](docs/VAL-01_LLM_输出校验_设计说明.md) | LLM 输出契约校验：三组 schema + 门禁进 agent loop + 退化检测 |
| [`docs/INT-04_竞品对比_WeKnora.md`](docs/INT-04_竞品对比_WeKnora.md) | 与腾讯 WeKnora 的对比与选型决策（结论：不替换，选择性借鉴）|
| [`docs/SA-01_销售客户状态Agent_业务契约.md`](docs/SA-01_销售客户状态Agent_业务契约.md) | 应用层需求唯一来源：范围边界 / 状态集合 / 状态机 / 验收指标 |
| [`docs/VAL-03_检索评测_黄金集.md`](docs/VAL-03_检索评测_黄金集.md) | 检索离线评测集（14 条）——**本地面试资产，不进公开仓库** |

- **发现并修正 3 处 PRD 内部不一致**（架构层数、MCP Server 取舍、编译触发机制）
- **开发范式收敛**：SDD（编译产物/检索，输入输出可形式化）+ TDD（审核确定性规则/数据层/API）；LLM 输出非确定部分明确不做 BDD
- **334 个 pytest 用例**：DDL 幂等、双写一致性、审核规则边界（中文紧邻漏报/金额阈值）、上传批处理补偿、驳回重提流程、索引重建鲁棒性、JWT 鉴权与越权、审计落库、搜索缺口聚合、启动自愈、pgvector 混合检索与降级、缺口判据、LLM 输出契约校验，以及应用层的状态转移约束、敏感数值分层、澄清轮次预算、提交内容级幂等、状态建议双引擎、合成回放评测
- **CI（GitHub Actions）**：真实 PG 全量测试 + Prompt 退化检测（契约短语 + golden 样例）+ 检索回归门禁（可公开合成语料，`--no-vector` 离线零 key）；真实黄金集的完整评测含向量通道，仍仅本地执行
- **检索评测结论**：融合 MRR@10=1.00 / Recall@10=0.95，**纯 grep 通道仅 0.22**；缺口检出力 3/3（本地黄金集 14 条，详见 SP4 设计文档）


---

## 目录结构

```
├── api/                  # FastAPI 后端（main/auth/audit/schemas + routers/：知识层 + 应用层路由）
├── core/        # 管理台与知识层页面（app/upload/review/growth + login/api_client + db/ops/rules）
├── frontend/             # Vue 3 销售工作台（Vite，:5173；复用同一 FastAPI）
├── vault/                # Obsidian 知识库根目录（Markdown 权威存储）
│   ├── RAW/              # 原始文档（个人_notes/会议/经验/项目）
│   ├── pending_review/   # 待审核概念页
│   ├── NEXUS/            # 编译产物（资源摘要/概念页/研究 + index/log）
│   └── _triggers/        # 触发文件消息队列（+ done/ 归档）
├── docs/                 # 文档体系四族：ARCH（定位）/ WIKI（知识层）/ SA（应用层）/ VAL·INT
├── workflows/            # 4 个 Agent 编排（compile/review/growth/health）
├── prompts/              # 5 个 Agent 系统提示词（编译/审核/问答/销售澄清/销售状态）
├── tools/                # 迁移/评测/标定/退化检测/输出校验 CLI（migrate_to_pg/eval_search/tune_search/prompt_regression/validate_llm_output/eval_sales_state）
├── .claude/              # hook + /process-triggers、/ask 命令 + skills/
├── schema.sql            # PostgreSQL DDL（幂等：知识层 7 表 + 应用层领域表 + pgvector + users）
├── init.sh               # 幂等初始化（目录树 + 建表 + SCHEMA.md）
├── tests/                # 334 个 pytest 用例（连真实 PostgreSQL 隔离库；含可公开合成检索语料）
```

---

## 演进状态

### 知识层（Phase 2 路线图）

| 子项目 | 内容 | 状态 |
|--------|------|------|
| **SP1 数据地基** | SQLite → PostgreSQL 16 + pgvector，知识层 7 表 + users，迁移脚本 | ✅ 已交付（`886b2f6`） |
| **SP2 API 与安全** | FastAPI REST + JWT 认证 + 审计日志，界面接入 | ✅ 已交付（`d174fa1`/`2cd5881`） |
| **SP2.5 可观测性** | 编译过程 Trace + 端点埋点 + 看板页 | ✅ 已交付（`a1b71f5`） |
| **SP3 增量编译** | 触发 watcher 全自动编译 + compile_tasks 断点续跑 | ✅ 已交付（`4ba4304`/`bbcfae7`） |
| **SP4 混合检索** | grep + pgvector 双通道加权融合 + 评测集/缺口阈值判据 | ✅ 已交付（`6269f1b`/`2571072`） |
| **SP5 知识智能** | 健康巡检 + 周报 + done 归档 | ✅ 已交付（`331189a`） |
| 工程收尾 | CI（测试 + prompt 退化检测）、LLM 输出契约校验（门禁进 loop）、检索评测黄金集 | ✅ 已交付（`8b20649`/`b97c2ce` 等） |

- **Phase 3 规划**：知识图谱（实体/关系抽取）、多租户 RBAC、外部源感知、分布式编译、生产级部署（详见 PRD）

### 应用层（销售客户状态 Agent，阶段 0-5）

| 阶段 | 内容 | 状态 |
|------|------|------|
| 阶段 0 | 范围冻结与验收契约（`SA-01` 业务契约、状态集合、风险边界） | ✅ 已确认 |
| 阶段 1-2 | 领域模型 + 数据层与状态机（幂等键、事件历史、过期/撤回） | ✅ 已交付（`b2b7032`） |
| 阶段 3 | 事实澄清 Agent（`SA-11` 契约：信息不足先追问） | ✅ 已交付（`33cdd50`） |
| 阶段 4 | 角色化工作台（销售提交 / 负责人审核） | ✅ 已交付 |
| 阶段 5 | Vue 3 工作台（澄清 + 审核 + 证据时间线） | ✅ 已交付（`405b4ad`） |
| 评测 | 36 条 synthetic 回放（拦截率 / 契约通过率 / token 估算） | ✅ 已交付（`docs/SA-04`） |

- **后续规划**：真实脱敏数据试点、人工标注集与成本基线、并发与合规验证（见 `docs/SA-03` 生产形态检查清单）


---

## 相关资源

- [Karpathy · LLM Wiki Gist](https://gist.github.com/karpathy/90f50cd5cbf126f36bde3a39d67d2431) — LLM Wiki 编译范式原始理念
- **Google OKF（Open Knowledge Format）v0.1** — 知识文件标准化规范（Just Markdown + YAML Frontmatter + Reserved Files + 容错消费），本项目严格对齐
- [docs/INT-04_竞品对比_WeKnora.md](docs/INT-04_竞品对比_WeKnora.md) — 与腾讯 WeKnora 的对比与迁移决策（结论：不替换，只借鉴解析/检索/图谱抽取的工程做法）
