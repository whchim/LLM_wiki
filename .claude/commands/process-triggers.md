# /process-triggers —— 处理知识库触发队列

扫描 vault/_triggers/*.md（排除 done/），按文件时间戳升序处理：

1. 对每个 compile_*.md：按 workflows/compile_workflow.md 执行批量编译
2. 对每个 review_*.md：按 workflows/review_workflow.md 执行六维度审核
3. 每个触发文件处理成功（或所有条目已尝试且记录失败原因）后，移入 vault/_triggers/done/
4. 处理报告：编译 N 个、审核 M 个、失败 K 个（附失败文件与原因）

## 可观测性（SP2.5）：编译 Trace 兜底采集

**每个编译批次处理完毕后，必须执行以下确定性步骤**（不依赖任何 Agent 记忆）：

```
trace_id="$(python -c \"import uuid; print(uuid.uuid4())\")"
latency_ms=<自本次编译开始起算的毫秒数>
python tools/record_compile_trace.py \
  --trace-id "$trace_id" \
  --operation batch \
  --compiled <本次 LLM 编译成功页数> \
  --cached <本次指纹缓存命中文件数> \
  --failed <本次失败文件数> \
  --files '<本次产出文件路径的 JSON 数组>' \
  --latency-ms "$latency_ms"
```

- **字段来源**：步骤 1 编译过程中已统计的 compiled/cached/failed 计数与产出文件清单；
  `latency_ms` = 本批次从开始到此刻的耗时（可用 date +%s%3N 记录起点与终点差值）。
- **必须在命令末尾执行**（compile 批次结束后、生成处理报告前）——即使用户中途追加触发文件，
  每个 compile 批次各采集一条 compile_session trace。
- 若本机无 Python，跳过并记录到处理报告（与编译期"无 Python 报错"策略一致，但不阻断流程）。

## 输出验收

- 每个 compile 批次产生 1 条 `trace_events(span_type='compile_session')`
- 触发文件全部移入 done/，处理报告明确 compiled/cached/failed 计数