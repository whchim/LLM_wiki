# 六维度审核 Workflow

**触发**：/process-triggers 消费 review_*.md 时执行。

## 输入
- 待审条目路径列表（来自触发文件）

## 步骤
1. 对每个条目：
   a. `cat` 读取 Markdown 全文（含 YAML）
   b. 构造去重候选：`grep -l "<标题关键词>" vault/NEXUS vault/pending_review --include=*.md`（排除自身），每个候选取路径+标题+前200字，最多 5 个
   c. 并行执行（Harness parallel）：
      - 确定性检查（bash/python，不调 LLM）：
        - 维度1 完整性：四字段（type/title/status/source）非空 + 正文≥100中文字符
        - 维度5 敏感信息：正则（18位身份证→blocked；11位手机号→warning；sk-开头→blocked；password:→blocked；"机密/绝密"→blocked）
      - LLM 子 Agent（每维度一个，按 prompts/review_prompt.md 对应节）：
        - 维度2 去重（对候选列表）· 维度3 职务归属 · 维度4 质量（1-5分）· 维度6 合规
   d. 按 review_prompt.md 判定逻辑链汇总 verdict（sensitive=blocked 一票否决等）
   e. `sqlite3` INSERT INTO pending_reviews (nexus_path, 'demo_user', department, ai_verdict, ai_scores=完整JSON, datetime('now','localtime'))
2. 触发文件处理完毕移入 vault/_triggers/done/
3. 返回：审核 N 条、判定分布

## 输出验收
- pending_reviews 每条目一行，ai_scores 为完整 JSON（verdict/department/scores/duplicates/concerns/summary）
- 审核页可展示该判定
