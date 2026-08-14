# /process-growth —— 生成周度自增长周报

按 workflows/growth_workflow.md 的触发节执行：

1. 导出近 7 天未命中搜索（search_logs 中 match_count=0，按次数倒序取 Top 100）
2. LLM 聚类语义相同的查询
3. 对每个聚类给出建议补充文档方向（产品资料/技术方案/…）
4. 生成 `NEXUS/研究/自增长周报_<YYYY-MM-DD>.md`（本周知识缺口 Top 20 + 上周已补缺口对比）
5. 更新 knowledge_entries（周报本身 type=research, status=active）
6. 处理完向用户汇报：周报路径与缺口数
