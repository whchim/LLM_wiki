# LLM 输出契约校验（设计说明）

> **版本**：v0.1 ｜ **日期**：2026-09-01
>
> **定位**：把 prompts/ 里的输出契约从"prompt 自觉"变成**代码级可执行断言**——LLM 输出不确定性是 Agent 工程的第一不确定源，确定性校验必须落在代码与测试上（沿用"规则明确交程序"范式，与 rules.py 同款）。

## 1. 三组校验（streamlit_app/output_schema.py）

| 校验 | 产物入口 | 契约来源 | 检查内容 |
|------|---------|---------|---------|
| `validate_review_output` | 审核 Agent 六维度 JSON | prompts/review_prompt.md「输出格式」+「判定逻辑」 | verdict/department/scores 枚举、quality 1-5 整数、duplicates/concerns 字符串数组、summary 非空、**判定一致性**（sensitive=blocked→rejected、insufficient→非 approved、duplicate→rejected、quality≤2→非 approved） |
| `validate_compile_output` | 编译 Agent JSON | prompts/compile_prompt.md「输出格式」+「编译规则」 | resource 必填+枚举（department/source_type）、summary 含 `## 摘要`/`## 关键信息`、tag/key_points 数组；concepts 可空数组、title 非空且全文档唯一、content 含四章节（定义/背景/关键细节/关联知识）、department 枚举 |
| `validate_entry_frontmatter` | 落盘条目 YAML Frontmatter | vault/SCHEMA.md | type/status 枚举、title≤30 字、source 非空、version 格式 `V{major}.{minor}`、department 枚举、tags 全部落在三类预定义命名空间 |

返回值约定：`list[str]` 错误明细；**空列表 = 合法**。模块纯函数、零 IO、零 DB——可单测可复用。

## 2. 接入点

| 位置 | 方式 | 失败语义 |
|------|------|---------|
| **审核读侧（已接入）** | `api/routers/review_router.py` `_to_out()`：`/reviews/pending|rejected` 响应新增 `ai_scores_valid` / `ai_scores_errors` | 非阻断：审核页可直观看到"AI 输出契约违例"（可观测，不破坏现有流程） |
| **引擎自检（已提供 CLI）** | `tools/validate_llm_output.py`：`review <json>` / `compile <json>` / `frontmatter <md>`，支持 stdin（`-`） | 退出码 0/1/2；引擎（Claude Code）编译/审核后自检，失败重试或标记 failed |
| **写侧（预留）** | 若未来 Python 直接写 ai_scores / 编译产物落盘，写入前先调对应 validator | 拒绝写入 + 错误明细 |

不侵入既有写路径：`db.insert_review` 等被测试以 `"{}"` 脚手架调用，校验塞入会破坏既有语义——故采用读侧标记 + CLI 自检的增量方案。

## 3. CLI 用法

```bash
python tools/validate_llm_output.py review <output.json>     # <-> 从 stdin 读
python tools/validate_llm_output.py compile <output.json>
python tools/validate_llm_output.py frontmatter <entry.md>
```

输入统一以 utf-8-sig 读取（自动剥 UTF-8 BOM——Windows 工具链常见，json/YAML 会拒绝 BOM）。

## 4. 测试（tests/test_output_schema.py，23 个用例）

- 合法样本（真实页面 frontmatter 形态 + 六维度/编译契约完整 JSON）→ 零错误
- 每类枚举非法、类型错（quality="4"）、缺字段（scores/summary/resource）→ 报错
- 判定一致性 4 例（blocked/insufficient/duplicate/quality≤2）× verdict 矛盾 → 报错
- 编译细节：summary 缺章节、concept title 重复、concept 缺章节、空 concepts 合法
- Frontmatter：version/status/type 非法、tags 越命名空间、缺 source、title 超长

## 5. 面试口径

"LLM 输出不确定性是第一不确定源。我的处理不是靠 prompt 自觉：把 prompts 里的 JSON 契约全部代码化——三组校验（审核六维度、编译产物、落盘 frontmatter），含判定逻辑一致性检查（比如 sensitive=blocked 却给了 approved 会直接报违例）；23 个测试锁边界；对外提供 CLI 让引擎自检（退出码门禁），审核 API 在读侧暴露 ai_scores_valid 标记让异常可见。规则明确交程序、拿不准交模型——这句落到实处就是这套校验。"

---

## Changelog

- **v0.1（2026-09-01）**：初稿。新增 streamlit_app/output_schema.py（三组校验）、tools/validate_llm_output.py（CLI 自检门禁，utf-8-sig 容错 BOM）、/reviews 响应 ai_scores_valid 标记（ReviewOut + _to_out）、tests/test_output_schema.py（23 用例）。不侵入既有写路径。