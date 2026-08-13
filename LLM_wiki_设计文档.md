# LLM Wiki 知识库平台 —— 详细设计文档（Demo 范围）

**版本**：v0.1
**状态**：待评审
**作者**：何豫东
**日期**：2026年8月13日
**上游文档**：LLM_wiki_PRD.md（v1.7）

---

## 1. 引言

### 1.1 文档目的

本文档是 PRD v1.7 的**落地设计规格**：将 PRD 中的功能需求、技术架构、数据模型转化为可直接编码的详细设计——包括文件路径、函数签名、SQLite DDL、Agent 输入输出契约、错误处理表。

**阅读对象**：开发者（本人）、Claude Code（作为实现时的约束上下文）。

### 1.2 范围

| 维度 | 范围 |
|------|------|
| **阶段** | 仅 Demo（PRD 第七章 Phase 1 范围，2-4 周） |
| **包含** | 编译引擎、审核流转、检索问答、自增长引擎（搜索反馈闭环）、Streamlit 3 页面、SQLite 4 表、触发信号机制、init.sh、Docker 部署 |
| **不包含** | 增量编译、向量检索（ChromaDB）、JWT 认证、审计日志、健康巡检、知识演进、关联涌现、外部源感知、FastAPI 后端（以上均为 Phase 2/3） |

### 1.3 与 PRD 的关系

- 本文档的一切设计以 PRD v1.7 为需求来源。**冲突时以 PRD 为准，并在 1.5 节记录待修正项**。
- PRD 中已确定且本文不重复展开的内容：Prompt 全文（`prompts/*.md`）、审核六维度判定标准、错误 UX 文案（PRD 9.3 表格直接复用）。

### 1.4 术语表

| 术语 | 含义 |
|------|------|
| Vault | Obsidian 知识库根目录，即项目的 `vault/` 目录 |
| RAW | 原始素材层，待编译的原始文档 |
| NEXUS | 加工知识层，编译产物所在 |
| 编译 | 将 RAW 文档经 LLM 转换为结构化知识产物（资源摘要 + 概念页）的过程 |
| 触发文件 | `_triggers/` 目录下的信号文件，Streamlit 写入、Claude Code 消费 |
| 条目（Entry） | 一个知识 Markdown 文件（资源摘要或概念页），以 Vault 相对路径标识 |
| 物化视图 | knowledge_entries 表——YAML Frontmatter 字段的 SQLite 缓存 |

### 1.5 与 PRD 的一致性说明

设计过程中发现 3 处 PRD 内部不一致，**已全部修正**（PRD 与本文现一致）：

| # | 原不一致 | 修正方式 | 修正位置 |
|---|---------|---------|---------|
| 1 | PRD 头部写"五层架构"，但第四章架构图实际只有 4 层（SQLite 已并入存储层） | 头部改为"四层架构（原六层减二：桥接层移除 + 元数据层并入存储层）" | PRD 头部第 7 行 |
| 2 | PRD 3.1 写"用户在 Streamlit 界面手动触发全量编译"，未定义 Streamlit → Claude Code 的触发机制 | 明确为触发文件信号机制：Streamlit 写 `vault/_triggers/compile_*.md`，Claude Code 经 SessionStart hook 或 /process-triggers 消费；移除失效的 `python -m ingest --all` CLI 残留 | PRD 3.1 模块一 |
| 3 | PRD docker-compose 中 `DB_PATH=/app/meta.db` 与 vault 挂载路径不一致 | 统一为 `DB_PATH=/app/vault/meta.db`，移除单独 meta.db 挂载，Vault 整体挂载 | PRD 9.1 |

---

## 2. 系统概览

### 2.1 架构回顾（PRD 4.1 落地版）

```
┌─────────────────────────────────────────────────────────────┐
│  界面层：Obsidian Desktop（全用户知识界面）                   │
│  浏览 · 搜索(Ctrl+Shift+F) · 图谱 · wikilink 导航             │
└──────────────────────────┬──────────────────────────────────┘
                           │ 文件系统直接读写（Obsidian 原生行为）
┌──────────────────────────▼──────────────────────────────────┐
│  存储层：vault/（Markdown 文件）+ vault/meta.db（SQLite）     │
│  SCHEMA.md · RAW/ · pending_review/ · NEXUS/ · _triggers/   │
└──────────────────────────┬──────────────────────────────────┘
                           │ Bash 工具（Claude Code） / Python（Streamlit）
┌──────────────────────────▼──────────────────────────────────┐
│  引擎层：Claude Code（本机终端运行）                          │
│  编译 Agent · 审核 Agent · 问答 Agent                        │
│  Harness（parallel 六维度审核 / pipeline 批量编译）          │
│  触发消费：SessionStart hook + /process-triggers 命令        │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP (localhost:8501)
┌──────────────────────────▼──────────────────────────────────┐
│  管理层：Streamlit（Docker 容器）                            │
│  上传页 · 审核页 · 自增长看板（直接读写 vault/ 与 meta.db）   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 组件清单

| # | 组件 | 位置 | 职责 | 谁写 | 谁读 | 触发方式 |
|---|------|------|------|------|------|---------|
| 1 | Obsidian | 用户本机应用 | 知识浏览、搜索、图谱 | 用户 | 用户 | 手动打开 |
| 2 | Claude Code | 用户本机终端 | 编译/审核/问答 Agent 执行 | Claude Code | 用户 | SessionStart hook、手动命令 |
| 3 | Streamlit 上传页 | `streamlit_app/upload.py` | 文件上传、编译触发、任务状态 | 用户交互 | 用户 | 浏览器 |
| 4 | Streamlit 审核页 | `streamlit_app/review.py` | AI 判定展示、人工通过/驳回 | 用户交互 | 用户 | 浏览器 |
| 5 | Streamlit 看板页 | `streamlit_app/growth.py` | 知识缺口 Top 20、周报展示 | 用户交互 | 用户 | 浏览器 |
| 6 | db.py | `streamlit_app/db.py` | SQLite 全部读写封装 | Streamlit 页面 | Streamlit 页面 | 函数调用 |
| 7 | init.sh | `init.sh` | Vault 初始化 + 建表 | 开发者 | 开发者 | 手动执行 |
| 8 | SQLite | `vault/meta.db` | 元数据缓存 + 过程数据 | Streamlit、Claude Code | 同上 | — |
| 9 | 触发文件 | `vault/_triggers/*.md` | 编译/审核信号 | Streamlit | Claude Code | Streamlit 写、Claude Code 扫描 |
| 10 | Prompt 文件 | `prompts/*.md`（3 个） | Agent 指令 | Claude Code 引用 | Claude Code | — |
| 11 | Workflow 文件 | `workflows/*.md`（3 个） | Agent 编排指令 | Claude Code 引用 | Claude Code | — |
| 12 | Claude Code 命令 | `.claude/commands/`（2 个） | 手动触发入口 | Claude Code | 用户 | 用户键入 /命令 |
| 13 | SessionStart hook | `.claude/hooks/session-start.sh` | 自动触发检测 | Claude Code | Claude Code | 会话启动 |

### 2.3 运行环境

| 项 | 要求 |
|----|------|
| 操作系统 | Windows 11（开发机） |
| Obsidian | Desktop 最新版，将 `vault/` 打开为 Vault |
| Claude Code | 终端运行，工作目录 = 项目根目录（`llm-wiki-demo/`） |
| Docker | Docker Desktop（仅运行 Streamlit 容器） |
| Python | 3.11+（Streamlit 容器内必须）；本机 3.11+ 可选——仅当上传 .pdf/.docx 时 Claude Code 需要本机 Python 做文本提取（pypdf + python-docx），纯 .md/.txt 流程无本机 Python 依赖 |
| LLM | Claude 模型（经 Claude Code 调用，无需自建 API 封装） |

**网络拓扑**：所有组件运行在同一台开发机上。Streamlit 在 Docker 容器内，通过 volume 挂载访问 `./vault`；Claude Code 与 Obsidian 在本机直接访问同一目录。

### 2.4 演示场景地图（与 PRD 9.4 脚本 A/B 对应）

| 时间点（脚本A） | 动作 | 参与组件 |
|----------------|------|---------|
| 00:00 | 打开 Obsidian 展示 Vault 结构 | 1, 8 |
| 00:30 | Streamlit 上传文档 + 触发编译 | 3, 6, 8, 9 |
| 01:00 | Claude Code 消费触发文件并编译 | 2, 8, 9, 10, 11 |
| 01:30 | 审核页查看 AI 判定 + 通过 | 2, 4, 6, 8 |
| 02:30 | Obsidian 中查看新概念页 + 图谱 | 1 |
| 03:30 | Obsidian 搜索"叫应体系" | 1 |
| 04:30 | 看板页展示知识缺口 | 5, 6, 8 |

---

## 3. 目录结构与初始化

### 3.1 完整目录树

```
llm-wiki-demo/                      # 项目根目录（Claude Code 工作目录）
├── LLM_wiki_PRD.md                 # 需求文档（v1.7）
├── LLM_wiki_设计文档.md            # 本文档
├── docker-compose.yml              # 单容器：Streamlit
├── Dockerfile                      # Streamlit 镜像
├── requirements.txt                # streamlit, pyyaml, pandas（仅容器用）
├── .env.example                    # 环境变量模板
├── init.sh                         # 初始化脚本（幂等）
├── .claude/                        # Claude Code 配置
│   ├── settings.json               # SessionStart hook 注册
│   ├── hooks/
│   │   └── session-start.sh        # 触发文件检测提示
│   └── commands/
│       ├── process-triggers.md     # /process-triggers：处理触发队列
│       └── ask.md                  # /ask <问题>：检索+问答
├── streamlit_app/
│   ├── app.py                      # 入口：侧边栏 + 页面路由
│   ├── db.py                       # SQLite 操作封装（唯一入口）
│   ├── upload.py                   # 上传页
│   ├── review.py                   # 审核页
│   └── growth.py                   # 自增长看板
├── vault/                          # Obsidian Vault = 知识库根目录
│   ├── .obsidian/                  # Obsidian 配置（用户自定义，见 3.4）
│   ├── SCHEMA.md                   # 知识结构规范（init.sh 生成）
│   ├── meta.db                     # SQLite（init.sh 建表）
│   ├── _triggers/                  # 触发信号区（init.sh 创建）
│   │   └── done/                   # 已处理触发文件归档
│   ├── RAW/                        # 原始素材层
│   │   ├── 个人_notes/
│   │   ├── 会议/
│   │   ├── 经验/
│   │   └── 项目/
│   ├── pending_review/             # 待审核区（Demo：扁平存储）
│   └── NEXUS/                      # 加工知识层
│       ├── index.md                # 全局索引（Reserved File）
│       ├── log.md                  # 审计日志占位（Reserved File，Demo 空文件）
│       ├── 资源/                   # 资源摘要（编译自动发布）
│       ├── 概念/                   # 概念页（审核通过后移入）
│       └── 研究/                   # 自增长周报等综合产物
├── prompts/
│   ├── compile_prompt.md           # 编译 Agent 指令（已有）
│   ├── review_prompt.md            # 审核 Agent 指令（已有）
│   └── answer_prompt.md            # 问答 Agent 指令（已有）
├── workflows/
│   ├── compile_workflow.md         # 批量编译编排
│   ├── review_workflow.md          # 六维度并行审核编排
│   └── growth_workflow.md          # 周度自增长分析编排
└── tests/
    ├── test_review_rules.py        # 审核确定性规则测试（TDD）
    └── sample_docs/                # 测试样例文档
        ├── 样例_示例监测产品产品白皮书.md
        ├── 样例_内容不足.txt
        └── 样例_含敏感信息.md
```

### 3.2 init.sh 设计

**职责**：初始化 Vault 目录树 + SQLite 建表 + 生成 SCHEMA.md。**必须幂等**——重复执行不破坏已有数据。

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VAULT="$ROOT/vault"

# 1. 目录树（mkdir -p 幂等）
mkdir -p "$VAULT"/RAW/{个人_notes,会议,经验,项目}
mkdir -p "$VAULT"/pending_review
mkdir -p "$VAULT"/NEXUS/{资源,概念,研究}
mkdir -p "$VAULT"/_triggers/done

# 2. Reserved Files（已存在则跳过，不覆盖）
[ -f "$VAULT/NEXUS/index.md" ] || printf '# 知识库索引\n\n（编译时由 Claude Code 逐次更新）\n' > "$VAULT/NEXUS/index.md"
[ -f "$VAULT/NEXUS/log.md" ]   || : > "$VAULT/NEXUS/log.md"

# 3. SCHEMA.md（已存在则跳过）
[ -f "$VAULT/SCHEMA.md" ] || cat > "$VAULT/SCHEMA.md" << 'EOF'
# 知识库 Schema
## 1. 合法 Type 列表
- concept / resource / research / glossary
## 2. 合法 Status 列表
- draft / pending / active / stale / deprecated
## 3. 合法 Tags 命名空间
- 部门: 销售/售前/产品/实施交付/开发/财务/人事/行政/共享层
- 领域: AI/大数据/云计算/安全/项目管理/产品设计/应急管理/智慧城市/物联网/数字孪生
- 类型: 实战经验/技术方案/产品文档/会议纪要/复盘总结/行业研究/标准规范/培训材料
## 4. Frontmatter 字段规范（详见设计文档 4.2）
## 5. 文件名与 Wikilink 约定（详见设计文档 5.4）
## 6. 版本号规则
- 格式 V{major}.{minor}，首次入库 V1.0；正文微调 V1.1；核心定义改写 V2.0
EOF

# 4. SQLite 建表（IF NOT EXISTS 幂等）
sqlite3 "$VAULT/meta.db" << 'EOF'
CREATE TABLE IF NOT EXISTS knowledge_entries (
    path        TEXT PRIMARY KEY,          -- Vault 相对路径，如 NEXUS/概念/示例监测产品.md
    type        TEXT NOT NULL,             -- concept/resource/research/glossary
    title       TEXT NOT NULL,
    department  TEXT,                      -- 9 部门 + 共享层
    status      TEXT NOT NULL DEFAULT 'pending',  -- draft/pending/active/stale/deprecated
    version     TEXT NOT NULL DEFAULT 'V1.0',
    fingerprint TEXT,                      -- 源文件 SHA256（资源摘要有，概念页继承其源）
    updated_at  TEXT                       -- YYYY-MM-DD
);
CREATE TABLE IF NOT EXISTS compile_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_path     TEXT NOT NULL,            -- RAW/分类/文件名.md
    nexus_path   TEXT,                     -- 产物资源摘要路径
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed/cached
    error_msg    TEXT,
    started_at   TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nexus_path    TEXT NOT NULL,           -- 概念页在 pending_review/ 下的路径
    submitter     TEXT,                    -- Demo 单用户，固定 'demo_user'
    department    TEXT,                    -- AI 判定归属部门
    ai_verdict    TEXT,                    -- approved/rejected/needs_human_review
    ai_scores     TEXT,                    -- 六维度 JSON（审核 Agent 原始输出）
    human_decision TEXT,                   -- approved/rejected，NULL=未处理
    reject_reason TEXT,                    -- 驳回原因（人工填写）
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS search_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,
    match_count INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'streamlit',  -- streamlit/claude_code
    timestamp   TEXT NOT NULL
);
EOF

echo "✅ 初始化完成。下一步：docker compose up -d 启动 Streamlit；Obsidian 打开 $VAULT"
```

### 3.3 Docker 部署设计

**docker-compose.yml（全文）**：

```yaml
version: '3.8'
services:
  streamlit:
    build: .
    ports:
      - "8501:8501"
    volumes:
      - ./vault:/app/vault        # Vault 整体挂载（含 meta.db 与 _triggers/）
    environment:
      - KB_ROOT=/app/vault
      - DB_PATH=/app/vault/meta.db
    restart: unless-stopped
```

**Dockerfile**：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY streamlit_app/ ./streamlit_app/
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

**设计要点**：
- Streamlit 容器内路径 `/app/vault` 与宿主机 `./vault` 是同一目录。**触发文件和 SQLite 均以 Vault 相对路径为约定，容器与宿主机两侧均不写绝对路径**，避免路径不一致。
- Claude Code 与 Obsidian **不进容器**，直接在本机操作 `./vault`。
- Docker Desktop 在 Windows 上的文件监听延迟可忽略——本设计没有 watcher，触发文件由 Claude Code 主动扫描。

### 3.4 Obsidian 配置建议

`.obsidian/` 目录由用户首次打开 Vault 时生成。建议配置（手动设置，init.sh 不干预）：

| 设置项 | 建议值 | 原因 |
|--------|--------|------|
| 文件与链接 → 检测所有文件扩展名 | 开启 | .md 无问题，保险起见 |
| 文件与链接 → 排除文件 | `_triggers` | 触发信号区不进入知识浏览视野 |
| 核心插件 → 图谱 | 开启 | 演示 wikilink 关系 |
| 核心插件 → 反向链接 | 开启 | 演示概念被引用关系 |

### 3.5 Claude Code 配置

**`.claude/settings.json`（SessionStart hook 注册）**：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash .claude/hooks/session-start.sh"
          }
        ]
      }
    ]
  }
}
```

**`.claude/hooks/session-start.sh`（触发检测提示）**：

```bash
#!/usr/bin/env bash
# 输出 additionalContext：指示 Claude 检查触发队列
TRIGGERS="$(find vault/_triggers -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$TRIGGERS" -gt 0 ]; then
  echo "【知识库触发队列】vault/_triggers/ 下有 $TRIGGERS 个未处理触发文件。请优先执行 /process-triggers 处理队列，处理完成后将触发文件移入 vault/_triggers/done/。"
fi
```

**`.claude/commands/process-triggers.md`**（手动兜底命令）：

```markdown
# /process-triggers —— 处理知识库触发队列

扫描 vault/_triggers/*.md（排除 done/），按文件时间戳升序处理：

1. 对每个 compile_*.md：按 workflows/compile_workflow.md 执行批量编译
2. 对每个 review_*.md：按 workflows/review_workflow.md 执行六维度审核
3. 每个触发文件处理成功（或所有条目已尝试且记录失败原因）后，移入 vault/_triggers/done/
4. 处理报告：编译 N 个、审核 M 个、失败 K 个（附失败文件与原因）
```

**`.claude/commands/ask.md`**（问答入口）：

```markdown
# /ask <问题> —— 检索知识库并生成带引用答案

1. 在 vault/NEXUS/ 下 grep 检索问题关键词，取匹配行数 Top-5 的 .md 文件
2. cat 读取这 5 个文件全文（含 YAML Frontmatter）
3. 按 prompts/answer_prompt.md 执行答案生成
4. 将 (query, match_count) 写入 vault/meta.db 的 search_logs 表（source='claude_code'）
5. 向用户呈现答案（引用来源为可点击的 Vault 相对路径）
```

---

## 4. 数据层设计

### 4.1 数据模型总览（PRD 5.0 落地）

| 层 | 载体 | 内容 | 权威性 |
|----|------|------|--------|
| 规范数据 | `vault/**/*.md` 的 YAML Frontmatter + 正文 | 知识条目全部属性与内容 | **唯一权威** |
| 查询缓存 | `meta.db::knowledge_entries` | YAML 字段的物化视图 | 可随时从文件重建 |
| 过程数据 | `meta.db::compile_tasks / pending_reviews` | 编译过程、审核过程 | 权威（文件无法表达） |
| 行为数据 | `meta.db::search_logs` | 搜索记录 | 权威 |

**一致性规则**（写路径/读路径/修复路径，来自 PRD 5.0）：

- **写路径**：任何状态变更同时写 YAML 文件 + SQLite。Claude Code 用 `sed`/文件重定向 + `sqlite3`；Streamlit 用 Python 同时操作两者。
- **读路径**：列表/过滤走 SQLite；内容展示走文件系统（Obsidian 原生、Claude Code `cat`）。
- **修复路径**：Streamlit 上传页的「重建索引」按钮 → 扫描 `NEXUS/**/*.md` + `pending_review/*.md` → 解析 YAML → `REPLACE INTO knowledge_entries`。
- **仲裁**：YAML 文件永远为准。

### 4.2 YAML Frontmatter 字段规范

| 字段 | 必填 | 取值/格式 | 写入方 | 说明 |
|------|------|----------|--------|------|
| `type` | ✅（OKF 唯一必填） | concept / resource / research / glossary | 编译 Agent | — |
| `title` | ✅ | 文本 | 编译 Agent | 概念名或文档标题 |
| `status` | ✅ | draft / pending / active / stale / deprecated | 编译 Agent 初始 `pending`；审核后由 Streamlit/Claude Code 改 | 见 4.4 状态机 |
| `source` | ✅ | RAW 相对路径 | 编译 Agent | 溯源 |
| `department` | ✅ | 9 部门 + 共享层 | 审核 Agent 判定 | 见 PRD 审核维度三 |
| `description` | 推荐 | ≤100 字 | 编译 Agent | — |
| `tags` | 推荐 | 数组，2-5 个，取 SCHEMA.md 合法值 | 编译 Agent | — |
| `created` | 推荐 | YYYY-MM-DD | 编译 Agent | — |
| `updated` | 推荐 | YYYY-MM-DD | 任何修改方 | 健康巡检（Phase 2）依据 |
| `version` | 推荐 | V{major}.{minor} | 编译 Agent 初始 V1.0 | — |
| `contributors` | 可选 | 字符串数组 | 编译 Agent（Demo 固定 ['demo_user']） | Phase 2 聚合 |
| `fingerprint` | 资源摘要必填 | SHA256 hex | 编译 Agent | 缓存判据 |

**Demo 强制校验项**（审核维度一「完整性」的确定性检查）：`type`、`title`、`status`、`source` 四字段存在且非空 + 正文 ≥ 100 中文字符。

### 4.3 SQLite DDL

与 init.sh 中建表语句一致（见 3.2 第 4 步）。补充设计说明：

- `knowledge_entries.path` 是主键。**概念页在审核通过前路径为 `pending_review/概念名.md`，通过后为 `NEXUS/概念/概念名.md`**——移动时执行 `DELETE` 旧行 + `INSERT` 新行。
- `compile_tasks.fingerprint` 缓存判据：同一 `raw_path` 存在 `fingerprint` 相同且 `status='done'` 的记录 → 本次编译标记 `cached`，不调用 LLM。
- `pending_reviews.ai_scores` 存审核 Agent 输出的完整 JSON（含 `verdict/department/scores/duplicates/concerns/summary`），Streamlit 审核页直接解析展示。
- 所有 TEXT 日期用 `YYYY-MM-DD`（日期）或 `YYYY-MM-DD HH:MM:SS`（时间戳），由写入方生成。

### 4.4 条目生命周期状态机

```
                    ┌─────────────────────────────┐
                    │  编译 Agent 产出概念页        │
                    │  pending_review/概念名.md    │
                    │  status = pending           │
                    └──────────────┬──────────────┘
                                   │ AI 审核（六维度）
                                   ▼
                    ┌─────────────────────────────┐
                    │  AI 判定写入 pending_reviews │
                    │  ai_verdict = approved /    │
                    │  rejected / needs_human_    │
                    │  review                     │
                    └──────────────┬──────────────┘
                                   │ 人工在 Streamlit 审核页决定
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
   │ [✓ 通过]        │   │ [✗ 驳回]        │   │ 不操作           │
   │ 文件移到 NEXUS/ │   │ 留在 pending_   │   │ 保持 pending     │
   │ 概念/           │   │ review/         │   │ （留在待审列表） │
   │ status=active   │   │ status=draft    │   │                 │
   └─────────────────┘   └────────┬────────┘   └─────────────────┘
                                  │ [重新提交审核] 按钮
                                  ▼
                          status = pending（重新进入 AI 审核）
```

**资源摘要不进入审核**：资源摘要是 RAW 的 1:1 事实性摘要，编译后直接发布（`NEXUS/资源/`，status=active）。审核只作用于概念页。

**状态与目录的对应关系**（Demo）：

| status | 概念页位置 | 资源摘要位置 |
|--------|-----------|-------------|
| pending | `pending_review/` | —（资源无 pending） |
| active | `NEXUS/概念/` | `NEXUS/资源/` |
| draft（驳回后） | `pending_review/` | — |
| stale / deprecated | Demo 不产生（Phase 2 健康巡检引入） | 同左 |

### 4.5 触发文件规范

**目录**：`vault/_triggers/`（待处理）+ `vault/_triggers/done/`（已处理归档）。

**两种类型**：

| 类型 | 文件名模式 | 写入方 | 消费方 | 内容 |
|------|-----------|--------|--------|------|
| 编译触发 | `compile_YYYYMMDD_HHMMSS.md` | Streamlit 上传页 | Claude Code | 待编译 RAW 路径列表 |
| 审核触发 | `review_YYYYMMDD_HHMMSS.md` | 编译 Agent（编译完成后写）、Streamlit 审核页（重试按钮） | Claude Code | 待审核条目路径列表 |

**文件格式**（Markdown + Frontmatter，Obsidian 中即使可见也可读）：

```markdown
---
type: trigger
kind: compile          # compile | review
created: "2026-08-13T14:30:00"
source: streamlit      # streamlit | compile_agent
---
- RAW/产品资料/示例监测产品产品白皮书.md
- RAW/技术方案/叫应体系技术方案.md
```

**写入原子性**：先写 `_triggers/.tmp_<文件名>`，再 `mv` 为正式文件名——避免 Claude Code 扫描到写了一半的文件。

**生命周期**：
1. Streamlit 写入 `_triggers/compile_<ts>.md`
2. Claude Code（SessionStart hook 或 /process-triggers）扫描到 → 执行 → 无论成败，处理完毕后整体移入 `_triggers/done/`（失败详情记录在 compile_tasks.error_msg / pending_reviews 中，触发文件本身不保留失败标记）
3. `done/` 归档仅作审计留痕，不自动清理

**扫描规则**：只处理 `_triggers/*.md` 一级文件（递归排除 `done/`）；按文件名时间戳升序；先 compile 后 review（一个会话内编译产物可能需要审核）。

### 4.6 缓存一致性维护

| 场景 | 处理方式 | 实现位置 |
|------|---------|---------|
| 编译完成 | 编译 Agent 写 YAML 文件 + `sqlite3` upsert knowledge_entries + insert compile_tasks | workflows/compile_workflow.md |
| 人工审核通过 | Streamlit 改 YAML status + 移动文件 + UPDATE knowledge_entries（path 变更 → DELETE+INSERT）+ UPDATE pending_reviews | review.py |
| 人工驳回 | Streamlit 改 YAML status=draft + UPDATE knowledge_entries + UPDATE pending_reviews（含 reject_reason） | review.py |
| 重建索引 | 扫描全部 .md → 解析 YAML → 全量 REPLACE INTO knowledge_entries | upload.py「重建索引」按钮 → db.py::rebuild_index() |
| Claude Code 直接编辑 NEXUS 文件 | 编辑后必须同步 sqlite3 更新对应行 | Agent 指令中强制要求 |

---

## 5. 编译引擎设计

### 5.1 编译流程时序

```
用户(浏览器)          Streamlit(upload.py)      Vault 文件系统      Claude Code(编译Agent)
     │                       │                       │                      │
     │ 1.选择文件+分类+上传    │                       │                      │
     │──────────────────────►│                       │                      │
     │                       │ 2.校验格式/大小        │                      │
     │                       │ 3.保存到 RAW/<分类>/    │                      │
     │                       │──────────────────────►│                      │
     │                       │ 4.写触发文件(原子写)    │                      │
     │                       │──────────────────────►│                      │
     │ 5.显示"已加入编译队列"  │                       │                      │
     │◄──────────────────────│                       │                      │
     │                       │                       │  6.SessionStart hook │
     │                       │                       │  或 /process-        │
     │                       │                       │  triggers 扫描       │
     │                       │                       │◄─────────────────────│
     │                       │                       │  7.逐个 RAW 文件:    │
     │                       │                       │  SHA256 → 查缓存     │
     │                       │                       │  8.未命中: 读文件+    │
     │                       │                       │  compile_prompt →    │
     │                       │                       │  LLM 编译            │
     │                       │                       │  9.写 NEXUS/资源/ +  │
     │                       │                       │  pending_review/     │
     │                       │                       │  + index.md +        │
     │                       │                       │  sqlite3 三张表      │
     │                       │                       │  10.写审核触发文件    │
     │                       │                       │  11.触发文件移 done/  │
```

### 5.2 触发文件处理流程（Claude Code Agent 视角）

执行 `/process-triggers` 或 SessionStart hook 提示后，Agent 按以下步骤操作（workflows/compile_workflow.md 全文展开）：

```
1. 列出 vault/_triggers/*.md（排除 done/），按文件名时间戳升序
2. 对每个 compile_*.md：
   a. 解析其中的 RAW 路径列表
   b. 对每个 RAW 路径，先检查该路径是否已存在于编译结果（见 5.6 缓存）
   c. 对未命中的文件列表，用 Harness pipeline 并发执行编译（见 5.3）
   d. 汇总每个文件的编译结果（done/cached/failed）
3. 所有 compile_*.md 处理完后，对每个 review_*.md 按第六章流程执行
4. 将已处理的触发文件 mv 到 vault/_triggers/done/
5. 向用户报告：编译 N 个（含缓存 M 个）、失败 K 个（附失败原因）
```

### 5.3 编译 Agent 输入输出契约

**输入**（pipeline 每个 item 传入）：
1. `raw_path`：RAW 文件 Vault 相对路径
2. `plaintext`：文件纯文本内容（.md/.txt 直接 `cat`；.pdf/.docx 由 Claude Code 用 Python 提取，见 5.7）

**处理**：执行 `prompts/compile_prompt.md`（已定稿，不改）。

**输出**（Agent 必须严格产出的 JSON，schema 见 compile_prompt.md）：

```json
{
  "resource": {
    "title": "示例监测产品产品白皮书",
    "description": "…",
    "tags": ["产品", "应急管理"],
    "department": "产品",
    "summary": "## 摘要\n…\n\n## 关键信息\n- …",
    "key_points": ["…"],
    "source_type": "项目"
  },
  "concepts": [
    {
      "title": "示例监测产品",
      "description": "…",
      "tags": ["产品"],
      "department": "产品",
      "content": "## 定义\n…\n\n## 关联知识\n- [[概念-多灾种监测预警]]",
      "related_to": ["多灾种监测预警"]
    }
  ]
}
```

**输出后的落盘动作**（Agent 执行，非代码执行）：

| 动作 | 目标 | 状态 |
|------|------|------|
| 写资源摘要 | `NEXUS/资源/<资源标题>.md`，YAML 含 type=resource, status=active, fingerprint | active |
| 写概念页 | `pending_review/<概念名>.md`，YAML 含 type=concept, status=pending, source=RAW 路径 | pending |
| 更新 index.md | 在「资源」节追加一行 `- [标题](NEXUS/资源/标题.md)`；概念节**不**追加（通过审核后由 Streamlit 追加） | — |
| sqlite3 | `knowledge_entries` upsert（资源 1 行 + 概念 N 行）；`compile_tasks` insert/update | — |
| 写审核触发文件 | `_triggers/review_<ts>.md`，列出本批所有概念页路径 | — |

### 5.4 产物文件路径与命名规则

| 规则 | 内容 |
|------|------|
| 资源摘要路径 | `NEXUS/资源/<资源标题>.md`（1:1 映射 RAW） |
| 概念页路径 | 审核前 `pending_review/<概念名>.md`；通过后 `NEXUS/概念/<概念名>.md` |
| 文件名清洗 | 移除 Windows 非法字符 `\ / : * ? " < > \|` 及首尾空格；空串回退为「未命名」；重名时追加 `-2`、`-3` |
| 同名冲突（概念） | 若 pending_review/ 或 NEXUS/概念/ 已存在同名文件：新概念追加 `-2` 后缀，并在 YAML `description` 末尾注明「（与《原名》同名，待合并评审）」 |
| wikilink 约定 | 概念页互链用 `[[概念-<概念名>]]` 格式（Obsidian 解析）；正文中引用资源用 Markdown 链接 `[标题](NEXUS/资源/标题.md)` |
| 中文路径 | 全链路 UTF-8，Docker 挂载不转换 |

### 5.5 index.md 更新规则

`NEXUS/index.md` 是渐进式目录（Reserved File），结构：

```markdown
# 知识库索引

## 资源
- [示例监测产品产品白皮书](NEXUS/资源/示例监测产品产品白皮书.md)

## 概念
- [[概念-示例监测产品]] → NEXUS/概念/示例监测产品.md
```

更新时机与方式：
- 编译 Agent 完成后：追加资源行（幂等——按标题查重，已存在则跳过）
- Streamlit 审核通过后：追加概念行
- 每次追加后更新头部统计行：`> 资源 N 篇 · 概念 M 个 · 最后更新 YYYY-MM-DD`

### 5.6 SHA256 指纹与缓存

| 步骤 | 实现 |
|------|------|
| 指纹计算 | `sha256sum <raw_file>`（Claude Code Bash 直接执行），hex 结果存 `compile_tasks.fingerprint` |
| 缓存判据 | 查询 `SELECT id FROM compile_tasks WHERE raw_path=? AND fingerprint=? AND status='done' ORDER BY id DESC LIMIT 1` |
| 命中处理 | 跳过 LLM 调用；插入新任务记录 `status='cached'`；不重写 NEXUS 产物 |
| 未命中处理 | 正常编译；`status` 依次 pending → processing → done/failed |
| 变更检测边界 | Demo 只按文件整体 SHA256；不区分"仅标题变"等微变更 |

### 5.7 错误处理表

| 场景 | 检测方式 | 处理 | 用户可见反馈（Streamlit） |
|------|---------|------|--------------------------|
| 格式不支持（.jpg 等） | Streamlit 上传前扩展名白名单：.md/.txt/.pdf/.docx | 拒绝保存 | 🔴 "不支持的文件格式：.jpg。支持的格式：.md, .txt, .pdf, .docx" |
| 文件过大（>10MB） | Streamlit 上传前 size 检查 | 拒绝保存 | 🔴 "文件大小超过限制（10MB）" |
| PDF 无法解析（加密/扫描件） | Claude Code 用 PyPDF2 提取文本，返回空或乱码 | 标记 failed | ⚠️ "PDF 文本提取失败：文件可能为扫描件或加密文档" + [重试] |
| LLM 返回非 JSON | Agent 解析失败 | 自动重试 1 次（重读 prompt 后重发）→ 仍失败则 failed | ⚠️ "编译引擎返回了非预期格式。系统已自动重试 1 次，仍失败。" + [查看原始返回] + [重试] |
| LLM 调用超时（单文件 >60s） | Agent 计时 | 自动重试 1 次 → 仍超时则 failed | ⚠️ "编译超时（已等待 60 秒）" + [重试] |
| 编译成功但 concepts=[] 且正文>50字 | Agent 检查 | 正常完成，任务记录 done，error_msg 备注"未提取到概念" | ℹ️ "编译完成，但未识别出独立知识概念。文档可能为过渡性内容。" |
| 内容 <50 字 | Agent 检查 | 按 compile_prompt 特殊规则输出 | ℹ️ 正常展示 |
| compile_tasks 写入失败（SQLite 锁） | sqlite3 报错 | Agent 重试 1 次；仍失败在 stderr 记录 | ⚠️ "编译完成但状态记录失败，请点击[重建索引]" |

**重试语义**：[重试] 按钮仅显示于 failed 任务行。点击 = 为该 RAW 路径重新写编译触发文件；Claude Code 消费时若存在相同 fingerprint 的 done 记录则按缓存处理（说明上次失败发生在缓存记录之后，直接复用产物并标记 cached），否则重新执行 LLM 编译。

---

## 6. 审核流转设计

### 6.1 审核流程时序

```
Claude Code(审核Agent)     SQLite                Streamlit(review.py)      Vault 文件系统
        │                    │                          │                       │
        │ 1.消费 review_*.md │                          │                       │
        │ 2.读待审条目全文   │                          │                       │
        │ 3.六维度并行审核   │                          │                       │
        │ 4.INSERT pending_ │                          │                       │
        │    reviews         │                          │                       │
        │───────────────────►│                          │                       │
        │                    │  5.审核页查询             │                       │
        │                    │  human_decision IS NULL  │                       │
        │                    │◄─────────────────────────│                       │
        │                    │ 6.渲染列表+AI判定+预览    │                       │
        │                    │─────────────────────────►│                       │
        │                    │                          │ 7.点击[✓通过]         │
        │                    │                          │ 8.移动文件 pending_   │
        │                    │                          │   review/ → NEXUS/   │
        │                    │                          │   概念/（改YAML       │
        │                    │                          │   status=active）    │
        │                    │                          │──────────────────────►│
        │                    │ 9.UPDATE knowledge_      │                       │
        │                    │   entries(DELETE+INSERT) │                       │
        │                    │   + pending_reviews      │                       │
        │                    │◄─────────────────────────│                       │
        │                    │                          │ 10.追加 index.md 概念行│
        │                    │                          │──────────────────────►│
```

### 6.2 AI 审核编排（workflows/review_workflow.md）

```
1. 解析 review_*.md 中的待审条目路径列表
2. 对每个条目：
   a. cat 读取 Markdown 全文 + YAML
   b. 构造去重候选列表：
      grep -l 标题关键词 NEXUS/ pending_review/（排除自身）
      → 每个候选取路径+标题+前200字正文，最多 5 个
   c. Harness parallel 派发 6 个子 Agent（每维度一个）：
      维度1 完整性   → 确定性检查（字段存在性+字数，不依赖 LLM，可直接用 bash/python 判定）
      维度2 去重     → LLM 语义相似度对比（对候选列表）
      维度3 职务归属 → LLM 语义判定
      维度4 质量     → LLM 四子维度评分
      维度5 敏感信息 → 确定性正则检查（身份证/手机号/邮箱/API Key/密码/大额金额/机密标记）
      维度6 合规     → LLM 语义判定
   d. 汇总子 Agent 结果 → 按 review_prompt.md 判定逻辑链计算 verdict
   e. sqlite3 INSERT INTO pending_reviews
      (nexus_path, submitter='demo_user', department, ai_verdict, ai_scores=完整JSON, created_at)
3. 全部条目处理完 → 触发文件移入 done/
```

**确定性维度不派发 LLM 子 Agent**：维度 1（完整性）与维度 5（敏感信息）是纯规则检查，由主 Agent 用 bash/python 直接判定，只有维度 2/3/4/6 需要 LLM。这样可以减少 LLM 调用量约 1/3，且规则维度结果 100% 可复现（与 TDD 测试用例对齐，见 10.2）。

> **与 PRD 8.5 的措辞差异说明**：PRD 8.5 Harness 示例 2 写「parallel 6 个子 Agent，每维度一个」，但 PRD 8.3 TDD 明确「敏感信息检测（正则匹配）和完整性检查（字段存在性）是纯逻辑规则，不依赖 LLM，可直接断言」。两处冲突时本文按 8.3 执行：**4 个 LLM 子 Agent（维度 2/3/4/6）+ 2 个确定性检查（维度 1/5）并行**，汇总逻辑与 6 个子 Agent 方案完全一致（任一维度 blocked/insufficient 的否决权不变）。

### 6.3 审核 Agent 输入输出契约

**输入**：
1. 待审条目全文（含 YAML Frontmatter）
2. 候选重复条目列表（可能为空）：`[{"path": ..., "title": ..., "excerpt": 前200字}, ...]`
3. 提交者：`demo_user`（Demo 单用户固定）

**输出**：严格按 `prompts/review_prompt.md` 的 JSON Schema（PRD 3.1 已列），落库为 `pending_reviews.ai_scores`。字段速查：`verdict / department / scores{completeness, dedup, quality, sensitive, compliance} / duplicates / concerns / summary`。

### 6.4 Streamlit 审核页设计

**数据来源**：`SELECT * FROM pending_reviews WHERE human_decision IS NULL ORDER BY created_at DESC`，JOIN knowledge_entries 取标题。

**页面布局**：

```
┌────────────────────────────────────────────────────────┐
│ 待审核列表（N 条）                                       │
│ ┌──────────┬──────────┬──────────┬──────────────────┐ │
│ │ 条目     │ 部门      │ AI 判定   │ 操作              │ │
│ │ 示例监测产品 │ 产品     │ ✅通过   │ [查看详情]        │ │
│ └──────────┴──────────┴──────────┴──────────────────┘ │
│                                                        │
│ 选中条目详情                                            │
│ ┌─────────────────────┬──────────────────────────────┐ │
│ │ AI 六维度评分面板     │ Markdown 预览（读文件渲染）   │ │
│ │ 完整性 pass          │ # 示例监测产品                    │ │
│ │ 去重   pass          │ ## 定义 ...                  │ │
│ │ 质量   4/5           │                              │ │
│ │ 敏感   pass          │                              │ │
│ │ 合规   pass          │                              │ │
│ │ 归属   产品           │                              │ │
│ │ concerns: [...]      │                              │ │
│ ├─────────────────────┴──────────────────────────────┤ │
│ │ [✓ 通过]  [✗ 驳回]   （驳回时弹出原因输入框，必填）    │ │
│ └────────────────────────────────────────────────────┘ │
│                                                        │
│ 已驳回条目（可重新提交）                                 │
│ ┌──────────┬──────────┬──────────────────────────────┐ │
│ │ 条目     │ 驳回原因  │ [重新提交审核]                 │ │
│ └──────────┴──────────┴──────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

**交互逻辑**：

| 按钮 | 动作序列（review.py 内） |
|------|------------------------|
| [✓ 通过] | 1) 移动文件 `pending_review/X.md` → `NEXUS/概念/X.md` 2) 改 YAML `status: active` 3) `db.py::move_entry(old_path, new_path, 'active')` + `db.py::set_human_decision(review_id, 'approved')` 4) 追加 index.md 概念行 |
| [✗ 驳回] | 1) 弹出原因输入框（必填） 2) 改 YAML `status: draft` 3) `db.py::update_status(path, 'draft')` + `db.py::set_human_decision(review_id, 'rejected', reason)` |
| [重新提交审核] | 1) 改 YAML `status: pending` 2) `db.py::update_status(path, 'pending')` + `db.py::resubmit_review(review_id)` 3) 写 `_triggers/review_<ts>.md`（含该条目路径） |

### 6.5 人工决定状态机（与 4.4 对应）

| 当前态 | 事件 | 次态 | YAML status | knowledge_entries.status | 文件位置 |
|--------|------|------|-------------|--------------------------|---------|
| pending | AI 审核完成 | pending（待人工） | pending | pending | pending_review/ |
| pending | 人工通过 | active | active | active | NEXUS/概念/ |
| pending | 人工驳回 | draft | draft | draft | pending_review/ |
| draft | 重新提交 | pending | pending | pending | pending_review/ |

**不变量**：任何时刻 `pending_review/` 中文件的状态 ∈ {pending, draft}；`NEXUS/概念/` 中文件的状态 = active。

### 6.6 错误处理表

| 场景 | 处理 | 用户可见反馈 |
|------|------|-------------|
| AI 审核 LLM 超时 | 该条目标记 ai_verdict=NULL，human_decision 仍 NULL | ⚠️ "AI 审核暂时不可用。" + [重试 AI 审核]（重写 review 触发文件）+ [跳过 AI，直接人工审核]（审核页仍可人工操作） |
| AI 审核结果 JSON 解析失败 | 主 Agent 重试 1 次 | ⚠️ "AI 审核结果解析失败。已自动重试，请查看下方原始输出。" + [重试] |
| 通过时目标文件已存在（同名概念已在 NEXUS/概念/） | 追加 `-2` 后缀后再移动，concerns 自动加一条 | ⚠️ "目标位置已存在同名条目，已重命名为《X-2》并在索引中注明。" |
| 移动文件失败（文件被 Obsidian 锁定） | 重试 1 次；仍失败保持原状并提示 | 🔴 "文件移动失败：可能被其他程序占用。请关闭该文件后重试。" |

---

## 7. 检索问答设计

### 7.1 检索通道（Demo）

| 通道 | 实现 | 适用 | 工程成本 |
|------|------|------|---------|
| Obsidian 原生搜索 | Ctrl+Shift+F，用户直接操作 | 用户在 Obsidian 中找知识 | 零（Obsidian 自带） |
| Claude Code 问答（/ask） | `grep -rl "关键词" vault/NEXUS/ --include="*.md"` + 排序 | 自然语言提问 + 答案生成 | 一个命令文件 |
| Streamlit 侧边栏搜索框 | `grep` 同款逻辑 + search_logs 记录 | 演示搜索反馈闭环 | db.py + app.py |

**搜索范围**：仅 `NEXUS/`（资源 + 概念 + 研究）。`pending_review/` 与 `RAW/` 不进检索（未审核内容不可见）。

**关键词切分**：查询串按空格与常见标点切分为词；每个词独立 grep，文件匹配行数 = 各词命中行数之和；文件新鲜度 = YAML `updated` 字段距今天数。

### 7.2 排序策略（PRD 4.x 落地）

```
score(file) = 0.7 × 匹配行数归一化 + 0.3 × 新鲜度归一化
匹配行数归一化 = 该文件命中行数 / 本批最大命中行数
新鲜度归一化   = 1 / (1 + 距今天数)      # 当天=1，一年前≈0.006
取 Top-5
```

实现为 Claude Code Bash 管道（或 Streamlit 中 Python 等价实现），无外部依赖。

### 7.3 问答流程（/ask 命令，Claude Code 内）

```
1. 用户键入 /ask 示例监测产品有哪些部署模式？
2. grep 检索（7.2 排序）→ Top-5 文件
3. cat 读取 5 个文件全文（单文件 <50KB，5 个 ≈ 最多 25 万字符，超长时截断至前 8000 字/文件）
4. 执行 prompts/answer_prompt.md：
   - 检索结果非空且相关 → 按六种策略之一生成答案
   - 空结果 → 「无匹配结果」策略 + 建议关键词
5. sqlite3 INSERT search_logs (query, match_count=Top-5 实际数, source='claude_code')
6. 呈现答案：引用来源写成 Vault 相对路径（Obsidian 中可点击跳转）
```

**交互边界（明确）**：Demo 的问答入口在 Claude Code 终端（/ask）与 Streamlit 侧边栏。Obsidian 内无问答面板——Obsidian 是纯本地应用，无调 Claude Code 的通道；若未来要 Obsidian 内嵌问答，需开发 Obsidian 插件（超出 Demo 范围，Phase 3 候选）。

### 7.4 search_logs 记录时机

| 来源 | 记录时机 | source 字段 |
|------|---------|-------------|
| Streamlit 侧边栏搜索 | 用户点击「搜索」按钮时，先 grep 得 match_count 再 INSERT | streamlit |
| Claude Code /ask | 答案生成前 INSERT（第 5 步） | claude_code |
| Obsidian 原生搜索 | **不记录**（无拦截通道）——已知限制，见 8.4 | — |

### 7.5 错误处理

| 场景 | 处理 |
|------|------|
| grep 零命中 | 按 answer_prompt「无匹配结果」策略回应 + search_logs 记 match_count=0（这正是自增长燃料） |
| 命中文件超长 | 每文件截断至前 8000 字，注明「（内容过长已截断，查看完整文件请打开路径）」 |
| 用户问题含错别字 | answer_prompt 已定义：纠正后再检索，回答中自然纠正 |

---

## 8. 自增长引擎设计

### 8.1 搜索反馈闭环（Demo 范围）

```
搜索(search_logs) → 缺口发现(实时 SQL + 周度聚类) → 看板展示 → 驱动上传 → 编译入库 → 缺口缩小
```

**两级实现**：

| 级 | 实现 | 触发 | 产出 |
|----|------|------|------|
| 实时缺口列表 | growth.py 直接聚合 SQL | 页面每次刷新 | 按 query 精确分组、match_count=0 的 Top 20 |
| 周度缺口聚类 | growth_workflow.md（LLM 聚类同义查询） | Claude Code 手动执行 /process-growth（或并入 /process-triggers） | `NEXUS/研究/自增长周报_YYYY-MM-DD.md` |

### 8.2 growth_workflow.md 设计

```
1. sqlite3 导出近 7 天 search_logs：
   SELECT query, COUNT(*) cnt FROM search_logs
   WHERE timestamp >= date('now','-7 days') AND match_count=0
   GROUP BY query ORDER BY cnt DESC LIMIT 100
2. LLM 聚类：把语义相同的查询合并（如「示例监测产品价格」≈「哨兵报价」）
3. 对每个聚类：猜测它对应哪类缺失文档（产品资料/技术方案/…），给出建议上传方向
4. 生成 NEXUS/研究/自增长周报_YYYY-MM-DD.md：
   # 自增长周报
   ## 本周知识缺口 Top 20
   | 排名 | 缺口主题 | 搜索次数 | 建议补充文档 |
   ## 上周已补缺口（对比上周报中缺口与本周新入库条目）
5. 更新 knowledge_entries（周报本身 type=research, status=active）
```

### 8.3 看板数据流

```
growth.py 渲染：
├── 卡片1：实时缺口 Top 20（SQL 直查，实时）
├── 卡片2：最近一份周报内容（读 NEXUS/研究/ 下最新周报文件渲染 Markdown）
└── 卡片3：搜索统计（总搜索数/未命中率，SQL 聚合）
```

### 8.4 局限与边界（明确声明）

| 局限 | 影响 | 缓解 |
|------|------|------|
| Obsidian 原生搜索不写 search_logs | 数据偏少，只统计 Streamlit 搜索框和 /ask | 演示时用侧边栏搜索框演示闭环；文档注明 |
| 缺口发现不自动触发编译 | 闭环最后一步靠人 | 看板「建议补充文档」列即为驱动 |
| 无点击日志 | 无法区分「搜到了但没点开」 | Phase 2 引入 |

---

## 9. Streamlit 管理层设计

### 9.1 总体结构

```
app.py（入口）
├── st.set_page_config(标题="LLM Wiki 管理台", 布局="wide")
├── 侧边栏（st.sidebar）：
│   ├── 视角切换下拉框（贡献者/审核者/消费者）——仅控制页面可用性提示，Demo 不做真权限
│   ├── 搜索框 + [搜索] 按钮 → 结果列表 + search_logs 记录
│   ├── [重建索引] 按钮 → db.py::rebuild_index()
│   └── 页面导航 radio（上传文档 / 审核管理 / 自增长看板）
├── 路由：
│   ├── upload.py::render()    # 上传页
│   ├── review.py::render()    # 审核页
│   └── growth.py::render()    # 看板页
└── 环境变量：KB_ROOT、DB_PATH（os.environ 读取，Docker 传入）
```

### 9.2 db.py 函数清单（编码契约）

```python
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ["DB_PATH"]  # /app/vault/meta.db

@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """SQLite 连接上下文。WAL 模式，timeout=5s。"""

def upsert_entry(path: str, type_: str, title: str,
                 department: str | None, status: str,
                 version: str, fingerprint: str | None,
                 updated_at: str) -> None:
    """INSERT OR REPLACE INTO knowledge_entries。"""

def update_status(path: str, status: str) -> None:
    """更新 knowledge_entries.status（不触碰文件，文件由调用方改）。"""

def move_entry(old_path: str, new_path: str, status: str) -> None:
    """DELETE 旧 path 行 + INSERT 新 path 行（保留其他字段，需调用方先读旧行）。"""

def insert_compile_task(raw_path: str, fingerprint: str) -> int:
    """插入 status='pending' 任务，返回 id。"""

def update_compile_task(task_id: int, status: str,
                        nexus_path: str | None = None,
                        error_msg: str | None = None) -> None:
    """更新任务状态与完成时间。"""

def insert_review(nexus_path: str, submitter: str, department: str,
                  ai_verdict: str, ai_scores: str) -> int:
    """插入 AI 审核结果，返回 id。"""

def set_human_decision(review_id: int, decision: str,
                       reject_reason: str | None = None) -> None:
    """人工通过/驳回。"""

def resubmit_review(review_id: int) -> None:
    """重新提交审核：human_decision 置 NULL、清空 reject_reason（Demo 复用同一行）。"""

def list_pending_reviews() -> list[dict]:
    """human_decision IS NULL 的审核记录（JOIN entries 取标题）。"""

def list_rejected_reviews() -> list[dict]:
    """human_decision='rejected' 的记录。"""

def insert_search_log(query: str, match_count: int, source: str) -> None:
    """写入搜索日志（timestamp 用本地时间）。"""

def top_missed_queries(limit: int = 20) -> list[dict]:
    """match_count=0 的 query 按次数降序。"""

def search_stats() -> dict:
    """{total: 总搜索数, miss_rate: 未命中率}。"""

def rebuild_index() -> int:
    """扫描 KB_ROOT 下 NEXUS/**/*.md 与 pending_review/*.md，
    解析 YAML → 全量重建 knowledge_entries。返回条目数。"""
```

### 9.3 上传页设计（upload.py）

```
┌────────────────────────────────────────────────┐
│ 上传文档                                        │
│ [文件选择器 st.file_uploader（多文件）]          │
│ [分类下拉：个人_notes/会议/经验/项目]             │
│ [上传并加入编译队列] 按钮                        │
├────────────────────────────────────────────────┤
│ 编译任务状态表（读 compile_tasks，按时间降序）    │
│ ┌──────┬──────────┬────────┬──────┬─────────┐ │
│ │ 文件 │ 指纹      │ 状态   │ 产物 │ 错误    │ │
│ └──────┴──────────┴────────┴──────┴─────────┘ │
│ [重试失败任务] 按钮 → 写 compile 触发文件        │
└────────────────────────────────────────────────┘
```

**上传动作序列**：
1. 校验扩展名白名单（.md/.txt/.pdf/.docx）与大小（≤10MB）——不合规直接 st.error，不落盘
2. `KB_ROOT/RAW/<分类>/<原始文件名>` 保存
3. 原子写 `_triggers/compile_<ts>.md`（含该 RAW 路径）
4. `insert_compile_task(raw_path, fingerprint)`——Streamlit 侧先算好 SHA256 并插入 pending 任务，Claude Code 编译时更新
5. st.success("已加入编译队列。Claude Code 处理中——状态见下表")

**任务状态轮询**：`st.rerun` 定时（`st_autorefresh` 或 st.button 手动刷新）；状态流转 pending → processing → done/cached/failed 由 Claude Code 通过 sqlite3 更新。

### 9.4 审核页设计（review.py）

见 6.4 布局与交互逻辑。补充实现要点：
- Markdown 预览：直接读文件 `Path(KB_ROOT)/nexus_path`，`st.markdown(body)` 渲染
- 六维度评分面板：`json.loads(ai_scores)` 后按 6.4 布局渲染；`ai_verdict IS NULL` 时显示「AI 审核未完成/失败」+ [重试 AI 审核] 按钮
- 通过/驳回后 `st.rerun()` 刷新列表

### 9.5 自增长看板设计（growth.py）

见 8.3。实现要点：
- 实时缺口：`top_missed_queries(20)` 渲染 st.dataframe
- 周报：`glob(KB_ROOT/NEXUS/研究/自增长周报_*.md)` 取最新 → `st.markdown` 渲染
- 统计卡片：`search_stats()` → st.metric × 2

### 9.6 错误 UX 实现映射（PRD 9.3 落地）

| 级别 | Streamlit 组件 | 场景 |
|------|---------------|------|
| 🔴 阻塞 | `st.error` | 格式不支持、超大小、文件移动失败 |
| ⚠️ 可恢复 | `st.warning` | PDF 提取失败、编译超时/非 JSON、AI 审核不可用、同名冲突 |
| ℹ️ 提示 | `st.info` | 编译完成无概念、搜索无结果 |

所有错误信息三要素：问题描述 / 可能原因 / 建议操作（PRD 9.3 已定义文案，直接实现）。

---

## 10. 测试与验收

### 10.1 SDD 规约清单（编译引擎，8 条）

| # | given | when | then |
|---|-------|------|------|
| S1 | RAW 文件 F（.md，>500字），SHA256=H | 执行编译 | `NEXUS/资源/<标题>.md` 存在；YAML 含 type/title/source/fingerprint=H；正文含「## 摘要」「## 关键信息」；knowledge_entries 有对应行（status=active） |
| S2 | F 二次编译且 SHA256 未变 | 再次执行编译 | 不调用 LLM；compile_tasks 新增 status='cached' 记录；产物文件未变化 |
| S3 | F 正文含 3 个独立概念 | 执行编译 | 产出 1 资源 + 3 概念页（pending_review/）；概念 YAML status=pending；互有 wikilink 的概念生成 `[[概念-…]]` |
| S4 | F 有效内容 <50 字 | 执行编译 | resource.summary 含「内容不足」标注；concepts=[]；任务 done |
| S5 | F 为 .pdf | 执行编译 | 先文本提取再编译；compile_tasks 记录完整 |
| S6 | F 含 >10 行表格 | 执行编译 | 摘要中表格截断为表头+前3行+「（共 N 行，已截断）」 |
| S7 | LLM 返回非 JSON | 执行编译 | 自动重试 1 次；仍失败 → 任务 failed + error_msg 非空 |
| S8 | `_triggers/compile_<ts>.md` 处理完毕 | Claude Code 处理触发队列 | 触发文件出现在 `_triggers/done/`；`_triggers/` 根目录无残留 |

### 10.2 TDD 用例清单（审核确定性规则 + 数据层）

**完整性检查（4 例）**：四字段齐全+正文≥100字→pass；缺 type→incomplete；正文 20 字→insufficient；缺 3 项→insufficient。

**去重指纹（3 例）**：标题+前200字相同→duplicate；标题同正文异→similar；完全不同→pass。

**敏感信息正则（6 例）**：18 位身份证→blocked；11 位手机号→warning；`sk-xxx`→blocked；明文密码→blocked；"机密"水印→blocked；无敏感内容→pass。

**状态机（4 例）**：通过→路径 pending_review→NEXUS/概念 且 status active；驳回→status draft 留在 pending_review；重新提交→status pending；move_entry 后 knowledge_entries 行数不变（DELETE+INSERT）。

**数据层（3 例）**：rebuild_index 从文件重建后行数与 NEXUS+ pending_review 文件总数一致；insert_search_log 后 top_missed_queries 正确聚合；同名概念移动冲突 → 追加 `-2` 后缀。

**文件名清洗（2 例）**：`A/B:C*D?.md` → 非法字符移除；空串回退「未命名」。

**合计 22 例**，全部为确定性断言（无 LLM），pytest 运行于容器外（tests/ 目录，Python 环境 + vault 副本）。

### 10.3 验收标准

**PRD 9.4 脚本 A（核心闭环，5 分钟）走通，检查点**：

| 时间点 | 检查点 |
|--------|--------|
| 00:30 | 上传成功、编译任务出现在状态表 |
| 01:00 | Claude Code 完成编译，任务状态 done，触发文件入 done/ |
| 01:30 | 审核页显示 AI 六维度判定，通过操作成功 |
| 02:30 | Obsidian 中 NEXUS/概念/ 出现新页，图谱可见关联 |
| 03:30 | Obsidian 搜索"叫应体系"命中 |
| 04:30 | 看板显示知识缺口（预置 search_logs 种子） |

**脚本 B（驳回，2 分钟）**：驳回→原因记录→重新提交→再次进入待审列表。

**边界验收**：上传 .jpg 被拒（🔴）；搜索无结果给出建议（ℹ️）；断开 LLM 时编译显示可恢复错误（⚠️）。

---

## 11. 风险与待定问题

### 11.1 技术风险表

| # | 风险 | 概率 | 影响 | 缓解 |
|---|------|------|------|------|
| R1 | LLM 编译输出非确定性（同一文档两次编译结果不同） | 高 | 中 | SHA256 缓存使同一文件只编译一次；审核门兜底；异常结果人工可见 |
| R2 | YAML 与 SQLite 不一致 | 中 | 中 | 写路径双写铁律（4.1）；「重建索引」按钮；验收测试覆盖 rebuild_index |
| R3 | 触发文件处理竞态（Streamlit 写入瞬间 Claude Code 扫描） | 低 | 低 | 原子写（.tmp + mv）；扫描只认 .md 一级文件 |
| R4 | Windows Docker 挂载中文文件名乱码 | 低 | 高 | 全链路 UTF-8；init.sh 生成中文目录作为冒烟测试；Docker volume 不做转码 |
| R5 | Claude Code 未运行 → 编译队列堆积 | 中 | 中 | Streamlit 状态表显示 pending 堆积数量；演示脚本明确"编译由 Claude Code 处理"这一步 |
| R6 | 大文档超出 Claude Code 上下文 | 低 | 中 | 单文件 ≤10MB 上传限制；编译按文件独立执行（pipeline item 粒度） |
| R7 | sqlite3 并发写锁（Streamlit 与 Claude Code 同时写） | 低 | 低 | WAL 模式 + busy_timeout=5000；两写方操作时间窗口天然错开（人 vs agent） |

### 11.2 已知限制

1. Obsidian 原生搜索不写 search_logs（8.4）
2. 问答入口在 Claude Code / Streamlit，不在 Obsidian 内（7.3）
3. Demo 单用户：submitter 固定 'demo_user'，无认证
4. 资源摘要自动发布、概念页走审核——若未来要求资源也审核，改动点在 5.3 落盘动作与 4.4 状态机

### 11.3 待定问题

| # | 问题 | 影响范围 | 建议决策时机 |
|---|------|---------|-------------|
| O1 | PRD 头部"五层架构"与第四章 4 层架构图不一致（1.5#1），PRD 是否改 | 文档 | PRD 下次修订 |
| O2 | 周度缺口聚类的执行入口：独立 /process-growth 命令 vs 并入 /process-triggers | 8.1 | 编码时定（倾向并入，少一个命令） |
| O3 | Streamlit 搜索框是否展示完整答案（调 Claude Code）还是只展示 grep 结果列表 | 7.1 | 编码时定（倾向只展示结果列表+跳转 Obsidian，答案生成统一走 /ask） |
| O4 | done/ 归档清理策略（Demo 不清理，Phase 2 定） | 4.5 | Phase 2 |

---

**文档状态**：待评审（v0.1）
**作者**：何豫东
**更新**：2026年8月13日
