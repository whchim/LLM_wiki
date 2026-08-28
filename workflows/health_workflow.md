# 健康巡检 Workflow（SP5）

**触发**：/health-check 命令，或 watcher 每周一自动执行。

## 步骤

1. **确定性巡检**（不依赖 Agent 判断）：
   ```
   python tools/health_check.py
   ```
   产出：health_reports 落库一行 + `NEXUS/研究/健康周报_<date>.md` +
   相似候选 `vault/_triggers/.similarity_candidates_<date>.json`

2. **相似候选 LLM 复核**（仅当候选 JSON 非空）：
   对每对候选，读两篇概念全文，三选一判定：
   - **同一概念**（重复入库）→ 建议【合并】：产出合并建议文件 `_suggestions/merge_<date>_<n>.md`
     （含两篇全文对照、建议保留的主版本、需合并的信息点）
   - **同主题不同侧面** → 建议【互链】：给出双向 wikilink 补丁（追加到两篇的"关联知识"节）
   - **完全无关**（误报）→ 忽略，在周报标注

3. **孤立概念判定**：对巡检列出的孤立节点，读全文判断：
   - 已被其他概念覆盖 → 建议【归档】（frontmatter status: deprecated 建议纸条）
   - 确有价值但缺关联 → 给出应建立的具体 wikilink 建议

4. **汇总**：所有建议写入 `_suggestions/`（人可读 Markdown），
   周报末尾追加"建议清单"节；**绝不直接修改 NEXUS 条目**——
   一切变更走人工确认 → 审核流 → 版本自增（V1.0→V1.1 微调 / V2.0 改写）

5. **返回**：巡检指标摘要 + 建议数（合并 x / 互链 y / 归档 z）

## 输出验收
- health_reports 新增一行；周报文件渲染于自增长看板
- 建议只存在于 _suggestions/ 与周报中，NEXUS 零直接改动