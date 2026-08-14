# 周度自增长分析 Workflow

**触发**：用户手动执行 /process-growth（或并入 /process-triggers 时作为尾段）。

## 步骤
1. 导出近 7 天未命中搜索：
   `sqlite3 vault/meta.db "SELECT query, COUNT(*) cnt FROM search_logs WHERE timestamp >= date('now','-7 days') AND match_count=0 GROUP BY query ORDER BY cnt DESC LIMIT 100"`
2. LLM 聚类语义相同的查询（如「示例监测产品价格」≈「哨兵报价」）
3. 对每个聚类给出建议补充文档方向（产品资料/技术方案/…）
4. 生成 `NEXUS/研究/自增长周报_<YYYY-MM-DD>.md`：
   - 本周知识缺口 Top 20（排名/缺口主题/搜索次数/建议补充文档）
   - 上周已补缺口（对比上周周报与本周新入库条目）
5. 更新 knowledge_entries（周报本身 type=research, status=active）
6. 返回：周报路径与缺口数

## 输出验收
- NEXUS/研究/ 出现当日周报；看板「最近自增长周报」卡片可渲染
