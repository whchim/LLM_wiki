# 竞品对比：腾讯 WeKnora

> 状态：结论已定（不迁移、不替换，选择性借鉴）
> 版本：v0.1
> 日期：2026-09-11
> 适用：技术选型自证、面试叙事"为什么不用现成 RAG 平台"、Phase 3 借鉴清单
> **证据强度声明**：本文对 WeKnora 的描述来自公开搜索引擎返回的官方仓库/文档标题与来源摘要，**未经逐条正文核对**（编写环境网络受限，无法直接抓取网页）。所有 WeKnora 事实按第八节"验证清单"自查后方可作为对外引用依据。

## 1. 一句话结论

**两者不是同一赛道竞品，工程重叠约 30%。** WeKnora 是"把原始文档变成可查可问的知识底座"的**通用平台**；本项目是"在明确业务边界内产出可审计事实"的**领域状态机**。重叠仅在"文档理解 + 检索"一层，而在**权威存储、审核治理、状态语义、可归责性**四处双方取向相反或不重叠。

因此不做技术替换，只做**选择性借鉴**（第六节）。

## 2. 各自定位

| | 腾讯 WeKnora | 本项目（LLM Wiki → 销售客户状态 Agent） |
|---|---|---|
| 官方定位 | Open-source LLM knowledge platform：把原始文档变成 queryable RAG + autonomous reasoning agent + self-maintaining Wiki | 销售洽谈记录 → 证据提取 → 状态建议 → 负责人确认 → 可审计状态事件 |
| 形态 | 可部署的通用平台（多租户、多模型、多接入） | 明确边界内的单域流程原型（第一版只处理中文文字洽谈记录） |
| 技术栈 | 后端 Go（Gin 主干）+ 前端 Vue3；Go 占比约 61% | Python：FastAPI + Streamlit + Vue3 工作台；PostgreSQL 16 + pgvector |
| 规模信号 | ~17.9K Star、MIT、社区活跃、容器化部署档位 | 7,260 行 Python、133 个 pytest、单人/小团队、文档驱动 |
| 许可 | MIT（待核） | 见仓库 LICENSE |

## 3. 逐维度对比

| 维度 | WeKnora | 本项目 |
|---|---|---|
| **核心范式** | 运行时 RAG：每次查询检索 → 拼上下文 → 生成 | **编译时**：入库即编译为结构化 Markdown；运行时直接读编译产物 |
| **文档理解** | 深度解析为重点投入方向：多格式、版面/OCR/多模态 | **只吃中文纯文本**（`meeting_note`/`transcript`/`chat_summary`）；音视频、图片、网页抓取明确列为非目标（SA-01 §2.2） |
| **检索** | 多检索引擎可插拔 + 混合检索 + rerank，多阶段 pipeline | grep + pgvector 双通道加权融合（0.5/0.3）；黄金集 14 条 + MRR@10/Recall@10 门禁；embedding 故障降级 grep-only |
| **知识治理** | LLM **自动生成、自动维护** Wiki | **人工门禁**：六维度审核（完整性/敏感信息=确定性规则，质量/合规/去重/职务归属=LLM）→ 人工放行才入 `NEXUS/概念/` |
| **权威存储** | 平台数据库/检索引擎为准 | **vault 的 YAML Frontmatter 是规范源**；PG 为可重建缓存；双写不一致时以文件为准 |
| **溯源与可回放** | 回答标注来源 chunk（引用溯源） | 证据不可覆盖 + `StateEvent` 事件史 + 幂等键 + 过期/撤回/更正 + synthetic 回放评测 |
| **领域建模** | 无（通用，不假设业务语义） | 7 状态有限状态机；每状态最低证据要求与默认有效期；`won` 需强证据；证据不足必须输出 `needs_review` |
| **敏感数据** | 租户/空间级隔离与权限 | **字段级假名化**：正文占位符 `[AMOUNT_REF:nv-001]` + 独立受限数值表 `sensitive_numeric_values` + 审计下恢复精确值 |
| **Agent 执行** | 内置 ReACT Agent + 工具调用 / MCP 集成 | **外部引擎**：Claude Code headless（`claude -p`）+ `_triggers/` 触发文件队列 + Harness 编排 |
| **安全/接口** | 多租户、RBAC、模型管理、IM 集成、生产级部署档位 | JWT(HS256)+三角色 RBAC + `audit_logs` 审计 + REST API；单租户 |
| **工程范式** | 平台/框架工程（广度、生态、可插拔） | SDD + TDD + 文档驱动裁决链（PRD → 设计 → 实施 + Changelog） |

## 4. 三个根本差异（不是功能多少，是取向）

1. **编译时 vs 运行时**
   WeKnora 在查询时理解文档，检索物是不可读的向量 chunk；本项目在入库时把理解固化为人类可读、可 diff、可 wikilink 的 Markdown。这是本项目与所有 RAG 项目最本质的分野，也是 PRD 第 20 行的范式声明。

2. **人工门禁 vs 自动维护**
   WeKnora 的核心卖点之一是"自动维护、自我增长"；本项目把"AI 建议 ≠ 事实，必须负责人确认"写进业务契约（SA-01 §3）——Agent 只能建议，`StateEvent` 才是事实唯一写入口。在需要对账/归责的企业场景，**"更贵的人工门禁"是特性而非缺陷**：它换来可归责性。

3. **广度平台 vs 深度域**
   WeKnora 追求覆盖更多格式、更多模型、更多接入；本项目主动冻结边界（第一版只处理一种文字记录、状态集合限制为 7 个）。两者验收标准不同，**不能用 Star 数或支持格式数量互相评判**。

## 5. 双方各自不可替代之处

### 5.1 WeKnora 明显强于本项目（差距是真实的）

| 能力 | 差距性质 |
|---|---|
| 文档解析工程（OCR、版面/表格理解、多模态、多格式） | 本项目明确不做，非"无所谓" |
| 检索成熟度（多路召回 + rerank + 可插拔引擎） | 本项目只有两路融合，尚属自研 |
| Agent 运行时自带工具调用/MCP | 本项目绑死外部 CLI（`claude -p` + headless 权限模式），交付到客户环境是硬约束 |
| 产品化程度（多租户、模型管理、IM 集成、部署档位） | 本项目为单租户原型 |
| 社区与维护生态 | 单人项目无法对冲 |

### 5.2 本项目 WeKnora 不打算做（也不该由它做）

| 能力 | 说明 |
|---|---|
| 可审计事实链 | 谁确认的、依据哪段原文、何时过期、如何撤回——状态机 + 事件溯源能力，非知识库能力 |
| 质量门禁 | LLM 输出契约校验（三组 schema）+ Prompt 退化检测 + 人工复核兜底，把"模型可能胡说"变成可拦截工程环节 |
| 可回放评测 | 合成回放 36 条 + 检索黄金集 + τ=0.52 阈值标定，有量化门禁而非感觉 |
| 程序/模型分工边界 | 确定性部分（正则、金额阈值、权限、状态校验）交程序，模糊部分交模型，边界写进文档并可断言 |

## 6. 借鉴清单（按性价比排序）

| 优先级 | 事项 | 落点 | 成本/收益 |
|---|---|---|---|
| P1 | 混合检索补一路关键词/BM25 加权 + rerank 阶段 | `api/routers/search_router.py`；权重用 `tools/tune_search.py` 重标定 | 低 / 直接提升召回，且已有黄金集验证 |
| P2 | 解析层预留"音频→文本"适配接口 | 现有 `source_type=transcript` 已是输入来源之一 | 低 / 不自己写转写，接现成组件 |
| P3 | 实体/关系抽取（Phase 3 知识图谱） | PRD Phase 3 规划项 | 中 / 可参照 WeKnora Wiki 模式的抽取设计 |
| P4 | LLM 引擎抽象成接口，解除 `claude -p` 硬绑定 | 触发 watcher 与 LLM 调用层 | 中 / 为本地模型或 MCP 工具调用留口，降低交付绑定 |

## 7. 决策记录：迁移 / 集成 / 独立

**决策：独立（方案 A），不做整体替换。**

**理由**：
1. 两者权威存储模型不兼容——本项目 YAML Frontmatter 为规范源、PG 为缓存；引入外部平台会产生"谁是写入权威"的二义性。
2. 审核模型不兼容——自动维护 vs 人工门禁，两套并存将导致审计链断裂。
3. 状态语义缺失——WeKnora 不含客户状态机与人工确认链，替换等于丢掉项目最有价值的差异点。
4. 与既有技术取向一致——PRD 对向量能力明确"用 pgvector 扩展，不引入独立向量库"（第 411 行），范式选择上也只采用 SDD + TDD、明确不引入额外重范式（§8.4）。本项目一贯取向是**用标准库/框架的普通用法支撑自研核心**，而非引入一个承担全部知识流程的外部平台。

**备选方案（记录备查，不采纳）**：
- **方案 B（可组合）**：把 WeKnora 当作上游"文档理解/检索服务"，本项目只做"证据 → 状态 → 确认 → 事件"这一段。边界清晰、可组合，但需额外定义跨系统契约与降级策略，当前阶段无必要。
- **方案 C（转向）**：若目标改为做通用企业知识平台，则 WeKnora/RAGFlow 直接构成对手，需另写 PRD，现有销售 Agent 降为上层应用。**当前不做此转向。**

## 8. WeKnora 验证清单（对外引用前必须自查）

本文第八节以下内容均标注证据强度，**引用前请逐条核对官方正文**：

- [ ] 当前版本号、Star 数、最近提交时间（题述"~17.9K Star"来自第三方拆解文章，非实时值）
- [ ] 依赖组件与可插拔检索引擎的完整清单（是否含 Neo4j 类图数据库）
- [ ] 支持的文档格式与解析链路细节（OCR/多模态的具体能力边界）
- [ ] ReACT Agent 与 MCP 集成的具体形态（是否可编排 workflow）
- [ ] "self-maintaining Wiki" 的自动化程度——是否也需人工确认环节
- [ ] License 细节与商用条款
- [ ] 建议直接查阅官方 `website-docs/01-getting-started/01-introduction.md` 与 `website-docs/02-architecture/` 两个目录

**已由来源标题/摘要直接支持的事实**：官方一句话定位（RAG + agent + self-maintaining Wiki）、Go 为主的技术栈、对话/文档解析/检索引擎等功能文档分节、Wiki Mode 与 Auto-Wiki Generation 存在、ReACT Agent 与 v0.2.0 发布、~17.9K Star 与 Go 61.3% 占比、多租户/部署档位文档、MIT（第三方称）。

## 9. 来源

- [Tencent/WeKnora · GitHub](https://github.com/Tencent/WeKnora)
- [WeKnora 文档：概述（website-docs/01-getting-started/01-introduction.md）](https://github.com/Tencent/WeKnora/blob/main/website-docs/01-getting-started/01-introduction.md)
- [WeKnora 文档：文档解析](https://github.com/Tencent/WeKnora/blob/main/website-docs/03-features/03-document-parsing.md)
- [WeKnora 文档：检索引擎](https://github.com/Tencent/WeKnora/blob/main/website-docs/03-features/05-retrieval-engines.md)
- [WeKnora 文档：架构总览](https://github.com/Tencent/WeKnora/blob/main/website-docs/02-architecture/01-overview.md)
- [DeepWiki：Hybrid Retrieval Strategies](https://deepwiki.com/Tencent/WeKnora/11.6-hybrid-retrieval-strategies)
- [DeepWiki：Wiki Mode and Auto-Wiki Generation](https://deepwiki.com/Tencent/WeKnora/11.7-wiki-mode-and-auto-wiki-generation)
- [DeepWiki：System Architecture](https://deepwiki.com/Tencent/WeKnora/3-system-architecture)
- [腾讯云开发者社区：WeKnora v0.2.0 + ReACT Agent](https://cloud.tencent.com.cn/developer/article/2610798)
- [从源码看 WeKnora 的技术栈](https://zhuwei.fun/blog/weknora-tech-stack-source-analysis-rag-platform/)
- [WeKnora 深度拆解：17.9K Star，Go 61.3%](https://studygolang.com/topics/19168)
- [WeKnora vs RAGFlow 对比（第三方）](https://raw.githubusercontent.com/retrovirusretro/weknora-english-guide/main/docs/vs-ragflow.md)

---

## Changelog

- **v0.1（2026-09-11）**：初稿。基于项目当前形态（README 已收敛为销售客户状态 Agent 生产形态原型；`schema.sql` 含 customers/conversations/evidence/state_proposals/state_decisions/state_events/current_states/sensitive_numeric_values 等表）与 WeKnora 公开资料对比。结论：**不迁移、不替换，选择性借鉴**——①工程重叠约 30%，重叠仅在文档理解与检索层；②三处取向相反（编译时/运行时、人工门禁/自动维护、深度域/广度平台）；③四项借鉴清单（混合检索 BM25+rerank / 音频转写接现成组件 / Phase 3 实体关系抽取 / LLM 引擎解耦）。**已知局限**：编写环境网络受限，WeKnora 事实均来自搜索摘要未经正文核对，已列第八节验证清单；本文档作为技术选型自证与面试叙事依据，对外引用前需完成核对。
