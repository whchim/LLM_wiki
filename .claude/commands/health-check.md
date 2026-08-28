# /health-check —— 知识库健康巡检（SP5）

执行 workflows/health_workflow.md：

1. 运行 `python tools/health_check.py`（确定性四类检测：孤立/断链/过期/相似）
2. 对相似候选做 LLM 复核（合并/互链/忽略三选一）
3. 对孤立概念给出归档或补链建议
4. 所有建议写入 `vault/_suggestions/`，汇总进周报——**不直接改 NEXUS**
5. 返回指标摘要与建议数

顺带执行 `python tools/archive_done.py`（done/ 超 90 天归档）。
