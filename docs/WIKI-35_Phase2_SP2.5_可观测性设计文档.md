# LLM Wiki 知识库平台 SP2.5「可观测性」设计文档 v0.2

> **版本**：v0.2 ｜ **日期**：2026-08-24 ｜ **状态**：草案（待评审）
>
> **定位**：为已交付的 SP1/SP2 补齐可观测性——重点是用户明确要的 **Claude Code 编译过程内部 Trace**，外加一套在 LLM 层验证 token 数据的探针。
>
> **范围**：a) 编译过程 trace 落库（trace_events 表）；b) Streamlit 可观测性页（编译次数/检索成败率/延迟/Top 失败）；c) Langfuse 最小探针（非侵入旁路，验证 token 数据价值）。**v0.2 修正**：上一版把 trace 误埋到 `/uploads`（那只是"触发编译"，不是编译内部）——本版改为观测 **编译过程本身**。

---

## 1. 问题与目标

**问题**：Claude Code 编译是不可观测的黑盒——不知道每次编译消耗多少 token、耗时多久、编译几页、错过缓存几次。排障靠猜。

**目标**：
- 每次编译会话落一条**过程 trace**：span（开始/各文件/结束）、token（如有）、耗时、（结果页数）、成功失败、命中缓存数
- Streamlit 新增可观测性页：当日编译次数、检索成功/失败率、平均响应延迟、Top 失败模式
- Langfuse 最小探针验证"LLM 内部 token 明细是否值得为它重构 Agent 驱动"

## 2. 层与边界（修正版）

**编译过程 trace 落在哪**：不在 FastAPI（FastAPI 不调 LLM、只写触发纸条），而在 **Claude Code 编译会话结束时**。

| 层级 | 观测物 | 采集方式 | 本 SP 做 |
|------|--------|---------|---------|
| **过程级 trace** | 一次编译会话：开始/各文件/结束、耗时、页数、缓存命中、成功失败 | 编译 workflow 末尾调用确定性采集脚本 `tools/record_compile_trace.py`，写 trace_events | ✅ 主交付 |
| **LLM token 探针** | 每次 LLM 调用 token/成本明细 | Langfuse SDK（Claude Code 侧最小接入，旁路） | ✅ 最小验证 |
| 应用级（FastAPI 端点） | 检索/审核端点耗时成败 | trace 依赖（v0.1 内容） | ✅ 保留（检索成败率指标需要） |

> **为什么删除 upload_trigger**：v0.1 的 `/uploads` trace 只反映"触发动作"，不反映编译内部——与用户诉求不符，弃用。

### 2.1 编译过程 trace 的现实形态

`compile_workflow.md` 是 Claude Code 按 Steps 执行的指令。为保证**确定性落 trace**（不依赖 Claude Code 每次自主决定），在 workflow 末尾固定加一步：

```
步骤 6（新增）：调用 python tools/record_compile_trace.py --json "<本次编译结果 JSON>"
```

编译结果 JSON 由 Claude Code 在步骤 1-4 中自行汇总（它知道编译了几页、缓存命中几次、每个文件成败）。采集脚本解析后写入 `trace_events`。

> 诚实说明局限：`token` 字段在过程 trace 里默认拿不到（Claude Code 不主动暴露单次会话 token）——所以 `token_usage` 由 Langfuse 探针补充；过程 trace 记录的是**耗时/数量/成败/缓存命中**这些 Claude Code 能从结果里确定的量。

## 3. Schema 变更（第 9 张表 trace_events）

```sql
CREATE TABLE IF NOT EXISTS trace_events (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    span_type   TEXT NOT NULL,          -- compile_session / search / review_approve / ...
    trace_id    TEXT,                   -- 一次编译会话的 UUID（过程 trace 分组键；检索等单操作可空）
    operation   TEXT,                   -- 细分动作
    status      TEXT NOT NULL,          -- ok / error
    latency_ms  INTEGER,                -- 会话/操作耗时（毫秒）
    detail      JSONB,                  -- 附加：compiled/cached/failed 计数、各文件、错误、search hit
    token_usage JSONB,                  -- Langfuse 探针回填（input/output/成本）；过程 trace 可空
    operator    TEXT,                   -- 触发者（compile_trace 记 system 或触发用户）
    created_at  TEXT NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_span_created ON trace_events (span_type, created_at);
CREATE INDEX IF NOT EXISTS idx_trace_traceid ON trace_events (trace_id);
```

> `trace_id` 让"一次编译会话"的过程 trace 可聚合成一条全链路（n 个文件 + 1 个会话级汇总）；Langfuse trace 的 id 可回填到此字段实现"过程↔LLM 明细"对账。

## 4. 采集实现

### 4.1 过程 trace：`tools/record_compile_trace.py`
确定性 CLI，被 `compile_workflow.md` 步骤 6 调用：
```
用法：python tools/record_compile_trace.py --trace-id <uuid> --operation "batch"
      --compiled 5 --cached 3 --failed 0 --files '["NEXUS/资源/a.md",...]' [--latency-ms 12000]
```
- 复用 `core/db.py` 的连接池写 `trace_events`
- 无 Python 环境时优雅降级（process-triggers 里已是"本机无 Python 时报错并标记 failed"，本次同策略）
- 参数由 Claude Code 编译会话填充（workflow 明确规定字段来源）

### 4.2 应用级 trace：FastAPI `api/trace.py` 依赖（沿用 v0.1）
- `span_type = search / review_approve / review_reject / review_resubmit / review_retry_ai / rebuild_index / login`
- 终端点注入 `Depends(trace("search"))`；ok+error 都记录；`detail` 按类型个性化（search 记 query/hit、approve 记目标路径）
- 只读端点（/search/missed、/tasks、/pending 等）不埋点

### 4.3 Langfuse（v0.3 起：应用层真接；知识层仍走自建埋点）
- **应用层（销售 Agent）已接入**：`core/llm_observability.py` 在 `core/model_port.py::OpenAICompatPort.complete()`
  边界上报 generation（模型、input/output token、延迟、成败、错误摘要）；`api/trace.py` 每次请求生成
  `trace_id` 绑定到上下文，**并写进 `trace_events.trace_id`**（此前一直是 NULL），于是 Langfuse 的 trace
  与我们的 trace 记录用同一个 id 对得上。
- **知识层（编译管道）不接**：编译由 watcher 以 headless 唤起 **Claude Code CLI**，LLM 调用在**外部进程**内，
  拿不到内部 span/generation；能拿到的只有编译结束后的汇总（页数/缓存命中/token/耗时）→ 自建
  `tools/record_compile_trace.py` → `trace_events`。要真接需 OTel 从 CLI 侧导出或改为 API 直调——重构而非接线。
- **默认关闭、零侵入**：未配置 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 时**不 import SDK、不发网络请求**；
  密钥齐全才惰性建客户端，SDK 构造/上报异常一律静默（不影响模型调用）。
- **只上报元数据**：默认不含 prompt/completion 正文（正文即便已脱敏也无理由外发第三方 SaaS），
  联调时用 `LANGFUSE_SEND_TEXT=1` 显式开启；`LANGFUSE_FLUSH_EACH=1` 可改为每次立即 flush。
- `tools/langfuse_probe.py` 保留为**手工探针**（命令行验证链路），不再是唯一接入方式。

## 5. Streamlit 可观测性页

新增 `core/obs.py`（第 4 页，只读直查 trace_events，与 growth.py 同模式）：

| 指标 | 口径 |
|------|------|
| 当日编译次数 | `span_type='compile_session' AND created_at>=today` |
| 检索成功/失败率 | `span_type='search'`：SUM(ok)/COUNT、SUM(error)/COUNT |
| 平均响应延迟 | `AVG(latency_ms)`；按 span_type 分组**
| Top 失败模式 | `GROUP BY span_type, detail->>'error'` → 降序 Top 10 |

> 编译"次数"是会话级（一次含 n 文件）；若要"编译文件数"用 detail->>'compiled' 求和。

## 6. 一致性
- 只读直查 PG 与 growth.py 一致；不违反"写操作走 API"
- 不阻塞 SP3/SP4；`span_type='search'` detail 预留 query/hit，SP4 混合检索口径不漂移
- Langfuse 探针**可选依赖**，默认零侵入

## 7. 测试计划
| 文件 | 覆盖 |
|------|------|
| `tests/test_trace.py` | record_compile_trace 写 compile_session（含 compiled/cached/failed 入 detail）；search 埋点 ok+error；失败也落 trace；trace_id 分组 |
| 回归 | 既有 66 绿；`/uploads` 不再写 upload_trigger（验证删除）|

## 8. 风险
| 风险 | 缓解 |
|------|------|
| Claude Code 不按 workflow 调采集脚本 | workflow 步骤强制 + /process-triggers 命令末尾兜底调用（命令文件里也加一行） |
| token 拿不到 | 过程 trace 不依赖 token；token 留给探针；诚实标注 |
| trace 表膨胀 | 低频写入，SP5 归档策略统一 |

## 9. 退出标准
- [ ] 编译会话落 compile_session trace（含 compiled/cached/failed/latency）
- [ ] 检索/审核/login trace 埋点 ok+error 全覆盖
- [ ] Streamlit 可观测性页 4 指标
- [ ] Langfuse 探针工具可运行（需环境变量）+ 文档说明
- [ ] 测试 + 66 回归绿

---

## Changelog
- **v0.1**：误将 trace 埋到 /uploads（触发动作，非编译内部）。弃用。
- **v0.2（2026-08-24）**：修正为观测**编译过程本身**——compile_workflow 末尾确定性采集（record_compile_trace.py）；保留检索/审核应用级 trace 支撑 4 指标；新增 Langfuse 最小探针（可选依赖，验证 token 价值，默认零侵入）；新增 trace_id 支持过程↔LLM 对账。

- **v0.3（2026-09-14）**：**应用层真接 Langfuse**（原状只有手工探针，若写"全链路 Langfuse"经不起追问）。新增 `core/llm_observability.py`：未配置 `LANGFUSE_*` 时**不 import SDK、不发请求**（零侵入），密钥齐全才惰性建客户端，SDK 构造/上报异常一律静默；在 `model_port.OpenAICompatPort.complete()` 边界上报 generation（模型、token、延迟、成败、错误摘要），**默认不含正文**（`LANGFUSE_SEND_TEXT=1` 才外发）。同时把 `api/trace.py` 的 `trace_id` 落地：每次请求生成 uuid 绑定上下文**并写入 `trace_events.trace_id`**（此前恒为 NULL），使 Langfuse trace 与自建 trace 用同一 id 对账。`langfuse>=2.10` 进 requirements（无密钥时完全惰性）。新增 `tests/test_llm_observability.py`（7 例）。**知识层仍为自建埋点**：外部 CLI 进程拿不到内部 span，硬接需重构驱动方式（见 §4.3）。