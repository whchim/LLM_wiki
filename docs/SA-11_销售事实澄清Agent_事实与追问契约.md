# 销售事实澄清 Agent：事实与追问契约

> 版本：v1 ｜ 阶段：1 ｜ 状态：已实现，等待负责人确认

## 1. 契约目的

本契约约束 Agent 的“理解层”输出。它只允许模型回答三件事：

1. 文本中有哪些可定位的销售事实；
2. 哪些事实归属于客户原话、销售判断或外部信息；
3. 哪些缺口会影响状态判断，是否需要最多两个追问。

Agent 不得在本契约中写入客户当前状态，也不得直接调用确认、撤回或更正接口。

## 2. 事实本体

| 类型 | 含义 | 示例 |
|---|---|---|
| `customer_need` | 客户明确需求或目标 | 客户希望先验证多租户能力 |
| `customer_commitment` | 客户明确承诺或同意 | 客户同意安排技术评审 |
| `objection` | 客户异议、阻塞或担忧 | 客户担心实施周期 |
| `decision_maker` | 决策角色或参与人 | 技术负责人参与评审 |
| `timeline` | 客户给出的时间约束 | 客户希望本月底完成评估 |
| `next_step` | 已约定的下一步动作 | 下周二双方进行演示 |
| `competitor_signal` | 竞品、替代方案或比较信号 | 客户同时评估另一家方案 |

第一版不抽取泛化的“客户情绪”“成交概率”或无法被证据支持的隐含意图。

## 3. 归因与确定性

### 3.1 归因

- `customer_quote`：客户明确说过或明确同意的内容；
- `salesperson_interpretation`：销售人员的判断、推测或总结；
- `external_fact`：可引用的外部事实；
- `unknown`：文本无法确定归属。

销售判断不得伪装成客户承诺。`customer_quote` 必须有精确证据，不能标记为 `unknown`。

### 3.2 确定性

- `explicit`：原文直接表达；
- `ambiguous`：存在语义线索但表达不完整或有歧义；
- `unknown`：无法从文本确认。

## 4. 最小证据矩阵

| 候选状态 | 至少需要的事实 | 不足时动作 |
|---|---|---|
| `new_lead` | 有客户主体或接触记录 | 无主体则拒绝处理 |
| `contacted` | 至少一次有效接触证据 | 缺失则追问接触是否发生 |
| `need_confirmed` | 客户需求或问题的客户归因证据 | 只能把销售判断作为待验证信息 |
| `solution_eval` | 需求 + 评估/试用/技术验证的明确证据 | 缺任一项则追问或转人工 |
| `commercial_negotiation` | 商务讨论、报价或合同推进的证据 | 不得仅凭“客户感兴趣”推进 |
| `won` | 客户明确签署/采购/成交承诺 | 永远需要负责人确认 |
| `lost_or_paused` | 客户明确暂停、拒绝或暂不推进 | 模糊的“再看看”不得直接判定丢失 |

`expired` 是系统时间事件，不是 Agent 可提出的目标状态。

## 5. 缺失事实与追问

`MissingFact` 必须包含：

- `type`：缺少哪类事实；
- `why_needed`：为什么会影响候选状态；
- `impact_states`：受影响的有限状态集合；
- `priority`：高、中、低。

`ClarificationQuestion` 必须引用一个 `missing_fact_id`，并声明回答类型。追问规则：

1. 每轮最多 2 个问题，最多 2 轮；
2. 只问会改变状态判断或负责人决策的问题；
3. 不重复询问已有证据；
4. 不索取身份证、手机号、密钥、精确金额、具体报价等非必要敏感信息；
5. 无法通过有限追问解决时，返回 `human_review` 或 `insufficient_evidence`。

## 6. 停止条件

| `stop_reason` | 含义 | `can_propose` |
|---|---|---:|
| `ready_for_proposal` | 事实和证据足以形成候选建议 | `true` |
| `needs_clarification` | 存在影响状态的缺口，返回 1-2 个问题 | `false` |
| `insufficient_evidence` | 证据不足且不适合继续追问 | `false` |
| `human_review` | 存在冲突、敏感或高风险情况 | `false` |

## 7. JSON 形状

```json
{
  "schema_version": "clarification.v1",
  "claims": [{
    "id": "claim-1",
    "type": "customer_commitment",
    "attribution": "customer_quote",
    "certainty": "explicit",
    "value": "客户同意安排技术评审",
    "evidence": [{"source": "initial_note", "quote": "客户同意安排技术评审", "start": 0, "end": 12}]
  }],
  "missing_facts": [],
  "questions": [],
  "stop_reason": "ready_for_proposal",
  "can_propose": true,
  "model_version": "provider-model-v1",
  "prompt_version": "sales-clarification-v1"
}
```

## 8. 阶段 1 验收

- 纯 JSON、字段枚举、证据边界和证据精确匹配均有确定性校验；
- 事实归因、缺失事实、追问与停止原因存在一致性校验；
- 追问数量、敏感信息和状态影响集合存在确定性门禁；
- 解析与校验不调用 LLM、不写数据库、不改变客户状态；
- 契约测试覆盖合法结果、错误归因、假证据、重复追问、越界状态、敏感追问和停止条件。
