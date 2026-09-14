# 销售事实澄清 Agent：模型调用契约

> 版本：v2 ｜ 阶段：3 ｜ 状态：已实现，等待负责人确认（v2：补充状态 Agent 复用同一端口，见第 5 节）

## 1. 端口边界

`ModelPort.complete()` 是唯一模型调用边界。供应商适配器只返回 `ModelResponse`，不得在适配器内写客户状态或数据库。当前仓库不绑定新的 LLM SDK，Claude Code、HTTP 服务和 fake port 都可以实现该接口。

## 2. 运行时行为

1. 运行时只接收脱敏正文、脱敏客户标识和当前状态；
2. 输入超过 12,000 字符或输出超过 20,000 字符直接失败；
3. 默认最大输出 2,000 tokens，默认失败后只重试 1 次；
4. 每次返回都经过纯 JSON 解析和 `clarification_schema` 校验；
5. 契约失败或模型异常最终返回 `needs_human_review`；
6. 记录模型版本、Prompt 版本、调用次数、token、耗时和错误摘要；
7. 运行时不创建 `StateProposal`，不更新 `CurrentState`，不调用负责人决定接口。

## 3. 重试与降级

重试只用于瞬时模型错误或可修复的契约错误，不进行无限重试。达到上限后由上层转人工或走传统表单基线。`audit_dict()` 不包含销售正文和模型原始输出，避免把敏感内容写进普通审计日志。

## 4. 阶段 3 验收

- fake port 合法输出可被接受；
- 假证据、非 JSON、模型超时等失败可被发现并按上限重试；
- token、耗时、模型/Prompt 版本可审计；
- 超长输入和超出 token 上限在调用模型前被拒绝；
- 阶段 1 的证据、归因、追问和停止条件校验继续生效。

## 5. 状态 Agent 复用同一端口（v2 补充）

状态判定（销售客户状态 Agent）**复用** `ModelPort`，不引入第二套调用层：

| 项 | 澄清 Agent | 状态 Agent（`sales_state_agent.run_state_agent`） |
|---|---|---|
| 提示词 | `prompts/clarification_prompt.md` | `prompts/sales_state_prompt.md` |
| 输出契约 | `core/clarification_schema.py` | `sales_state_agent.validate_state_agent_output`（字段白名单 + 状态机 + 证据可定位） |
| 证据偏移 | 契约内校验 | **服务端按 `quote` 重新定位**（模型给的 start/end 不可信），定位失败即拒绝 |
| 降级 | 契约失败 → `needs_human_review` | 契约失败 → 重试 1 次 → 服务层回退确定性规则；`mode=llm` 时直接 409 |
| 审计 | `audit_dict()`（不含原文） | 同左（`StateAgentRun.audit_dict()`），另在建议里落 `model_version`/`prompt_version` |

调用纪律不变：**只产出建议**，不写 `StateProposal`、不改 `CurrentState`、不碰 `state_events`；默认只取脱敏正文与追问回答，精确金额始终走占位符。
