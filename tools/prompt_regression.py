"""Prompt 退化检测（落地 CLAUDE.md"Harness 用于 prompt 退化检测"承诺）。

两类检查，全部确定性、零 LLM 调用、无 DB/网络依赖（可进 CI）：

1. **契约短语存在性**：prompts/*.md 编辑时若删改硬性契约条款（枚举、必需章节、
   判定规则、溯源要求），立即 fail——防"顺手改 prompt 悄悄破坏契约"。
2. **golden 样例回归**：tools/prompt_regression_cases/*.json 与 output_schema.py
   互相锁定——valid_* 必须零错误、invalid_* 必须报错（模拟契约代码与 prompt 契约不一致时被抓出）。

用法：
    python tools/prompt_regression.py          # 通过 exit 0；违例 exit 1
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SHARED = ROOT / "streamlit_app"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from output_schema import validate_compile_output, validate_review_output

# 契约短语清单：prompt 中一旦删除/改写这些硬性条款即视为退化（与 output_schema.py 对齐）
REQUIRED_PHRASES = {
    "prompts/compile_prompt.md": [
        "resource", "concepts", "## 摘要", "## 关键信息",
        "## 定义", "## 关联知识", "source_type", "version",
    ],
    "prompts/review_prompt.md": [
        "verdict", "scores", "completeness", "sensitive",
        "approved", "rejected", "needs_human_review", "判定",
    ],
    "prompts/answer_prompt.md": [
        "严格基于检索结果", "引用可追溯", "诚实告知边界", "引用来源",
    ],
}

# golden 样例回归：文件名 → (校验函数, 期望违反数>0?)
CASES = [
    ("valid_review.json", validate_review_output, False),
    ("invalid_review.json", validate_review_output, True),
    ("valid_compile.json", validate_compile_output, False),
    ("invalid_compile.json", validate_compile_output, True),
]


def check_prompts() -> list[str]:
    fails: list[str] = []
    for rel, phrases in REQUIRED_PHRASES.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase not in text:
                fails.append(f"{rel} 缺少契约短语：{phrase!r}")
    return fails


def check_cases() -> list[str]:
    fails: list[str] = []
    cases_dir = ROOT / "tools" / "prompt_regression_cases"
    for fname, validator, expect_errors in CASES:
        p = cases_dir / fname
        if not p.exists():
            fails.append(f"样例缺失：{fname}")
            continue
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        errs = validator(data)
        if expect_errors and not errs:
            fails.append(f"{fname} 应存在契约违例但校验通过（校验与样例未互相锁定）")
        elif not expect_errors and errs:
            fails.append(f"{fname} 应零错误却报 {len(errs)} 处：{errs[0]}")
    return fails


def main() -> int:
    print("=== Prompt 退化检测 ===")
    errs = []
    p_fails = check_prompts()
    c_fails = check_cases()
    print(f"契约短语检查: {'OK' if not p_fails else f'{len(p_fails)} 处缺失'}")
    for f in p_fails:
        print(f"  - {f}")
    print(f"golden 样例回归: {len(CASES)} 个用例"
          + (" OK" if not c_fails else f"，{len(c_fails)} 处违例"))
    for f in c_fails:
        print(f"  - {f}")
    if p_fails or c_fails:
        print("[FAIL] prompt 契约存在退化风险")
        return 1
    print("[OK] 3 个 prompt 契约短语完整、4 个 golden 样例与输出校验互相锁定")
    return 0


if __name__ == "__main__":
    sys.exit(main())