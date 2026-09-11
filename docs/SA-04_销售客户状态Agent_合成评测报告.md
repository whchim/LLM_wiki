# 销售客户状态 Agent 合成评测报告

> 数据集：`synthetic_sales_state_v1`  
> 运行脚本：`python tools/eval_sales_state.py --json`  
> 评测性质：synthetic evaluation，仅验证确定性门禁、结构化契约和回放可重复性，不代表真实企业业务效果。

## 1. 数据集

共 36 条合成销售文字记录：

| 类型 | 数量 | 目的 |
|---|---:|---|
| normal | 24 | 覆盖新线索、接触、需求确认、方案评估、商务谈判和成交建议 |
| prompt_injection | 4 | 验证明显指令混入在调用 Agent 前被拦截 |
| numeric_unprotected | 4 | 验证未配置受控加密器时敏感数值不能进入 Agent |
| future_time | 4 | 验证异常未来时间进入人工处理 |

## 2. 回放结果

| 指标 | 结果 |
|---|---:|
| 预处理接受率 | 66.67%（24/36） |
| 预处理拦截率 | 33.33%（12/36） |
| 已接受样本的 Agent 契约通过率 | 100%（24/24） |
| 契约通过样本的证据可定位率 | 100%（24/24） |
| 估算输入 token | 220 |
| 估算输出 token | 2,880 |
| 估算总 token | 3,100 |

路由统计：规则拦截 12 条，便宜模型候选 24 条，强模型候选 4 条（`won`），人工确认 24 条。

## 3. 失败与限制

- 回放使用固定 oracle 输出，只能验证字段、证据定位、状态转移和人工门禁，不能证明 LLM 的真实语义判断准确率；
- token 是按字符长度和固定输出预算估算，不是模型供应商账单；
- 没有真实客户数据、真实录音转写或真实销售结果，因此不计算真实状态建议准确率；
- 未来应追加 30 至 50 条人工标注案例，并单独记录人工修改率、冲突率、延迟和重试率。

## 4. 可重复运行

```powershell
python tools/eval_sales_state.py
python tools/eval_sales_state.py --json
python tools/eval_sales_state.py --write-dataset docs/synthetic_sales_state_v1.jsonl
```
