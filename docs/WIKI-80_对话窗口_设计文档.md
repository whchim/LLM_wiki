# 对话窗口（应用内问答）设计文档 v0.1

> 定位：知识层的**消费端**。编译产物只有被用起来才算闭环——`/ask` 让"编译好的结构化知识"
> 以**带引用溯源**的问答形式被使用，并且**答不出来时把缺口回流给知识生产侧**。
> 分层口径以 `docs/ARCH-00_项目定位与分层.md` 为准；引擎侧（编译/审核）见 `docs/WIKI-70`。

## 1. 问题：Demo 期的问答在应用外

Demo 与 Phase 2 的问答入口一直是 **Claude Code 里的 `/ask` 命令**（`.claude/commands/ask.md`
+ `prompts/answer_prompt.md`），人机交互发生在终端。这带来三个问题：

1. **云端不可用**：多租户部署后没有宿主机的 Claude Code 与登录态，终端命令这条路走不通；
2. **不可观测/不可计量**：问答不进 `trace_events`、不记 token、不受租户配额约束；
3. **不可信**：模型答案没有机器可校验的引用契约——"看起来有来源"和"来源真的是这条"是两件事。

## 2. 目标架构

```
Vue 工作台「对话窗口」（frontend/src/pages/AskPage.vue）
        │  POST /ask  {question, top_k}
        ▼
api/routers/ask_router.py            只做 HTTP 壳：鉴权 / trace / 响应整形 / 409
        ▼
core/answer_service.py               应用内问答引擎（逻辑都在这里）
        ├── core/retrieval.py        检索原语（grep + pgvector 融合、按租户分区、读正文）
        ├── prompts/answer_prompt.md 系统提示词（含 JSON 输出契约）
        ├── core/output_schema.validate_answer_output   契约校验 + 引用溯源
        └── core/model_port.for_tenant(purpose="answer") 租户模型/密钥/配额
        ▼
trace_events(span_type='ask')  +  llm_usage  +  search_logs(source='ask')
```

**双入口同一契约**：CLI 路径（`.claude/commands/ask.md`）保留为本地开发路径，
两个入口共用 `prompts/answer_prompt.md`；prompt 里给出两种输出格式（JSON / Markdown），
应用内走 JSON（可校验），CLI 走 Markdown（人读）。

## 3. 五条纪律（都有代码与测试锁定）

| # | 纪律 | 落点 |
|---|------|------|
| 1 | **门禁先于模型** | 检索零命中 → **一次模型都不调**，直接给"知识库暂无"+建议，`status=no_hits` |
| 2 | **引用必须可溯源** | 路径必须在本次检索结果里；`quote` 必须在条目正文中**逐字出现** |
| 3 | **违例回灌重试** | 把契约违例原因回灌给模型重试 1 次（盲重试几乎必然再错） |
| 4 | **不静默降级** | 两次违例 → `status=failed` + `contract_ok=false`，**不返回答案**；模型未配置 → API 409 |
| 5 | **缺口回流** | 没答出来（`no_hits`/`failed`/`insufficient`）→ `search_logs.match_count=0`，进自增长看板 |

纪律 1 的价值不只是省钱：**对话窗口最大的可信度风险是模型用预训练知识冒充企业知识**。
没有依据就不答，是这套系统能被企业接受的前提。

纪律 2 是确定性的（路径集合 + 子串匹配），所以必须由**代码**判——模型自报"我有引用"不算数
（与 `docs/VAL-01`、审核侧"规则/模型分工"同一条原则）。

## 4. 输出契约

契约写在 `prompts/answer_prompt.md` §输出契约，代码化在
`core/output_schema.validate_answer_output(d, retrieved_paths, bodies)`：

```json
{
  "answer": "Markdown 正文（不在这里写引用区块）",
  "citations": [{"path": "NEXUS/概念/示例概念.md", "quote": "正文中逐字出现的原文片段", "note": "支撑的结论"}],
  "insufficient": false,
  "followups": ["后续追问（0-2 条）"]
}
```

校验项：形状（`answer` 非空、`insufficient` 布尔、`followups` 字符串数组）+
溯源（路径 ∈ 检索结果、`quote` ⊂ 正文）+
判定一致性（无检索结果时不得 `insufficient=false`；`insufficient=false` 时必须至少一条引用）。

## 5. 检索原语抽取（`core/retrieval.py`）

原先检索原语只活在 `api/routers/search_router.py`，问答要么重写一遍、要么 import 一个路由模块。
现在**唯一实现**在 `core/retrieval.py`：`grep_files` / `vector_search` / `fuse` / `run_search` / `read_entry`。

- `search_router` 保留 `_grep/_vector_search/_fuse` 作为**模块级别名**
  （`tools/eval_search.py`、`tools/tune_search.py`、`tests/test_trace.py` 依赖它们，
  其中 `test_trace` 还会 monkeypatch `sr._grep`，所以路由必须继续按自己模块内的名字调用）；
- `run_search(..., with_content=True)` 会带上条目正文（截断 2500 字/条，最多 `content_k` 条）——
  问答的依据就是这些正文，**RAW 下未编译内容不参与**（`read_entry` 只认 `NEXUS/`、`pending_review/`）；
- `read_entry` 解析后必须仍在知识库根内（拒 `../` 穿越），并对**本租户子树**解析（L3.5）。

## 6. 前端对话窗口（`/ask` 页）

页面语义与后端 `status` 一一对应，不把失败包装成答案：

| `status` | 页面表现 |
|---|---|
| `answered` | 正文 + **引用卡片**（点开抽屉看原文，走 `/entries/content`）+ 检索依据折叠 + 后续追问 chips |
| `insufficient` | 正文（模型的"我答不了，缺 X"）+ 黄色提示：依据不足，已记为知识缺口 |
| `no_hits` | 蓝色提示：知识库暂无内容，**本次未调用模型**（已记为知识缺口） |
| `failed` | 红色错误提示：契约违例/模型异常，**不给占位答案** |
| HTTP 409 | 提示未配置问答模型（`/admin/model-configs` 配 `answer` 用途） |

每条回答底部显示模型名、用时、token、trace 短 id——**可核对、可追责**。

## 7. 测试与验收

`tests/test_answer_service.py`（14 例）：
无依据不调模型 + 缺口入库；正文读不出的条目不算依据；编造路径 → 回灌后改正；
编造 quote → 回灌后改正；两次违例 → `failed` 且 `answer is None`；
无引用却给确定答案 → 违例；模型异常 → 重试后失败；正常作答（引用规范化、token、trace、命中数入日志）；
依据确实进了 prompt；`insufficient` 记缺口；模型未配置明确失败；空问题不触发任何调用；
**检索只看得见本租户子树**（同名条目各归其主）；`read_entry` 拒穿越 / 拒 RAW。

`tests/test_api_ask.py`（6 例）：未登录 401；空问题/超长 422；正常 200（引用 + 用量 + trace 头）；
缺口 200 且模型零调用；模型未配置 409；契约违例 → `failed` 且依据照实返回。

## 8. 已知取舍与未完成项

1. **对话历史不落库**：只在前端内存里，刷新即清空；也没有多轮上下文（每问都是独立检索）。
   要做得补 `ask_sessions/ask_turns` 两张表 + 多轮 query 改写，属下一轮迭代。
2. **引用粒度是条目而非段落**：`quote` 能定位到原文片段，但前端不做段落级高亮。
3. **没有流式输出**：一次请求一个完整 JSON（契约校验需要完整对象；流式需改成"流式正文 + 末尾校验"）。
4. **没有用户反馈回路**（赞/踩）：缺"答案质量"这一路的评测数据，只有缺口（无答案）一路。
5. **模型侧无重排（rerank）**：融合权重仍是 SP4 的 0.5/0.3，取 Top-K 直接送模型；
   条目多、问题宽时依据里可能有噪音（模型被要求只用依据，所以表现为"答不全"而不是"答错"）。
6. **`insufficient` 判据在模型侧**：是否"依据不足"由模型判断；代码只锁"没依据必须 insufficient"。
   想更严可以加"引用覆盖率"这类确定性指标，属后续优化。

---

## Changelog

- **v0.1（2026-09-15）**：**对话窗口落地（应用内问答 L5）**。① 新增 `core/answer_service.py`：
   门禁先于模型（无依据不调模型 + 记缺口）、引用溯源（路径 ∈ 检索结果、`quote` 逐字可查）、
   违例回灌重试 1 次、两次违例明确失败、租户模型与用量走 `model_port.for_tenant(purpose="answer")`；
   ② 检索原语抽到 `core/retrieval.py`（`search_router` 保留模块级别名，工具与测试零改动），
   新增 `run_search(with_content=...)` 与 `read_entry`（拒穿越、拒 RAW、按租户分区）；
   ③ `prompts/answer_prompt.md` 增加 **JSON 输出契约**（原 Markdown 格式保留给 CLI 入口），
   `core/output_schema.validate_answer_output` 代码化 + `tools/prompt_regression.py` 增补契约短语；
   ④ 新增 `POST /ask`（`api/routers/ask_router.py`，登录即可用；未配模型 409）
   + `ask` trace span（可观测页展示为「对话问答」）；⑤ 新增 Vue 页面「对话窗口」
   （引用卡片点开原文、依据折叠、缺口提示、后续追问）；⑥ 新增 `tests/test_answer_service.py`（14 例）
   与 `tests/test_api_ask.py`（6 例）。
