# 企业级LLM Wiki知识库平台 —— 产品需求文档（PRD）

**版本**：v1.7
**状态**：Demo 就绪稿
**作者**：何豫东
**日期**：2026年8月11日
**变更**：架构简化——移除 MCP Server 桥接层，Claude Code 通过 Bash 工具直接操作 Vault 和 SQLite；四层架构（原六层减二：桥接层移除 + 元数据层并入存储层）


## 一、产品概述

### 1.1 产品定位

本产品是一个**基于LLM Wiki范式、符合Google OKF规范的企业级知识流转系统**。

核心定位不是"又一个RAG知识库"，而是**让知识在企业内从个人沉淀→审核流转→企业共享的完整闭环**。产品以"知识编译"为核心思想——将原始文档编译为结构化、可链接、可持续演进的Markdown Wiki。

### 1.2 核心理念

本产品基于Karpathy提出的LLM Wiki模式：**摒弃传统RAG"每次查询重新检索"的解释器模式，转向"一次编译、持续复用"的编译器模式**。知识在入库时被编译为结构化的Markdown文件，后续查询直接读取编译产物，而非反复检索原始文档。

### 1.3 与RAG的关系澄清

**本产品不是"不需要RAG"，而是做了一次范式升级**。

| 范式 | 传统RAG（解释器模式） | 本产品（编译器模式） |
|------|---------------------|---------------------|
| **知识理解时机** | 查询时（run-time） | 入库时（compile-time） |
| **检索对象** | 不可读的向量chunk | 结构化Markdown + YAML Frontmatter |
| **查询流程** | query → embed → retrieve chunks → generate | query → grep/向量/图谱三通道检索Markdown条目 → **generate答案** |
| **LLM调用** | 每次查询都调用LLM理解文档+生成答案 | 入库时LLM编译一次（理解文档、提取概念），查询时LLM仅负责基于已编译知识生成答案 |

**关键澄清**：本产品在查询阶段仍然需要 Retrieval + Generation（即"RG"部分），但检索物从碎片化 chunk 变成了已编译的结构化知识条目。模块四（混合检索）的输出是相关知识 Markdown 文件列表，**之后还有一个答案生成步骤（LLM基于检索结果生成自然语言回答）**，否则检索产出物（文件列表）与用户期望产出物（自然语言答案）之间存在缺口。

> **一句话总结**：从 Run-time RAG → Compile-time + Run-time RAG（编译时理解 + 运行时检索生成）。

### 1.4 差异化定位

| 对比维度 | 传统RAG知识库 | 本产品（LLM Wiki企业版） |
|---------|-------------|----------------------|
| 知识组织 | 向量检索（查询时动态检索） | 知识编译（入库时静态编译） |
| 知识表示 | 向量片段（不可读） | Markdown + YAML Frontmatter（人类可读） |
| 知识流转 | 单向存储 | 个人→审核→企业的完整流转链路 |
| 可追溯性 | 弱（只能追溯到文档片段） | 强（每个知识点保留来源声明） |
| 多源同步 | 不支持 | 冲突检测 + 去重 + 贡献者聚合 |
| 检索-生成 | chunk → LLM直接生成 | 结构化知识 → 关键词/语义检索 → LLM生成（附可点击来源） |

### 1.5 与Google OKF的对齐

本产品严格遵循Google于2026年6月发布的**Open Knowledge Format（OKF）v0.1规范**：

- **Just Markdown**：知识主体使用标准Markdown
- **YAML Frontmatter**：每个知识文件顶部带结构化元数据，`type`为唯一必填字段
- **Reserved Files**：保留 `index.md`（渐进式目录，编译时逐次更新）和 `log.md`（审计日志，Demo 创建空文件占位，Phase 2 起记录操作事件）
- **容错消费**：Agent不因缺少可选字段而拒绝解析（消费端对缺失字段使用安全默认值）
- **厂商中立**：无SDK或专有工具依赖


## 二、目标用户与使用场景

### 2.1 用户角色

| 角色 | 职责 | 主要工具 | 使用频率 |
|------|------|---------|---------|
| **知识贡献者** | 上传文档、撰写知识条目、发起审核 | Streamlit（上传）+ Obsidian（浏览） | 日常（每天） |
| **知识审核者** | 审核pending_review中的知识，决定通过/驳回 | Streamlit（审核面板）+ Obsidian（预览原文） | 每周2-3次 |
| **知识消费者** | 检索和阅读知识库内容 | Obsidian（搜索、浏览、图谱） | 日常（每天） |
| **系统管理员** | 管理用户权限、监控知识库健康度与自增长 | Streamlit（管理看板）+ Obsidian（全库视角） | 每周1次 |

> **Obsidian 作为全用户前端**：所有角色的知识浏览、搜索、图谱可视化、wikilink 导航均通过 Obsidian 完成。Streamlit 仅覆盖 Obsidian 不擅长的结构化表单操作——上传文档、审批、数据看板。

### 2.2 核心使用场景

| 场景 | 用户角色 | 典型需求 |
|------|---------|---------|
| **个人知识沉淀** | 知识贡献者 | 将个人笔记、项目复盘、技术方案入库 |
| **知识审核流转** | 知识审核者 | 审核个人贡献的知识，决定是否进入企业库 |
| **多源同步** | 知识贡献者 + 系统 | 多个人向同一企业库贡献时自动去重 |
| **企业知识检索** | 知识消费者 | 用自然语言查询企业知识库，获得结构化答案 |
| **知识健康巡检** | 系统管理员 | 定期检查知识库质量，发现孤立节点和冲突 |

### 2.3 权限模型概述

| 角色 | 读权限 | 写权限 | 审核权限 | 管理权限 |
|------|--------|--------|---------|---------|
| 知识贡献者 | 个人RAW + 企业NEXUS | 个人RAW（仅自己） | 无 | 无 |
| 知识审核者 | 全库 | 无 | pending_review（指定部门） | 无 |
| 知识消费者 | 企业NEXUS | 无 | 无 | 无 |
| 系统管理员 | 全库 | 全库 | 全库 | 用户管理、系统配置 |

> **Demo 阶段**：单用户模式，无角色区分，无登录。所有操作（上传/审核/搜索）对当前用户开放。Phase 2 引入 JWT 认证后实现管理员/普通用户区分。Phase 3 实现完整 RBAC。


## 三、功能需求

### 3.1 核心功能模块

#### 模块一：知识编译引擎（核心）

**功能描述**：将RAW目录中的原始文档编译为结构化的NEXUS知识层。

##### 编译引擎输入-输出规约

**输入**：RAW 目录下的原始文档，编译前先经过文本提取（将 .pdf/.docx 转为纯文本，.md/.txt 直接读取）。LLM 编译引擎消费的是提取后的纯文本，不直接处理二进制格式。
**输出**：NEXUS 目录下的编译产物，分为三类：

| 产物类型 | 目录 | 说明 | 生成规则 |
|---------|------|------|---------|
| **资源摘要** | `NEXUS/资源/` | 源文件的内容摘要和元信息 | 每个 RAW 文件对应一个资源摘要，保持 1:1 映射 |
| **概念页** | `NEXUS/概念/` | 从文档中提取的独立知识概念 | 由 LLM 从 RAW 中识别独立知识单元，1 个文档可产生 0~N 个概念页 |
| **研究简报** | `NEXUS/研究/` | 跨文档的综合分析或深度研究 | 由 LLM Agent 跨多个关联文档综合生成，通常由用户主动发起 |

**编译流程**（单个文件）：

```
RAW文件 → 格式解析(text提取) → 指纹计算(SHA256) → 缓存命中检查
         ↓ 命中 → 复用已有产物
         ↓ 未命中 →
    ┌─── LLM编译 ───┐
    │ 1. 资源摘要生成 │ → NEXUS/资源/{分类}/{文件名}.md
    │ 2. 概念识别     │ → NEXUS/概念/{概念名}.md
    │ 3. 跨文档关联   │ → 更新 index.md 和相关 wikilink
    │ 4. 去重检测     │ → 与已有概念对比，重复则标记为候选合并
    └────────────────┘
```

**核心能力**：

| 能力 | 说明 | 实现方式 | 阶段 |
|------|------|---------|------|
| **全量编译** | 扫描 RAW 目录，全部文件重新编译 | 遍历目录 → 指纹计算 → LLM 编译 → 写入 NEXUS | Demo |
| **缓存复用** | 未变更的源文件复用已有编译结果 | 文件 SHA256 指纹命中 → 跳过 LLM 调用，引用已有 NEXUS 产物 | Demo |
| **OKF兼容输出** | 编译结果符合OKF规范（Markdown + YAML Frontmatter） | 模板化输出，type 字段强制注入 | Demo |
| **增量编译** | 只处理新增或变更的文件，不全量重建 | file watcher 监听 RAW 目录变更，仅编译变更文件 | Phase 2 |
| **断点续跑** | 编译中断后可恢复，不重复执行已完成的工作 | compile_tasks 状态表追踪每个文件状态 | Phase 2 |

> **编译触发方式**：Demo 阶段采用**触发文件信号机制**——Streamlit 上传页将待编译 RAW 路径列表写入 `vault/_triggers/compile_*.md`（原子写入），Claude Code 通过 SessionStart hook 或 `/process-triggers` 命令扫描触发目录并执行编译。Phase 2 增加 file watcher 自动触发增量编译。

#### 模块二：个人→企业知识流转

**功能描述**：实现从个人知识库到企业知识库的完整审核流转链路。

**流转路径**：

```
个人知识库 RAW → ingest编译 → NEXUS摘要 → 审核Agent判定 → [人工通过/驳回] → 企业知识库
```

> **Demo 阶段**：审核 Agent 六维度判定后，由人工在 Streamlit 界面做出最终通过/驳回决定。不做独立的终审 Agent。

**子功能**：

| 子功能 | 说明 | Demo 范围 |
|------|------|---------|
| **个人知识入库** | 用户上传文件 → 存入个人RAW → 触发ingest编译 | ✅ 全做 |
| **审核Agent判断** | 六维度判断（见下方判定标准） | ✅ 全做 |
| **pending_review缓冲** | 审核通过前暂存 | ✅ 简化版（不按职务目录分类，统一存放在 pending_review/ 根目录） |
| **人工审核** | 审核者在 Streamlit 界面查看 AI 六维度评分，点击通过/驳回 | ✅ Demo 核心 |
| **企业终审Agent** | 三道审核：敏感复核 → 去重+冲突检测 → 综合判定 | ❌ Demo 不做（合并到审核 Agent 一步完成） |
| **人工兜底** | 边界案例进入待人工目录，由运营后台审核 | ❌ Demo 不做 |

##### 审核Agent六维度判定标准

| 维度 | 判定标准 | 判定结果 | 权重 |
|------|---------|---------|------|
| **完整性** | 知识条目是否包含 title + type + 来源声明 + 正文≥100字 | pass / incomplete / insufficient | 必过 |
| **去重检查** | 标题+正文前200字符指纹是否与NEXUS已有条目重复 | pass / duplicate / similar(>70%) | 必过 |
| **职务归属** | 基于内容语义判断知识应归属的部门分类（销售/售前/产品/实施交付/开发/财务/人事/行政/共享层） | 归属部门标签 | — |
| **质量评估** | 内容是否逻辑清晰、无明显事实错误、格式规范 | score(1-5) | ≥3 |
| **敏感信息** | 是否包含手机号/身份证/密钥/商业机密等敏感内容 | pass / warning / blocked | blocked一票否决 |
| **合规检查** | 是否符合企业知识管理规范（不含攻击性内容、不侵犯第三方IP） | pass / flagged | pass |

**审核Agent输出格式**（JSON Schema）：

```json
{
  "verdict": "approved | rejected | needs_human_review",
  "department": "销售",
  "scores": { "completeness": "pass", "dedup": "pass", "quality": 4, "sensitive": "pass", "compliance": "pass" },
  "duplicates": ["NEXUS/概念/xxx.md"],
  "concerns": ["数据指标缺少明确来源"],
  "summary": "知识条目质量较高，建议通过"
}
```

> **跨部门归属处理**：当知识涉及多个部门时，按**主导部门入主目录 + 共享层标记多部门可见性**。例如：一份涉及"销售+产品"的文档，归入销售目录，同时在共享层创建链接。

#### 模块三：多源同步与去重

**功能描述**：多个个人知识库向同一企业知识库同步时，自动处理冲突和重复。

| 能力 | 说明 |
|------|------|
| **内容指纹去重** | 基于标题+正文前200字符生成SHA256指纹，相同内容不重复入库 |
| **贡献者聚合** | 相同内容被多人提交时，合并贡献者列表（YAML Frontmatter 中 contributors 字段为数组） |
| **版本管理** | 首次入库V1.0，内容变更后版本号自动递增（格式：V{major}.{minor}） |
| **冲突检测** | 检测同主题知识的事实性矛盾，生成冲突报告（存conflicts表） |

#### 模块四：混合检索 + 答案生成

**功能描述**：支持三通道混合检索，检索到的结构���知识经 LLM 融合生成自然语言答案。

**Demo 查询链路**（Phase 1 实际实现）：

```
用户自然语言查询
    → grep 精确匹配（在 NEXUS 目录 Markdown 文件中搜索关键词）
    → 按匹配行数 + 文件新鲜度排序
    → Top-K 知识条目（Markdown全文）作为上下文
    → LLM 答案生成（基于结构化知识生成自然语言回答，附引用来源）
```

**完整查询链路**（Phase 2-3 目标）：

```
用户自然语言查询
    → 查询意图分类（精确匹配/语义理解/关联探索）
    → 三通道并行检索：
        ├── grep 精确匹配（术语、编号、代码）
        ├── 向量语义检索（模糊描述、概念理解）—— Phase 2
        └── 知识图谱扩展（跨知识点、关系推理）—— Phase 3
    → 三通道结果融合排序（re-rank）—— Phase 2
    → Top-K 知识条目（Markdown全文）作为上下文
    → LLM 答案生成（基于结构化知识生成自然语言回答，附引用来源）
```

| 检索通道 | 说明 | 适用场景 | 实现阶段 |
|---------|------|---------|---------|
| **grep精确匹配** | 基于关键词的全文搜索 | 精确术语、编号、代码片段 | Demo |
| **LLM 答案生成** | LLM 融合检索结果，生成带引用的自然语言回答 | 所有查询 | Demo |
| **向量语义检索** | 基于 Embedding 的语义相似度 | 模糊描述、概念理解 | Phase 2 |
| **知识图谱扩展** | 基于实体关系的关联检索 | 跨知识点、关系推理 | Phase 3 |

> **检索结果排序策略**：Demo 阶段排序简化为匹配行数 + 文件新鲜度。Phase 2 引入多通道后：精确匹配权重 0.5 + 语义相似度 0.3 + 图谱相关性 0.2。

#### 模块五：知识健康巡检

**功能描述**：定期扫描知识库，检测并报告质量问题。

| 检查项 | 说明 |
|------|------|
| **孤立节点检测** | 发现未被任何其他节点引用的知识页面 |
| **语义冲突标记** | 同一事实在不同来源中出现矛盾时自动标记 |
| **断链检测** | 检查wikilink目标文件是否存在 |
| **过期内容标记** | 基于 `updated` 字段，超过180天未更新的知识标记为 `stale` |
| **健康报告输出** | 每周生成知识库健康度报告（孤立节点数/冲突数/断链数/过期数/增长趋势） |

#### 模块六：自增长引擎

**功能描述**：知识库不能只靠人工上传增长。自增长引擎让知识库从使用中自我进化——越用越厚、越用越准。

**核心机制**：

##### 6.1 搜索反馈闭环（Demo）

```
用户搜索 → search_logs 记录 (query, match_count, timestamp)
         → 有结果 → 记录点击（隐性正反馈）
         → 无结果（match_count = 0）→ 记录为"知识缺口"

每周 Claude Code 分析 search_logs →
  → 聚合同类缺口 → 生成"用户想知道但库中没有的 Top 20"
  → 管理员看板展示 → 驱动贡献者上传缺失文档
  → 上传 → 编译 → 入库 → 缺口缩小
```

**Demo 实现**：
- `search_logs` 表：id, query, match_count, timestamp
- Streamlit 自增长看板：搜索未命中 Top 20 表格
- 每周 Claude Code Workflow 自动聚合并去重

##### 6.2 知识演进（Phase 2）

```
新文档编译 → 去重检测发现 similar（70-90%相似）
          → Claude Code 判断：新文档是否补充/更新/修正已有概念？
          → 更新建议 → 审核者决定是否创建新版本（V1.0 → V1.1）
          → 修正建议 → "概念 A 中的 X 数据已过时，新文档显示为 Y"

概念被更新 → 所有引用该概念的条目状态标记为"待核实"
          → 管理员可在自增长看板中查看"受影响的条目列表"
```

##### 6.3 关联涌现（Phase 2-3）

```
Claude Code 定期分析 NEXUS 全量概念：
  → 3+ 个概念共引同一术语但该术语无独立条目 → 建议创建新概念
  → 2 个概念内容高度重叠但标题不同 → 建议合并
  → 概念 A 长时间零引用（孤立节点）→ 建议归档或删除
```

##### 6.4 外部源感知（Phase 3）

```
监控外部数据源（RSS、文档库、Git 仓库 Wiki）
  → 关联文档更新 → 自动触发增量编译
  → 受影响概念标记"可能已过期"
  → 管理员收到通知
```

**自增长四层递进**：

| 层 | 机制 | 阶段 | 一句话 |
|----|------|------|--------|
| **搜索即燃料** | 搜索日志 → 知识缺口发现 | Demo | "用户搜了但没找到的 = 知识库下一个要长的方向" |
| **知识有代谢** | 新文档触发已有概念更新/修正 | Phase 2 | "新知识不会只堆积，会反过来让旧知识进化" |
| **关联自涌现** | 概念共引分析 → 合并/拆分/新建建议 | Phase 2-3 | "知识之间的隐性关联被自动发现和显式化" |
| **外部源感知** | 上游文档变更 → 自动重新编译 | Phase 3 | "知识库不是终点，是持续同步的活系统" |

### 3.2 技术功能需求

| 需求 | 说明 |
|------|------|
| **OKF格式输出** | 所有知识文件符合OKF规范，支持跨平台迁移 |
| **CLI工具** | 提供命令行工具，支持批量导入、导出、编译触发 |
| **API接口** | 提供RESTful API，支持与其他系统集成 |

##### Claude Code 知识库操作方式

Claude Code agent 通过 Bash 工具直接操作 Obsidian Vault（文件系统）和 SQLite 数据库，无需中间协议层：

| 操作 | 实现方式 | 说明 |
|------|---------|------|
| **搜索知识** | `grep -rl "关键词" NEXUS/` | 全文精确匹配，Demo 阶段 |
| **获取条目** | `cat NEXUS/部门/条目名.md` | 读取完整 Markdown 内容 |
| **列出条目** | `sqlite3 meta.db "SELECT * FROM knowledge_entries WHERE status='pending'"` | SQLite 结构化查询 |
| **写入条目** | `cat > NEXUS/概念/新概念.md << 'EOF' ...` | 标准文件写入 |
| **更新状态** | `sqlite3 meta.db "UPDATE knowledge_entries SET status='active' WHERE path='...'"` | SQL 更新 |
| **读取索引** | `cat NEXUS/index.md` | 渐进式目录 |

> **为什么不用 MCP Server**：Demo 阶段目录结构和 SQL schema 稳定、单人维护、Claude Code 原生支持 Bash 工具。MCP Server（FastAPI + MCP SDK）作为一个独立进程增加了部署、调试、维护成本，而它做的事情（封装文件系统+数据库操作）Bash 都能做。Phase 2 引入向量检索和多用户后，再建真正的 REST API 后端。


## 四、技术架构

### 4.1 核心架构

本产品构建在两个基础设施之上：**Obsidian**（知识界面 + 存储）、**Claude Code**（LLM 引擎 + Agent 编排）。

```
┌─────────────────────────────────────────────────────────────┐
│               知识界面层                                     │
│                                                             │
│   Obsidian Desktop（全用户前端）                             │
│   知识浏览 · 图谱可视化 · wikilink 导航 · 全文搜索           │
│   YAML Frontmatter 原生支持 · 反链面板 · Dataview 插件       │
└──────────────────────────┬──────────────────────────────────┘
                           │ 文件系统直接读写（Obsidian 原生行为）
┌──────────────────────────▼──────────────────────────────────┐
│               知识存储层                                     │
│                                                             │
│   Obsidian Vault = 知识库根目录                              │
│   ├── SCHEMA.md   ├── RAW/   ├── pending_review/   ├── NEXUS/ │
│   └── .obsidian/（Obsidian 配置，用户自定义）                 │
│                                                             │
│   元数据：SQLite（knowledge_entries + compile_tasks          │
│           + pending_reviews + search_logs）                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ Bash 工具（grep / cat / sqlite3 / 文件读写）
┌──────────────────────────▼──────────────────────────────────┐
│               引擎层（Claude Code）                          │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  编译 Agent                                          │  │
│   │  Bash 读取 RAW 文件 → 执行 compile prompt           │  │
│   │  → Bash 写入 NEXUS 产物 + SQLite 更新               │  │
│   └─────────────────────────────────────────────────────┘  │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  审核 Agent                                          │  │
│   │  Bash 读取待审条目 → 执行 review prompt              │  │
│   │  → 输出六维度 JSON 判定结果 → SQLite 写入            │  │
│   └─────────────────────────────────────────────────────┘  │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  问答 Agent                                          │  │
│   │  用户提问 → Bash grep/sqlite3 检索                  │  │
│   │  → 执行 answer prompt → 生成带引用答案                │  │
│   └─────────────────────────────────────────────────────┘  │
│                                                             │
│   Harness（Workflow / parallel / pipeline）                  │
│   批量编译 · 六维度并行审核 · 健康巡检 · 自增长分析          │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│               轻量管理层（Streamlit）                        │
│                                                             │
│   文档上传 · 编译触发 · 审核面板 · 自增长看板                │
│   （仅覆盖 Obsidian 不擅长的交互——上传、审批、数据看板）     │
│   （直接读写 Vault 文件系统和 SQLite，不经过中间层）         │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 各层职责

| 层 | 组件 | 职责 | 为什么不是其他方案 |
|------|------|------|------------------|
| **界面层** | Obsidian | 知识浏览、搜索、图谱可视化、wikilink 导航 | 原生支持 Markdown + YAML Frontmatter + wikilink，与 OKF 规范天然对齐。不需要自己写知识浏览器 |
| **存储层** | Obsidian Vault（文件系统） + SQLite | RAW + NEXUS + pending_review 的 Markdown 文件存储 + 结构化元数据 | OKF 规范要求的"文件系统优先"。每个知识条目是一个 .md 文件，git 可追踪、rsync 可同步。SQLite 轻量零配置，适合 Demo 规模 |
| **引擎层** | Claude Code | 执行编译/审核/问答 Agent；Harness 编排批量任务；通过 Bash 工具直接操作 Vault 和 SQLite | 不需要自己封装 LLM API、不需要自己写并发编排、不需要自己维护 Agent 循环。Prompt 文件直接被执行。Bash 工具原生内置，零额外依赖 |
| **管理层** | Streamlit | 文档上传触发、审核管理面板、自增长看板 | 上传和审批是结构化表单操作，不适合在 Obsidian 中完成。Streamlit 是最快的 Python 表单方案。直接读写 Vault 文件和 SQLite |

### 4.3 技术选型

| 层级 | 技术选型 | 角色 | 阶段 |
|------|---------|------|------|
| **知识界面** | Obsidian Desktop | 全用户的知识浏览、搜索、图谱可视化前端 | Demo |
| **LLM 引擎** | Claude Code（Claude 模型） | 编译/审核/答案生成的 Agent 运行时 + Harness 编排 + Bash 工具直接操作文件系统 | Demo |
| **管理层 UI** | Streamlit | 文档上传、审核面板、自增长看板 | Demo |
| **元数据存储** | SQLite | 知识条目索引 + 编译任务 + 审核记录 + 搜索日志 | Demo |
| **知识存储** | 文件系统（Obsidian Vault） | OKF 兼容的 Markdown 文件 | Demo |
| **向量检索** | ChromaDB | 语义检索（条目 > 1000 时替代 grep 全量匹配） | Phase 2 |
| **后端 API** | FastAPI + REST | 真正后端（向量检索、JWT 认证、多用户 API） | Phase 2 |
| **全文检索** | Obsidian 内置搜索 / grep | 精确匹配 | Demo |
| **管理界面升级** | React 19 + Ant Design 5 + Vite 5 | 替代 Streamlit，完整企业级管理后台 | Phase 2 |
| **生产数据库** | PostgreSQL | 替代 SQLite | Phase 2 |
| **知识图谱** | NetworkX / Neo4j | 实体关系图存储与图检索 | Phase 3 |

> **架构原则**：不重造 Obsidian 擅长的轮子（知识浏览、图谱、wikilink）；不重造 Claude Code 擅长的轮子（LLM 调用、Agent 编排、并发、Bash 文件操作）；只做两者都不做的事（上传审批表单、自增长看板）。Demo 阶段目录结构和 SQL schema 稳定清晰，Bash 工具（grep/cat/sqlite3）完全覆盖所有数据操作需求——没必要为"封装文件系统"而引入一个独立进程。

### 4.4 核心数据流

#### 编译流程

```
用户拖拽文件到 Streamlit 上传页
  → Streamlit 存文件到 RAW/
  → Streamlit 触发 Claude Code 编译 Workflow
  → Claude Code agent 通过 Bash (cat) 读取 RAW 文件
  → Claude Code 执行 compile prompt
  → Claude Code agent 通过 Bash (cat > file) 写入 NEXUS/资源/ + NEXUS/概念/
  → Claude Code agent 通过 Bash (>> index.md) 更新 index.md
  → Claude Code agent 通过 Bash (sqlite3) 写入 knowledge_entries + compile_tasks 记录
```

#### 审核流程

```
Claude Code agent 通过 Bash (sqlite3) 查询 status='pending' 的待审列表
  → Claude Code agent 通过 Bash (cat) 逐条读取
  → Claude Code 执行 review prompt（六维度并行：Harness parallel）
  → 输出 JSON 判定结果 → Bash (sqlite3) 写入 pending_reviews 表
  → 审核者在 Streamlit 面板查看 AI 判定 + Markdown 预览
  → 审核者点击 [通过] 或 [驳回]
  → Streamlit 直接更新 SQLite status 字段
```

#### 检索+问答流程

```
用户在 Obsidian 中按 Ctrl+Shift+F 输入查询
  → Obsidian 原生搜索返回匹配的文件列表（grep 级别）
  → 如需语义理解或生成答案，用户选中查询文本，触发 Claude Code 问答 workflow
  → Claude Code agent 通过 Bash (grep -rl + sqlite3) 检索相关知识条目
  → Claude Code 执行 answer prompt
  → 答案以 Markdown 格式呈现在 Obsidian 中（可保存为新笔记）
```

#### 自增长流程

```
每次搜索 → Streamlit 写入 search_logs 记录（query, match_count, timestamp）
  → 每周末 Claude Code 自增长 Workflow 自动触发
    ├── Bash (sqlite3) 分析 search_logs → 生成"知识缺口 Top 20"
    ├── Bash (grep) 扫描 NEXUS wikilink → 检测断链和孤立节点
    ├── Bash (sqlite3) 检查 updated 字段 → 标记 stale 内容
    └── 输出健康报告 + 增长建议 → 显示在 Streamlit 看板
  → 管理员根据报告 → 上传缺失文档 → 触发编译 → 缺口缩小
```


## 五、数据模型

### 5.0 数据模型哲学：文件优先，非数据库优先

传统 Web 应用的数据模型是数据库驱动（Database → API → UI）。但在 Claude Code + Obsidian 架构下，**Markdown 文件（含 YAML Frontmatter）是规范数据源（canonical），SQLite 是查询缓存和过程数据存储**。

```
┌─────────────────────────────────────────────────────────────┐
│  规范数据层（Canonical）                                     │
│                                                             │
│  Obsidian Vault 中的 .md 文件                               │
│  ├── YAML Frontmatter：type, title, status, department,     │
│  │                      tags, version, fingerprint, ...     │
│  └── Markdown 正文：知识内容                                 │
│                                                             │
│  谁写：Claude Code（编译/审核后写文件）、Streamlit（状态变更）│
│  谁读：Obsidian（用户浏览）、Claude Code（grep/cat）          │
│  谁索引：SQLite（缓存 YAML 字段，加速列表查询）               │
└──────────────────────────┬──────────────────────────────────┘
                           │ 不一致时：YAML 文件为准
┌──────────────────────────▼──────────────────────────────────┐
│  查询缓存层（SQLite）                                        │
│                                                             │
│  knowledge_entries 表 ← YAML Frontmatter 的物化视图          │
│  compile_tasks 表     ← 编译过程状态（文件无法表达）          │
│  pending_reviews 表   ← 审核过程数据（AI 判定、人工决定）     │
│  search_logs 表       ← 用户行为数据                         │
│                                                             │
│  谁写：Streamlit、Claude Code（sqlite3 命令）                │
│  谁读：Streamlit（快速列表/过滤/统计）                       │
│  维护：Streamlit 提供"重新扫描"按钮，从 YAML 文件重建缓存     │
└─────────────────────────────────────────────────────────────┘
```

**一致性规则**：
- **写路径**：任何状态变更必须同时写 YAML 文件和 SQLite。Claude Code agent 用 `sed` 更新 YAML + `sqlite3` 更新 SQLite。Streamlit 用 Python 同时写文件系统和 SQLite。
- **读路径**：列表/过滤走 SQLite（快）；内容展示走文件系统（Obsidian 原生或 Claude Code `cat`）。
- **修复路径**：Streamlit 提供"从文件重建索引"按钮——扫描全部 NEXUS/*.md，解析 YAML，覆盖 knowledge_entries 表。
- **冲突仲裁**：YAML 文件永远为准。SQLite 是缓存，可随时从文件重建。

### 5.1 核心目录结构（符合OKF规范）

```
知识库根目录/
├── SCHEMA.md                    # 知识结构规范（详见5.2）
├── meta.db                      # SQLite 数据库（查询缓存 + 过程数据）
├── RAW/                         # 原始素材层（只读，不可变）
│   ├── 个人_notes/              # 个人工作笔记
│   ├── 会议/                    # 会议纪要
│   ├── 经验/                    # 经验总结
│   └── 项目/                    # 项目文档
├── pending_review/              # 待审核区
│   └── （Demo：扁平存储，不按部门分类）
│   ── Phase 2 扩展为按职务部门分类 ──
│   ├── 销售/                    # Phase 2
│   ├── 售前/                    # Phase 2
│   ├── 产品/                    # Phase 2
│   ├── 实施交付/                # Phase 2
│   ├── 开发/                    # Phase 2
│   ├── 财务/                    # Phase 2
│   ├── 人事/                    # Phase 2
│   ├── 行政/                    # Phase 2
│   ├── 共享层/                  # Phase 2
│   ├── 申报/                    # Phase 2
│   └── 待人工/                  # Phase 2
└── NEXUS/                       # 加工知识层（OKF兼容）
    ├── index.md                 # 全局索引（Reserved File）
    ├── log.md                   # 审计日志（Reserved File）
    ├── 资源/                    # 源文件摘要（1:1映射RAW文件）
    ├── 概念/                    # 概念百科页（1份RAW可提取0~N个概念）
    └── 研究/                    # 深度研究简报（跨文档综合）
```

> **meta.db 与 Vault 的关系**：`meta.db` 放在 Vault 根目录，Docker 挂载时与 Markdown 文件一起被 Streamlit 容器访问。Obsidian 用户可以在 `.obsidian/config` 中配置忽略 `.db` 文件，不影响知识浏览体验。

**RAW 分类与 pending_review 职务分类的映射**：

| RAW 来源分类 | 默认映射部门 | 说明 |
|-------------|------------|------|
| 个人_notes | 由审核Agent根据内容判定 | 个人笔记无固定部门归属 |
| 会议 | 由参会人员和议题判定 | 跨部门会议 → 共享层 |
| 经验 | 由内容领域判定 | 技术经验 → 开发/产品；管理经验 → 共享层 |
| 项目 | 由项目归属部门判定 | 售前项目 → 售前；交付项目 → 实施交付 |

> 注：RAW 分类是**来源类型维度**（素材形式），pending_review 分类是**知识归属维度**（组织架构），两者不是一一对应关系。审核 Agent 的职务归属判定负责架设这个映射。

### 5.2 SCHEMA.md 大纲

`SCHEMA.md` 是整个知识库的元结构规范文件，定义合法的 type、tags、字段和命名约定。它同时是 **Claude Code agent 的编译/审核约束规范**——Prompt 中的 type/tags/department 枚举值均以此文件为准。

内容大纲如下：

```markdown
# 知识库 Schema

## 1. 合法 Type 列表
- concept      # 概念百科页（知识的基本单元）
- resource     # 资源摘要（RAW 文件的编译摘要）
- research     # 研究简报（跨文档综合分析）
- glossary     # 术语表条目

## 2. 合法 Status 列表
- draft        # 草稿（仅个人可见）
- pending      # 待审核
- active       # 已发布（企业可见）
- stale        # 过期（超过180天未更新，待审查）
- deprecated   # 已废弃

## 3. 合法 Tags 命名空间
- 部门: 销售/售前/产品/实施交付/开发/财务/人事/行政/共享层
- 领域: AI/大数据/云计算/安全/项目管理/产品设计/...
- 类型: 实战经验/技术方案/产品文档/会议纪要/...

## 4. Frontmatter 字段规范
## 5. 文件名与 Wikilink 约定
## 6. 版本号规则
```

> SCHEMA.md 在项目初始化时由 `init.sh` 自动生成默认版本。管理员可通过 Streamlit 管理后台修改，修改后 Claude Code agent 下次执行时自动读取新规范。

### 5.3 OKF兼容的知识文件格式

```markdown
---
type: concept                              # 必填
title: "示例监测产品"                            # 推荐
description: "示例企业自主研发的多灾种监测预警产品"
tags: ["产品", "应急管理", "监测预警"]
department: "产品"                          # 职务归属部门
created: "2026-08-10"
updated: "2026-08-10"
source: "RAW/产品资料/示例监测产品产品白皮书.md"
version: "V1.0"
status: "active"
contributors: ["何豫东", "张三"]             # 贡献者列表（支持聚合）
fingerprint: "a1b2c3d4..."                  # 内容指纹（SHA256）
---

# 示例监测产品

## 定义
示例监测产品是示例企业自主研发的...

## 关联知识
- [[概念-叫应体系]]
- [[概念-多灾种预警]]

## 引用来源
- 来源：产品白皮书 V2.1，2026年7月
```

> **字段说明**：除 `type`（OKF 唯一必填）外，`title`、`status`、`department`、`source` 为本产品强制必填字段——编译 Agent 注入，缺失时审核 Agent 直接判 `incomplete`。

### 5.4 关键数据表

#### 表设计原则

1. **规范数据（YAML）与过程数据（SQLite）分离**：知识属性（title/department/tags/status）存在 YAML 中，SQLite 只缓存查询需要的字段。编译过程、审核判定、搜索日志等"过程性"数据存 SQLite。
2. **路径即主键**：知识条目的唯一标识是其在 Vault 中的相对路径（如 `NEXUS/概念/示例监测产品.md`），不使用自增 ID。wikilink 天然引用路径，消除 ID→路径 的转换层。
3. **SQLite 只是缓存**：`knowledge_entries` 表的任何记录都可以通过扫描 YAML 文件重建。Demo 阶段容忍缓存不一致——Streamlit 提供"重建索引"按钮。

#### Demo 阶段（4 张表）

| 表名 | 存储类型 | 说明 | 关键字段 |
|------|---------|------|---------|
| **knowledge_entries** | 查询缓存 | YAML Frontmatter 的物化视图，加速列表/过滤查询 | `path` (PK, TEXT), `type`, `title`, `department`, `status`, `version`, `fingerprint`, `updated_at` |
| **compile_tasks** | 过程数据 | 编译任务状态追踪 | `id` (PK, INTEGER), `raw_path`, `nexus_path`, `fingerprint`, `status`(pending/processing/done/failed/cached), `error_msg`, `started_at`, `completed_at` |
| **pending_reviews** | 过程数据 | 审核记录 + AI 判定结果 | `id` (PK, INTEGER), `nexus_path` (FK→entries.path), `submitter`, `department`, `ai_verdict`, `ai_scores`(JSON), `human_decision`, `created_at` |
| **search_logs** | 行为数据 | 搜索日志（自增长燃料） | `id` (PK, INTEGER), `query`, `match_count`, `timestamp` |

> **为什么 knowledge_entries 用 path 做主键而不是自增 ID**：Obsidian wikilink `[[概念-示例监测产品]]` 直接对应文件路径 `NEXUS/概念/示例监测产品.md`。用 ID 做主键意味着每次 wikilink 解析都要 `SELECT path FROM entries WHERE id=?`——纯浪费。路径即身份。

> **为什么 compile_tasks 和 pending_reviews 保留自增 ID**：同一个文件可以被多次编译（重试、文档更新），同一份知识可以被多次提交审核（驳回后重新提交），路径不能唯一标识一次编译任务或一次审核记录。

#### Phase 2 新增（4 张表）

| 表名 | 存储类型 | 说明 | 关键字段 |
|------|---------|------|---------|
| **audit_logs** | 审计数据 | 操作审计日志 | `id`, `operator`, `action`, `target_path`, `detail`(JSON), `timestamp` |
| **contributors** | 关系数据 | 贡献者记录（多对多） | `entry_path`, `user_id`, `contribution_type`(submit/review/approve) |
| **conflicts** | 过程数据 | 知识冲突记录 | `id`, `entry_a_path`, `entry_b_path`, `conflict_type`(factual_contradiction/duplicate/stale), `status`, `created_at` |
| **health_reports** | 聚合数据 | 健康巡检报告 | `id`, `report_date`, `orphan_count`, `conflict_count`, `broken_link_count`, `stale_count`, `total_entries`, `growth_rate` |

#### 缓存一致性维护

```
场景                          处理方式
────                          ────────
Claude Code 编译完成          编译 agent 写 YAML 文件 + sqlite3 INSERT/UPDATE knowledge_entries
Streamlit 人工审核通过        Streamlit 改 YAML(status) + UPDATE knowledge_entries + INSERT pending_reviews
Claude Code grep 直接读文件   不经过 SQLite，无一致性问题
缓存怀疑不一致                Streamlit "重建索引"按钮：扫描 NEXUS/**/*.md → 解析 YAML → REPLACE INTO knowledge_entries
```


## 六、非功能需求

| 需求 | 说明 |
|------|------|
| **私有化部署** | Demo 阶段使用 DeepSeek API（云端），数据经 API 传输但不在服务端持久化；生产环境（Phase 3）规划本地模型方案实现完全离线运行 |
| **增量编译性能** | 单次增量编译时间 < 30秒（RAW 100份文档以内，单文档 < 5万字符） |
| **检索响应** | 检索 + 答案生成总响应时间 < 5秒（Phase 1 grep检索）；Phase 2 引入向量检索后目标 < 3秒（知识条目 < 10,000条规模） |
| **并发支持** | 支持10人同时上传/检索（MVP阶段），Phase 3 目标 100 并发 |
| **知识条目规模** | MVP 目标 1,000 条，Phase 2 支持 10,000 条，Phase 3 支持 100,000+ |
| **单条目大小** | 单个知识 Markdown 文件 < 50KB（约 5000 中文词） |
| **可观测性** | 编译引擎：编译时长/成功率/失败原因分布；审核流转：各状态条目数/平均审核耗时/驳回率；检索：QPS/平均延迟/P99延迟 |
| **可迁移性** | 知识库支持完整导出（tar.gz + metadata.json）/导入，跨平台迁移零依赖 |
| **安全** | Demo：单用户模式无权限控制；敏感信息检测（审核 Agent 中做）；Phase 2：JWT 认证 + 管理员/普通用户区分；Phase 3：操作审计日志 + 完整 RBAC |


## 七、迭代路线图

### Phase 1：MVP Demo（2-4周）—— 验证核心价值闭环

**Demo 目标**：证明"上传文档 → AI 编译 → 审核流转 → 自然语言检索"的核心链路可行且有价值。

| 模块 | 功能 | 优先级 | 备注 |
|------|------|--------|------|
| 知识编译引擎 | RAW→NEXUS 全量编译（SHA256 指纹缓存，不做增量） | P0 | Claude Code agent 通过 Bash 工具执行 |
| OKF 兼容输出 | YAML Frontmatter + Markdown（type 必填注入） | P0 | |
| SCHEMA.md | 默认版本自动生成 | P1 | |
| 审核流转 | 六维度 AI 审核（Claude Code agent 执行）+ Streamlit 人工审核界面 | P0 | 不做终审 Agent（合并为一道） |
| 检索 + 答案生成 | Obsidian 原生搜索（grep）+ Claude Code 答案生成 | P0 | 不做语义检索和知识图谱 |
| 自增长引擎 | search_logs + 搜索未命中 Top 20 看板 | P0 | 搜索反馈闭环 |
| 前端 | Obsidian（知识浏览/搜索/图谱）+ Streamlit 3 页面（上传/审核/自增长看板） | P0 | |
| 数据库 | SQLite + knowledge_entries + compile_tasks + pending_reviews + search_logs | P0 | |
| 部署 | Docker Compose 一键启动 + Obsidian Vault 初始化 | P0 | |
| 种子数据 | 5-10 份示例企业真实文档预置在 RAW 目录 | P1 | |

**Demo 不做**：增量编译、JWT 认证（单用户模式）、审计日志、多源同步、健康巡检、知识演进、权限模型。

> **从 Demo 到 Phase 2**：Demo 验证通过后，Phase 2 建真正的 FastAPI 后端（REST API + 向量检索 + JWT 认证），替代 Bash 直接操作 → SQLite 迁移至 PostgreSQL。

### Phase 2：企业级能力（2-3个月）

| 模块 | 功能 |
|------|------|
| 混合检索 | grep + 向量语义检索（ChromaDB）双通道 + re-rank |
| 多源同步 | 冲突检测、去重、贡献者聚合、版本管理 |
| 知识演进 | 新文档触发已有概念更新/修正建议 + 版本自动递增 |
| 关联涌现 | 概念共引分析 → 合并/拆分/新建建议 |
| 健康巡检 | 孤立节点、冲突、断链检测 + 周报自动生成 |
| 前端升级 | React + Ant Design 管理后台，替代 Streamlit |
| 安全 | JWT 认证 + 管理员/普通用户角色区分 |
| 数据库 | 从 SQLite 迁移至 PostgreSQL |

### Phase 3：生产级（3-4个月）

| 模块 | 功能 |
|------|------|
| 知识图谱 | 实体识别（LLM提取）+ 关系抽取 + 图谱存储（NetworkX / Neo4j）+ 图检索集成到混合检索 + Obsidian Graph View 增强 |
| 外部源感知 | 上游文档变更监控 → 自动触发增量编译 |
| 权限模型 | 多租户隔离、部门级权限、完整 RBAC |
| 分布式编译 | 并行编译、断点续跑（分布式）、编译队列 |
| 审计闭环 | 完整的审计日志和版本回溯（diff 对比） |
| 生产部署 | Docker + K8s 部署方案、监控告警 |
| 性能优化 | 100 并发、10万+ 知识条目、P99 < 3秒 |


## 八、开发规范

### 8.1 开发范式总览

本项目**只采用两种开发范式**——选择标准是一个简单问题："这个模块的输出是否可精确断言？"

| 问题 | 答案 | 范式 |
|------|------|------|
| 输出是文件/JSON/数据库记录，可精确验证字段和结构？ | 是 | **SDD** |
| 输出是规则判定结果（对/错/分数），可穷举边界条件？ | 是 | **TDD** |
| 输出是 LLM 自由生成的自然语言，只能"好/坏"不能"对/错"？ | 是 | **不引入形式化范式，用手工走查 + Demo 脚本验证** |

```
         SDD                            TDD
          │                              │
    知识编译引擎                   审核流转（六维度判定规则）
    检索 API（输入→输出 schema）    多源同步与去重（纯算法）
    OKF 格式输出（字段校验）        健康巡检（检查逻辑）
```

### 8.2 SDD（规约驱动开发）—— 编译引擎 & API

**适用条件**：输入输出可形式化、管道式处理、产物可静态检查字段和结构。

**模块一「知识编译引擎」**：编译管道的每一步写成规约，给定 RAW 输入，断言 NEXUS 产物的文件结构、YAML 字段、正文内容。

```
规约示例（伪代码）：

  given:  RAW文件 F，SHA256 = H
  when:   执行 ingest(F)
  then:   NEXUS/资源/{分类}/F.name 文件存在
          YAML Frontmatter 包含 type, title, source, fingerprint=H
          正文包含 ## 摘要 和 ## 关键信息 两个章节
          knowledge_entries 表中新增一条记录

  given:  RAW文件 F，SHA256 未变化（二次编译）
  when:   再次执行 ingest(F)
  then:   命中缓存，不触发 LLM 调用
          compile_tasks 表中状态为 'cached'

  given:  RAW文件 F，正文包含 3 个独立知识概念
  when:   执行 ingest(F)
  then:   生成 1 个资源摘要 + 3 个概念页
          概念页之间通过 wikilink 互链
```

**检索 API**：输入（query string）→ 输出（结果列表 + 引用来源）的 schema 可形式化。需断言的是返回结构和引用完整性，不是自然语言答案的措辞。

### 8.3 TDD（测试驱动开发）—— 审核判定 & 多源同步 & 健康巡检

**适用条件**：规则可穷举、边界条件多、输出可精确判定对错。

**模块二「审核流转」的六维度判定**：每个维度独立成测试组，每个维度 5-10 个用例覆盖核心路径 + 边界 + 异常。敏感信息检测（正则匹配身份证/手机号/API Key）和完整性检查（字段存在性）是纯逻辑规则，不依赖 LLM，可直接断言。

```
[完整性维度]
  ✅ 包含 title + type + source + 正文≥100字 → pass
  ✅ 缺少 type 字段 → incomplete
  ✅ 正文仅 20 字 → insufficient

[去重维度]
  ✅ 标题+正文前200字完全相同 → duplicate
  ✅ 标题相同但正文内容不同 → similar (相似度>70%)
  ✅ 完全不同的主题 → pass

[敏感信息维度]
  ✅ 包含身份证号（18位数字格式） → blocked
  ✅ 包含手机号（11位数字格式） → warning
  ✅ 包含可识别的 API Key（sk-xxx 等模式） → blocked
  ✅ 无任何敏感内容 → pass

[质量评估 & 职务归属 & 合规检查]
  ⚠️ 这三个维度依赖 LLM 语义理解，输出非确定性
  → 不强制 TDD，改为 prompt 退化检测（见 8.5 Harness 第 4 条）
```

**TDD 在 LLM 项目中的特殊价值**：确定性规则（完整性/去重/敏感信息检测）用 TDD 保证每次改 prompt 不引入回归；非确定性维度（质量/归属/合规）用 prompt 退化检测保证。

**模块三「多源同步」纯算法逻辑，无 LLM 参与**：

```
✅ SHA256("title" + "body前200字") 相同 → 去重，合并 contributors
✅ 版本号 V1.0 → 正文微调 → V1.1 (minor)
✅ 版本号 V1.0 → 核心定义改写 → V2.0 (major)
✅ 两个条目同主题但数据矛盾 → 生成冲突记录
✅ 三个用户提交同一内容 → contributors 数组去重后长度为 3
```

**模块五「健康巡检」检查逻辑部分**：

```
✅ 概念A未被任何其他概念引用 → 标记为孤立节点
✅ wikilink [[概念-X]] 但 X.md 不存在 → 断链
✅ updated 字段距今 > 180天 → 标记 stale
✅ 已被标记 stale 的条目被编辑后 → 自动恢复为 active
```

### 8.4 明确不引入的范式及理由

| 不引入 | 理由 | 替代方案 |
|--------|------|---------|
| **BDD**（行为驱动开发） | LLM 生成的自然语言答案措辞非确定——同样的搜索两次返回表述不同但都对。Gherkin 的 then 断言无法精确表达"答案质量好"；弱断言（"答案包含关键词"）几乎不失败，也发现不了退化。此外 BDD 框架（Behave/pytest-bdd）的维护成本在 Demo 阶段不划算 | 9.4 节的手工 Demo 演示脚本覆盖核心用户路径。检索 API 的输出结构用 SDD 规约保证 |
| **战术 DDD**（聚合根/实体/值对象/仓储/领域事件） | 项目核心逻辑在 LLM prompt 中，不在 Python 代码中。Python 代码做的是读文件、调 API、存结果、搜全文——CRUD + Pipeline，不是复杂领域模型。Demo 阶段一个人写代码，战术 DDD 只会增加包装代码而不加速交付 | FastAPI 直接操作 SQLAlchemy ORM + 文件系统。当 Phase 3 审核流转演进为"规则引擎 + prompt 增强"混合模式、且团队 > 3 人时，再评估引入战术 DDD |

### 8.5 Harness 使用时机

#### 什么是 Harness

Harness 指 Claude Code 的多 Agent 编排能力（Workflow / parallel / pipeline），可以并行派发子 Agent、对抗性验证结果、fan-out 搜索。

#### 判断框架

判断一个任务是否需要 harness，看三个条件：

| 条件 | 满足 → harness 有价值 | 不满足 → 手写代码 |
|------|----------------------|---------------------|
| **LLM 在运行时参与？** | 审核判定、概念提取、答案生成、语义冲突检测 | 文件哈希、版本号递增、grep 检索、CRUD |
| **有多个独立子任务可并行？** | 批量编译、六维度审核、五项健康检查 | 单文件上传、单条记录写入 |
| **需要"验证方"独立视角？** | 代码审查、端到端验证、prompt 退化检测 | 新建 API endpoint、加一个表单字段 |

#### ✅ 本项目推荐使用 Harness 的场景

**1. 批量知识编译**

```
场景：一次导入 50 份文档
做法：pipeline 模式
  - 主流程扫描 RAW 目录，识别待编译文件列表
  - pipeline(items, stage1: LLM编译, stage2: 去重检测)
  - 50 个文件独立流入管道，失败的自动标记，不阻塞成功的
优势：总耗时 = 最慢单个文件的全流程时间（而非 50×单文件时间）
```

**2. 审核 Agent 六维度并行判定**

```
场景：一条知识进入 pending_review
做法：parallel 6 个子 Agent，每维度一个
  - 敏感信息维度 blocked → 一票否决，立即返回
  - 其他维度继续运行，最终汇总
优势：敏感检测和完整性检查同时进行，快者先出
```

**3. 知识健康巡检**

```
场景：每周定时健康扫描
做法：parallel 5 个子 Agent
  - 孤立节点 / 语义冲突 / 断链 / 过期内容 / 增长趋势 五项并行
  - 汇总为一份健康报告
优势：五项检查完全独立，总耗时 = 最慢的那个
```

**4. 审核 Prompt 调优的退化检测**

```
场景：审核 Agent 误判率高，需要调 prompt
做法：
  - 准备 20 条已知正确判定结果的测试样本
  - 用新旧 prompt 各审一次
  - 对比结果，标记判定变化的 case
优势：系统性发现 prompt 变更的副作用，而非凭感觉判断"好像变好了"
```

**5. 端到端验证**

```
场景：编译引擎实现完成后验证
做法：不走单元测试，驱动真实流程
  - 上传一份真实测试文档
  - 检查 NEXUS 产物是否产生
  - 用 Obsidian 搜索刚入库的知识
  - 确认返回结果中包含正确的引用来源
优势：验证的是用户真正使用的路径，而非 mock 出的假环境
```

**6. 代码审查（/code-review）**

```
场景：每个模块阶段性完成或 PR 提交前
做法：
  - 多维度审查（正确性 / 安全性 / 性能 / 简化重构）
  - 对抗性验证每个 finding（尝试 refute）
  - 只保留经得起挑战的发现
优势：独立于开发者的视角，防止"自己写的代码自己审"的盲区
```

#### ❌ 不需要 Harness 的场景

| 任务 | 原因 | 替代做法 |
|------|------|---------|
| 文件 SHA256 指纹计算 | 纯函数，一行代码 | `hashlib.sha256()` |
| YAML Frontmatter 解析/生成 | 标准库操作 | `yaml.safe_load()` / `yaml.dump()` |
| grep 全文检索 | 一个命令 | `subprocess.run` 或 SQLite FTS5 |
| REST API CRUD | FastAPI 标准模式 | `@app.get("/entries/{id}")` |
| 数据库迁移 | Alembic 流程 | `alembic upgrade head` |
| 前端表单/列表 | Ant Design 组件 | `<Table>`, `<Form>` |
| 版本号递增 | 字符串拼接 | `f"V{major}.{minor}"` |
| JWT 认证中间件 | 安全框架标准用法 | `python-jose` + FastAPI Dependency |
| 配置文件读写 | 标准 I/O | `json.load()` / `yaml.safe_load()` |

### 8.6 开发流程约定

```
┌─────────────────────────────────────────────────────────┐
│  每个功能模块的标准开发流程                               │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. 写规约/测试（先于实现代码）                            │
│     ├── SDD 模块：编写规约文档（输入→输出断言）            │
│     └── TDD 模块：编写测试用例（先红灯）                   │
│                                                         │
│  2. 实现代码（最小可用实现）                              │
│     └── 目标：刚好让规约通过 / 测试变绿                    │
│                                                         │
│  3. Harness 验证（LLM 参与或需要独立视角时启动）           │
│     ├── /code-review 多维度审查                          │
│     ├── /verify 端到端驱动真实流程                        │
│     └── prompt 变更时跑退化检测                           │
│                                                         │
│  4. 重构（规约/测试保护下优化代码结构）                    │
│                                                         │
│  5. 提交（规约/测试全部通过，harness 验证无回归）          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

| 模块 | 步骤 1 的产出物 | 步骤 3 的验证方式 |
|------|---------------|-----------------|
| 知识编译引擎 | SDD 规约文档（若干 given-when-then） | /verify 上传真实文档 → 检查产物 → Obsidian 搜索验证 |
| 审核流转 | 完整性/去重/敏感信息检测的测试用例 | /code-review + prompt 退化检测 |
| 多源同步 | 去重/版本/冲突各 5+ 用例 | /code-review |
| 检索+答案生成 | 检索 API 输出 schema 规约 + Demo 演示脚本（9.4节） | /verify 真实搜索 → 走查答案质量 |
| 健康巡检 | 孤立节点/断链/过期检测各 5+ 用例 | /code-review |
| 前端 | Demo 演示脚本（9.4节）中的页面操作步骤 | /verify 打开浏览器走完整用户流程 |


## 九、Demo 实施指南

### 9.1 Docker 部署方案

Demo 使用 Docker Compose 一键启动，目标：新人克隆代码后，一条命令即可体验完整流程。

#### 目录结构

```
llm-wiki-demo/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example              # Claude API 配置（无需额外 LLM Key）
├── init.sh                   # 初始化 Vault + 建表 + 生成 SCHEMA.md
├── streamlit_app/            # Streamlit 轻量管理层（直接操作 Vault 和 SQLite）
│   ├── upload.py             # 上传页
│   ├── review.py             # 审核面板
│   ├── growth.py             # 自增长看板（搜索未命中 Top 20）
│   └── db.py                 # SQLite 操作封装
├── vault/                    # Obsidian Vault = 知识库根目录
│   ├── .obsidian/            # Obsidian 配置（用户自定义）
│   ├── SCHEMA.md
│   ├── RAW/                  # 种子数据预置在此
│   ├── NEXUS/                # 编译产物（运行时生成）
│   │   ├── index.md
│   │   ├── log.md
│   │   ├── 资源/
│   │   ├── 概念/
│   │   └── 研究/
│   └── pending_review/       # 待审核区（运行时生成）
├── prompts/                  # Claude Code Prompt 文件
│   ├── compile_prompt.md
│   ├── review_prompt.md
│   └── answer_prompt.md
├── workflows/                # Claude Code Workflow 脚本
│   ├── compile_workflow.md   # 批量编译编排
│   ├── review_workflow.md    # 六维度并行审核编排
│   └── growth_workflow.md    # 周度自增长分析编排
└── tests/                    # 测试用例
    └── sample_docs/          # 测试用示例文档
```

#### docker-compose.yml 关键配置

```yaml
version: '3.8'
services:
  streamlit:
    build: ./streamlit_app
    ports:
      - "8501:8501"             # Streamlit 管理界面
    volumes:
      - ./vault:/app/vault      # Obsidian Vault（文件系统直接读写，含 meta.db 与 _triggers/）
    environment:
      - KB_ROOT=/app/vault
      - DB_PATH=/app/vault/meta.db
```

> **Claude Code 不需要在 Docker 中运行**——它运行在开发者的终端中，通过 Bash 工具直接操作 `vault/` 中的 Markdown 文件和 `meta.db` SQLite 数据库。没有中间进程——Claude Code 的 agent 直接用 `grep`/`cat`/`sqlite3`/文件重定向完成所有数据操作。Prompt 文件和 Workflow 脚本在本地文件系统中，直接由 Claude Code 读取执行。

#### 启动步骤

```bash
# 1. 初始化 Obsidian Vault + SQLite 数据库
./init.sh

# 2. 将 vault/ 目录在 Obsidian 中打开为 Vault
#    Obsidian → Open folder as vault → 选择 ./vault

# 3. 启动 Streamlit
docker compose up -d

# 4. 访问
# Streamlit 管理界面: http://localhost:8501
# Obsidian 知识界面: 用户本地的 Obsidian 应用
# Claude Code: 终端运行，直接通过 Bash 操作知识库
```

### 9.2 种子数据需求

Demo 需要 5-10 份示例企业真实文档作为种子数据。这些文档被预置在 `data/RAW/` 目录下，启动时已被系统发现并处于"待编译"状态。

#### 文档要求

| 要求 | 说明 |
|------|------|
| **数量** | 5-10 份 |
| **主题** | 涵盖 2-3 个核心产品/业务线（如示例监测产品、叫应体系等） |
| **领域覆盖** | 至少涉及 2-3 个部门（如产品 + 售前 + 开发） |
| **格式** | 以 .md 为主（PDF/DOCX 需额外提供文本提取后的版本用于对照） |
| **关联性** | 至少 2 对文档包含可互链的知识点（如两份文档都提到"示例监测产品"，可从不同角度互相引用） |
| **长度** | 每份 500-5000 字 |

#### 建议的文档清单

| # | 模拟文件名 | 类型 | 部门 | 包含的知识概念 |
|---|-----------|------|------|-------------|
| 1 | 示例监测产品产品白皮书.md | 产品文档 | 产品 | 示例监测产品、多灾种监测预警、监测预警平台 |
| 2 | 叫应体系技术方案.md | 技术方案 | 售前 | 叫应体系、应急响应流程、多级联动 |
| 3 | 示例监测产品部署手册.md | 实施交付 | 实施交付 | 部署架构、数据接入规范、运维监控 |
| 4 | 智慧城市项目复盘.md | 复盘总结 | 实施交付 | 项目管理经验、风险应对、验收标准 |
| 5 | 应急管理系统竞品分析.md | 行业研究 | 产品 | 竞品格局、差异化策略、市场定位 |
| 6-10 | （根据实际可用文档补充） | | | |

#### 种子数据的 Demo 效果

- 用户第一次打开 Demo，看到 5-10 份文档处于"待编译"状态
- 一键触发全量编译 → 生成 ~15-25 个概念页（每份文档提取 2-3 个概念）
- 搜索"示例监测产品"→ 返回 3+ 个相关知识条目 + 自然语言答案
- 审核页面有 1-2 条待审核条目，可演示通过/驳回操作

### 9.3 错误 UX 设计

Demo 必须优雅处理错误。每个可能出错的环节都设计用户可见的反馈。

#### 错误状态矩阵

| 环节 | 错误场景 | 用户看到什么 | 用户能做什么 |
|------|---------|------------|------------|
| **文件上传** | 格式不支持（如 .jpg） | 🔴 "不支持的文件格式：.jpg。支持的格式：.md, .txt, .pdf, .docx" | 重新选择文件 |
| **文件上传** | 文件过大（>10MB） | 🔴 "文件大小超过限制（10MB）。当前文件大小：12.3MB" | 压缩或拆分后重新上传 |
| **文本提取** | PDF 无法解析（加密/扫描件） | ⚠️ "PDF 文本提取失败：文件可能为扫描件或加密文档。建议上传可编辑的 PDF 或转换为 Markdown 格式" | 转换为文本后重新上传 |
| **编译** | LLM API 超时 | ⚠️ "编译超时（已等待 60 秒）。文档可能过长或 API 暂时拥堵。" + [重试] 按钮 | 等待后重试，或拆分文档 |
| **编译** | LLM 返回非 JSON | ⚠️ "编译引擎返回了非预期格式。系统已自动重试 1 次，仍失败。" + [查看原始返回] + [重试] | 查看原始返回（调试用），或重试 |
| **编译** | 编译结果异常（概念数 = 0 但文档有内容） | ℹ️ "编译完成，但未识别出独立知识概念。文档可能为过渡性内容。" | 手动查看编译结果，或标记为需人工处理 |
| **审核** | LLM API 超时 / 错误 | ⚠️ "AI 审核暂时不可用。" + [重试 AI 审核] + [跳过 AI，直接人工审核] | 重试或人工判定 |
| **审核** | 审核结果解析失败 | ⚠️ "AI 审核结果解析失败。已自动重试，请查看下方原始输出。" + [重试] | 跳过 AI，人工判定 |
| **搜索** | LLM API 超时 | ℹ️ 显示 grep 检索结果列表（不含自然语言答案） + "AI 答案生成暂时不可用，以下为关键词匹配结果。" + [重试生成答案] | 直接阅读检索到的条目 |
| **搜索** | 无匹配结果 | ℹ️ "知识库中暂无与'xxx'直接相关的信息。" + 建议关键词 + [提交知识] 按钮 | 尝试不同关键词，或上传新文档 |
| **全局** | DeepSeek API Key 未配置 | 🔴 "未检测到 DEEPSEEK_API_KEY。请在 .env 文件中配置后重启服务。" | 配置环境变量后重启 |

#### 错误展示 UI 约定

- 🔴 红色：阻塞性错误，用户必须处理才能继续
- ⚠️ 橙色：可恢复错误，有重试或替代方案
- ℹ️ 蓝色：信息提示，不影响后续操作

所有错误信息包含三个要素：
1. **问题描述**（发生了什么）
2. **可能原因**（为什么会发生）
3. **建议操作**（用户现在该做什么）

### 9.4 Demo 演示脚本

> **Demo 角色说明**：Demo 为单用户模式，无登录。演示者通过 Streamlit 侧边栏的"视角切换"下拉框（贡献者 / 审核者 / 消费者）手动切换角色，体验不同角色的操作界面。所有角色共享同一个数据视图，无需登入登出。

#### 脚本 A：核心闭环演示（5分钟）

```
[00:00] 打开 Obsidian，左侧文件面板展示 Vault 目录结构
        可见 RAW/（5-10份种子文档）、NEXUS/（编译产物）、pending_review/

[00:30] 切换到 Streamlit (http://localhost:8501) → "上传文档"标签页
        上传 sample_示例监测产品产品白皮书.md
        列表中出现新条目，点击 [触发编译]

[01:00] 编译由 Claude Code agent 在后台执行（约 10-20 秒）
        Streamlit 状态变为 ✅ "待审核"

[01:30] 切换到"审核管理"标签页
        看到待审核列表，点击"示例监测产品"条目
        左侧：AI 六维度评分面板；右侧：Markdown 预览
        审核者：质量 4 分，无敏感信息，归属"产品"部门
        点击 [✓ 通过审核]

[02:30] 切换回 Obsidian
        NEXUS/概念/ 下出现 示例监测产品.md、多灾种监测预警.md 等新概念页
        打开示例监测产品.md → 渲染完整的 Markdown 知识页面
        Obsidian Graph View → 看到概念之间的 wikilink 连接图

[03:30] Obsidian 中按 Ctrl+O 搜索 "叫应体系"
        跨概念关联生效——搜索结果展示相关页面
        反链面板显示哪些页面引用了当前概念

[04:30] 回到 Streamlit → "自增长看板"标签页
        搜索未命中记录：之前有人搜过"区块链技术方案"→ 0 结果
        Top 20 知识缺口列表 → 驱动后续贡献方向
```

> Obsidian 负责：知识浏览、图谱可视化、wikilink 导航、原生搜索。Streamlit 仅负责：上传、审核、看板。

#### 脚本 B：审核驳回演示（2分钟）

```
[00:00] 审核者登录，看到一条待审条目
        六维度评分显示：质量 2 分，有格式问题

[00:30] 审核者查看详情
        发现问题：正文只有 60 字，缺少来源声明

[01:00] 审核者点击 [✗ 驳回]
        填写驳回原因："正文不足 100 字，请补充详细说明和引用来源"
        提交

[01:30] 提交者（或管理员）登录
        看到状态为"已驳回"
        查看驳回原因，修改后重新提交
```


## 十、附录 A：LLM Prompt 模板

> 以下三套 Prompt 是系统的核心引擎。Prompt 以独立文件维护在 `prompts/` 目录，支持热更新——修改 Prompt 文件后无需重启服务即可生效。

### 10.1 知识编译 Prompt

**文件路径**：`prompts/compile_prompt.md`

**功能**：将 RAW 原始文档编译为 OKF 兼容的结构化知识产物（资源摘要 + 概念页）。

**核心输出约束**：

| 约束项 | 要求 |
|--------|------|
| 输出格式 | 严格 JSON（含 resource + concepts 两个顶层字段） |
| 资源摘要 | 每个文档必产出一个 resource，含 title/summary/tags/department/source_type |
| 概念提取 | 0-N 个概念，每个含 title/description/content/related_to |
| 部门归属 | 根据内容语义判定，9 个部门 + 共享层 |
| 内容 < 50 字 | concepts 为空数组，resource.summary 标注"内容不足" |
| 表格 | 转 Markdown 表格，>10 行截断并注明 |
| 图片标记 | 摘要中注明"（原文包含图片，请查看原始文档）" |
| 信息保真 | 不编造原文不存在的事实 |

> 完整 Prompt 见 `prompts/compile_prompt.md`

### 10.2 知识审核 Prompt

**文件路径**：`prompts/review_prompt.md`

**功能**：对待审核知识条目进行六维度评估，输出判定结果。

**六维度速查**：

| 维度 | 判定结果 | 权重 |
|------|---------|------|
| 完整性 | pass / incomplete / insufficient | 必过 |
| 去重 | pass / duplicate / similar | 必过 |
| 职务归属 | 9 个部门之一 | — |
| 质量 | 1-5 分（逻辑 30% + 信息 30% + 格式 20% + 表达 20%） | ≥3 |
| 敏感信息 | pass / warning / blocked | blocked = 一票否决 |
| 合规 | pass / flagged | pass |

**判定逻辑链**：

```
sensitive = blocked → rejected（一票否决）
completeness = insufficient → rejected
quality ≤ 2 / compliance = flagged / sensitive = warning / concerns ≥ 3 → needs_human_review
dedup = duplicate → rejected
dedup = similar → approved（加标注）
其他 → approved
```

> 完整 Prompt（含每维度详细判定标准、JSON Schema、判定逻辑伪代码）见 `prompts/review_prompt.md`

### 10.3 答案生成 Prompt

**文件路径**：`prompts/answer_prompt.md`

**功能**：基于检索到的知识条目，生成带引用来源的自然语言回答。

**回答策略**：

| 场景 | 策略 |
|------|------|
| 精确事实查询 | 概念定义 + 补充背景 |
| 对比/列举查询 | 表格对比 + 总结 |
| 操作指南查询 | 编号步骤 + 注意事项 |
| 无匹配结果 | 告知无结果 + 建议关键词 + 邀请提交 |
| 部分匹配 | 列出相关条目 + 建议细化关键词 |
| 信息矛盾 | 标注矛盾 + 呈现两方说法 + 建议核实 |

**核心原则**：严格基于检索结果、引用可追溯、诚实告知边界、简洁优先。

> 完整 Prompt（含六种回答策略模板、格式规范、质量要求、行为边界）见 `prompts/answer_prompt.md`


## 十一、附录 B：参考资源

| 资源 | 说明 |
|------|------|
| **Google OKF规范** | 开放知识格式v0.1，知识文件标准化规范 |
| **Karpathy LLM Wiki Gist** | 原始LLM Wiki理念 |
| **Arkon** | 企业级MCP Server参考实现 |
| **腾讯WeKnora** | 开源LLM知识平台参考 |
| **企业级LLM Wiki工程化标准** | 四大主流改造方案 |


**文档状态**：Demo 就绪稿（v1.7）
**作者**：何豫东
**更新**：2026年8月11日

**主要变更（v1.7）**——架构简化，移除 MCP Server：

🔧 架构精简：
- 第四章技术架构：六层 → 五层，移除桥接层（MCP Server）
- Claude Code agent 改为通过 Bash 工具（grep/cat/sqlite3/文件重定向）直接操作 Obsidian Vault 和 SQLite
- 原因：Demo 阶段目录结构和 SQL schema 稳定，Bash 完全覆盖所有数据操作需求，MCP Server 作为独立进程增加部署维护成本但未增加实质能力
- 架构原则更新：不重造 Obsidian 的轮子，不重造 Claude Code 的轮子，只做两者都不做的事

📋 连带调整：
- 3.2 节：MCP Server Tools 表替换为 Claude Code Bash 操作方式表
- 4.3 技术选型：桥接层行删除，LLM 引擎行扩展角色说明（含 Bash 工具），Phase 2 新增 FastAPI 后端行
- 5.0 新增「数据模型哲学」：文件优先，非数据库优先——YAML Frontmatter 为规范数据源，SQLite 为查询缓存
- 5.4 数据表重设计：knowledge_entries 主键从自增 ID 改为 path (TEXT)；pending_reviews 外键从 entry_id 改为 nexus_path；新增"缓存一致性维护"设计；Demo 表从 4 张保持 4 张但结构变化
- 5.1 目录结构新增加 `meta.db`（SQLite 放在 Vault 根目录）
- 第七章 Demo 范围：MCP Server 行删除，P0 数从 11 减至 10
- 第八章 SDD 标题和示例：移除 MCP Server，8.6 开发检查清单移除 MCP Server 行
- 第九章目录结构：删除 mcp_server/，docker-compose 从双容器减为单容器，启动步骤从 5 步减为 4 步
- 头部层数修正：五层 → 四层（元数据层并入存储层，与第四章架构图一致）
- 3.1 编译触发方式：明确为触发文件信号机制（_triggers/ + SessionStart hook + /process-triggers），移除失效的 `python -m ingest --all` CLI 残留
- 9.1 docker-compose：统一 DB_PATH=/app/vault/meta.db，移除单独 meta.db 挂载（与 5.1 目录结构一致）

**历史变更（v1.6）**——架构重写 + 自增长：

🏗️ 底层架构切换：
- 第四章技术架构彻底重写：Claude Code 替代 DeepSeek API 作为 LLM 引擎和 Agent 编排层
- Obsidian 替代 Streamlit/React 作为全用户知识界面（浏览、搜索、图谱、wikilink）
- MCP Server 从 Phase 2 升格为 Demo P0——Claude Code 访问 Obsidian Vault 的唯一通道
- Streamlit 从"后端前端"退化为"轻量管理层"——仅做上传触发、审核面板、自增长看板
- 新增架构图含六层：界面层(Obsidian) → 存储层(Vault) → 桥接层(MCP Server) → 引擎层(Claude Code) → 管理层(Streamlit) → 元数据(SQLite)
- 新增核心数据流四张图：编译/审核/检索问答/自增长

🔄 自增长引擎：
- 新增模块六「自增长引擎」：搜索反馈闭环(Demo) + 知识演进(Phase 2) + 关联涌现(Phase 2-3) + 外部源感知(Phase 3)
- 新增 search_logs 表 + Streamlit 自增长看板（搜索未命中 Top 20）
- 自增长四层递进模型

📋 连带调整：
- 第二章用户角色表改为"主要工具"列（Obsidian/Streamlit 分角色说明）
- 第七章 Demo 范围：MCP Server 升 P0、加自增长引擎、Streamlit 4页→3页
- Phase 2：加知识演进 + 关联涌现 + JWT 认证
- Phase 3：加外部源感知 + Obsidian Graph View 增强
- 第九章目录结构重写：vault/ + mcp_server/ + streamlit_app/ + workflows/
- 演示脚本重写：体现 Obsidian 操作 + 自增长看板

**历史变更（v1.5）**：三轮自审修复 14 处内部矛盾与精度缺陷
**历史变更（v1.4）**：移除 BDD + 战术 DDD；范式收敛为 SDD + TDD
**历史变更（v1.3）**：新增 Demo 实施指南 + 三套 Prompt 模板
**历史变更（v1.2）**：新增开发规范章节
**历史变更（v1.1）**：新增 RAG 范式澄清、编译规约、审核标准、答案生成链路等

**历史变更（v1.4）**：移除 BDD + 战术 DDD；范式收敛为 SDD + TDD
**历史变更（v1.3）**：新增 Demo 实施指南 + 三套 Prompt 模板；Phase 1 重定义为 Demo 范围
**历史变更（v1.2）**：新增开发规范章节
**历史变更（v1.1）**：新增 RAG 范式澄清、编译规约、审核标准、答案生成链路等
