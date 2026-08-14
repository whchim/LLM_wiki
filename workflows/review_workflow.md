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
        - 维度1 完整性 + 维度5 敏感信息：调用确定性规则单一权威实现 rules.py：
          `python -c "import sys; sys.path.insert(0, 'streamlit_app'); from rules import check_completeness, check_sensitive; ..."`
          （rules.py 是确定性规则的单一权威实现，覆盖 review_prompt 维度五全表模式，Task 13 交付；
          若 rules.py 不存在，则按下方兜底参考临时执行并在判定中记录）
        - 兜底参考（仅 rules.py 缺失时使用，以 rules.py 为准）：
          - 维度1 完整性：四字段（type/title/status/source）非空 + 正文≥100中文字符
          - 维度5 敏感信息：18位身份证→blocked；11位手机号→warning；邮箱格式→warning；sk-/api_key/token/secret→blocked；password:→blocked；>100万元精确金额→warning；"机密/绝密/内部/confidential"→blocked
      - LLM 子 Agent（每维度一个，按 prompts/review_prompt.md 对应节）：
        - 维度2 去重（对候选列表）· 维度3 职务归属 · 维度4 质量（1-5分）· 维度6 合规
   d. 按 review_prompt.md 判定逻辑链汇总 verdict（sensitive=blocked 一票否决等）
   e. 写入审核结果：若该 nexus_path 已有 human_decision IS NULL 的行则 UPDATE 该行（按 id 更新 ai_verdict/ai_scores），否则 INSERT。调用 `streamlit_app/db.py` 的 `insert_review(nexus_path, 'demo_user', department, ai_verdict, ai_scores)` 函数签名（python -c 内联，如 `import sys; sys.path.insert(0, 'streamlit_app'); from db import insert_review; insert_review(...)`），不手写字段列表
2. 触发文件处理完毕移入 vault/_triggers/done/
3. 返回：审核 N 条、判定分布

## 输出验收
- pending_reviews 每条目一行，ai_scores 为完整 JSON（verdict/department/scores/duplicates/concerns/summary）
- 审核页可展示该判定
