# /process-triggers —— 处理知识库触发队列

扫描 vault/_triggers/*.md（排除 done/），按文件时间戳升序处理：

1. 对每个 compile_*.md：按 workflows/compile_workflow.md 执行批量编译
2. 对每个 review_*.md：按 workflows/review_workflow.md 执行六维度审核
3. 每个触发文件处理成功（或所有条目已尝试且记录失败原因）后，移入 vault/_triggers/done/
4. 处理报告：编译 N 个、审核 M 个、失败 K 个（附失败文件与原因）

## 断点续跑（SP3）：消费前的任务级去重

对 compile 纸条内的**每个 RAW 路径**，先查 `compile_tasks` 最新状态再决定动作：

| 最新状态 | 动作 |
|---------|------|
| 无记录 | 全新编译 |
| `done` 且指纹相同 | **跳过**（输出"已编译，跳过"），不重复消耗 LLM |
| `done` 但指纹不同 | 重编译（文档已变更） |
| `cached` | 跳过 |
| `failed` / `pending` | 重编译（started_at 重置） |
| `processing` | 距更新超过 30 分钟 → 视为僵尸，重编译；否则跳过（另一会话可能在跑） |

查询（PostgreSQL，用 psql 或 python psycopg）：

```sql
SELECT status, fingerprint, started_at FROM compile_tasks
WHERE raw_path = '<路径>' ORDER BY id DESC LIMIT 1;
```

> 这是"中断后续跑不重复执行已完成工作"的保证：任何原因（崩溃/超时/重启）导致的中断，
> 重新消费时 done/cached 的任务一律跳过，只补未完成的。

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
  --skipped <本次断点跳过文件数（SP3 新增，默认 0）> \
  --failed <本次失败文件数> \
  --files '<本次产出文件路径的 JSON 数组>' \
  --latency-ms "$latency_ms"
```

- **字段来源**：步骤 1 编译过程中已统计的 compiled/cached/failed/skipped 计数与产出文件清单；
  `latency_ms` = 本批次从开始到此刻的耗时。
- **必须在命令末尾执行**（compile 批次结束后、生成处理报告前）。
- 若本机无 Python，跳过并记录到处理报告（不阻断流程）。

## 输出验收

- 每个 compile 批次产生 1 条 `trace_events(span_type='compile_session')`（detail 含 skipped）
- done/cached 任务被跳过且明确输出"已编译，跳过"
- 触发文件全部移入 done/，处理报告明确 compiled/cached/failed/skipped 计数