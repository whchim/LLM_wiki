# LLM Wiki 知识库平台 Phase 2 SP5「知识智能」设计文档

> **版本**：v0.1 ｜ **日期**：2026-08-28 ｜ **状态**：草案（待评审）
>
> **定位**：Phase 2 子项目 SP5 的详细设计。依据 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md) SP5；需求冲突时以 [LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8 为准。
>
> **架构边界（路线图风险表钉死）**：SP5 只用 **YAML + 审核流 + Claude 轨**，不碰 API 层——巡检脚本只读 YAML/PG 缓存，产出的建议**一律走既有 pending_review 审核流**，绝不直改 NEXUS。

---

## 1. 目标与范围

**问题**：知识库会"腐烂"——概念页越来越多后：孤立节点没人连、wikilink 断链没人修、过期知识没人标 stale、相似概念重复入库没人合并。Demo 期 30 条肉眼可管，Phase 2 之后必须自动化。

**范围内（本 SP 交付）**：
- **健康巡检引擎**（确定性 Python，TDD）：孤立节点 / wikilink 断链 / 过期（>180 天）/ 相似候选 → `health_reports` 表 + `NEXUS/研究/健康周报_<date>.md`
- **知识演进 / 关联涌现**（Claude 轨 workflow）：巡检的"相似候选"交 LLM 判定 → 合并/拆分/新建建议 → **写 review 纸条进审核流**；概念更新后引用方标记"待核实"
- **done/ 归档清理**：>90 天归档文件移入 `_triggers/done/archive_<年月>/`（保留审计留痕，不删除）
- health_reports 表 DDL（SP1 预留未建，本 SP 补）

**范围外**：缺口判据重定义（等 SP4 的向量通道）、部门目录分类迁移（涉及全库文件移动，独立小任务放最后）、外部源感知（Phase 3）。

## 2. 技术决策

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | 巡检实现 | **确定性 Python 脚本**（`tools/health_check.py`），非 Agent | 四类检测全部可形式化（PRD 8.1：输出可断言→TDD）；Agent 跑不确定性高、成本高 |
| 2 | 相似检测 | **基线用编辑距离/字符重合度**（difflib SequenceMatcher ≥ 0.85 入候选）；语义级相似留给 Claude 轨二次判定 | 不依赖 embedding（不阻塞 SP4）；LLM 只对候选清单做语义复核，成本可控 |
| 3 | 建议落地方式 | **写 review 纸条**（kind=rename 后续扩展，先复用 review）+ `_suggestions/` 目录 | 沿用"纸条 = 消息队列"架构；建议进审核流，人工放行才生效 |
| 4 | 版本自增 | 人工审核通过"更新建议"后，由 workflow 步骤执行 frontmatter version 自增（V1.0→V1.1 微调 / V2.0 核心改写） | PRD 6.2 定义；不自动自增——必须过审核 |
| 5 | 过期阈值 | 180 天（PRD 6.4 定义），frontmatter `updated` 缺失时用 `created` 兜底 | Demo 期编译 Agent 未写 updated 字段（已知缺口），兜底保证可运行 |
| 6 | 周报触发 | 手动 `/health-check` 命令 + watcher 集成（每周一自动跑一次） | 与既有触发机制一致 |

## 3. 健康巡检引擎（tools/health_check.py）

### 3.1 四类检测（全部确定性）

| 检测 | 算法 | 严重度 |
|------|------|--------|
| **孤立节点** | NEXUS/概念/*.md 的 frontmatter/正文**零 wikilink 入链且零出链** | warning |
| **wikilink 断链** | 正文 `[[概念-X]]` 解析出目标路径，文件系统不存在 → 断链 | error |
| **过期（stale）** | `updated`（缺则 `created`）距今 > 180 天 | warning |
| **相似候选** | 两两概念 title+description 的 SequenceMatcher ≥ 0.85 | info（候选，LLM 复核） |

### 3.2 输出

1. **`health_reports` 表**（SP5 补 DDL）：
```sql
CREATE TABLE IF NOT EXISTS health_reports (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date   TEXT NOT NULL,
    orphan_count      INTEGER NOT NULL DEFAULT 0,
    broken_link_count INTEGER NOT NULL DEFAULT 0,
    stale_count       INTEGER NOT NULL DEFAULT 0,
    conflict_count    INTEGER NOT NULL DEFAULT 0,   -- 相似候选数（对）
    total_entries     INTEGER NOT NULL DEFAULT 0,
    growth_rate       REAL,                          -- 相比上次巡检的增长率
    detail            JSONB                          -- 明细清单（路径列表）
);
```
2. **`NEXUS/研究/健康周报_<YYYY-MM-DD>.md`**：五类指标 + 明细表 + Top 建议（人类可读，Obsidian 可看）
3. **相似候选清单** `vault/_triggers/.similarity_candidates_<date>.json`：供 Claude 轨消费

## 4. 知识演进 / 关联涌现（Claude 轨 workflow）

新增 `workflows/health_workflow.md` + `.claude/commands/health-check.md`：

```
/health-check 执行流：
1. 跑 tools/health_check.py（确定性巡检）→ 周报 + 候选 JSON
2. 若存在相似候选：Claude 逐对读两篇概念全文 → 判定：
   - 同一概念（重复）→ 建议【合并】（写合并建议纸条）
   - 同主题不同侧面 → 建议【互链】（给出 wikilink 补丁）
   - 完全无关 → 忽略（误报）
3. 零引用孤立概念 → 建议【归档】或【补链接】（LLM 判断该概念是否已被其他概念覆盖）
4. 所有建议统一写入 _suggestions/（Markdown，人可读）+ 汇总进周报
5. 人工在 Obsidian/Streamlit 确认后，走既有审核流落地（版本自增在此步执行）
```

**"待核实"标记**：概念 X 被更新并审核通过后，workflow 扫描全文引用 `[[概念-X]]` 的条目，
在它们 frontmatter 追加 `verify_needed: true`——下次巡检列出"待核实"清单。

## 5. done/ 归档清理

`tools/archive_done.py`（确定性）：`_triggers/done/` 中 mtime > 90 天的文件移入
`_triggers/done/archive_<YYYY-MM>/`。审计留痕保留（不删除），目录不无限膨胀。watcher 启动时顺带执行。

## 6. 文件变更

```
├── schema.sql                    # 🔄 补 health_reports DDL（SP5 按需建，现补上）
├── tools/health_check.py         # 新增：四类确定性巡检（核心，~200 行）
├── tools/archive_done.py         # 新增：done/ 归档清理
├── workflows/health_workflow.md  # 新增：Claude 轨（相似复核/孤立判定/建议生成）
├── .claude/commands/health-check.md        # 新增：手动入口
├── streamlit_app/growth.py       # 🔄 看板加"最近健康周报"卡片（与周报同模式）
├── tests/test_health_check.py    # 新增：四类检测 + 周报落库 + 幂等
└── docs/LLM_wiki_Phase2_SP5_设计文档.md    # 本文档
```

## 7. 测试计划

| 用例 | 断言 |
|------|------|
| 孤立节点检测 | 零入零出链的概念被标记；有链不标 |
| 断链检测 | 指向不存在文件的 wikilink 被抓出；存在的放过 |
| 过期检测 | updated 180 天前 → stale；updated 缺失用 created 兜底；新文件不标 |
| 相似候选 | "示例监测产品" vs "示例监测产品-2" 高相似入候选；无关概念不入 |
| 周报落库 | health_reports 一行，五指标正确；二次运行幂等（新行，growth_rate 计算） |
| 归档清理 | >90 天文件移动，新文件不动，无删除 |

## 8. 退出标准（对齐路线图）
- [ ] 周报自动生成含 5 类指标（孤立/断链/过期/相似/总量+增长率）
- [ ] 版本自增链路走通（更新建议审核通过 → version 自增）
- [ ] 合并/新建建议进入审核流（纸条 + _suggestions/）
- [ ] done/ 不再无限膨胀（90 天归档）

---

## Changelog
- **v0.1（2026-08-28）**：初稿。决策：巡检走确定性脚本（TDD）+ 相似检测用字符基线（不阻塞 SP4）+ LLM 只做候选复核与建议生成（Claude 轨）；建议一律进审核流不直改；缺口判据重定义明确等 SP4；部门目录迁移移出本 SP 范围（独立小任务）。