# /process-triggers —— 处理知识库触发队列

扫描 vault/_triggers/*.md（排除 done/），按文件时间戳升序处理：

1. 对每个 compile_*.md：按 workflows/compile_workflow.md 执行批量编译
2. 对每个 review_*.md：按 workflows/review_workflow.md 执行六维度审核
3. 每个触发文件处理成功（或所有条目已尝试且记录失败原因）后，移入 vault/_triggers/done/
4. 处理报告：编译 N 个、审核 M 个、失败 K 个（附失败文件与原因）
