"""LLM 输出契约校验 CLI（引擎自检门禁 + CI/健康巡检可调用）。

把 prompts 里的输出契约变成可执行断言：LLM（Claude Code 引擎）在编译/审核
产物写入前调用本工具自检，失败则重试或标记 failed——"输出可不可接受"由代码判定，
不靠 prompt 自觉。校验规则见 streamlit_app/output_schema.py 与
docs/LLM_输出校验_设计说明.md。

用法：
    python tools/validate_llm_output.py review <output.json>      # 审核 Agent 六维度 JSON
    python tools/validate_llm_output.py compile <output.json>     # 编译 Agent JSON（resource + concepts[]）
    python tools/validate_llm_output.py frontmatter <entry.md>    # 落盘条目（解析 YAML Frontmatter 校验）
    # <path> 传 "-" 则从 stdin 读（便于引擎管道调用）

退出码：0=合法；1=不合法（逐条打印错误）；2=用法错误。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SHARED = ROOT / "streamlit_app"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import yaml  # pyyaml（requirements 已含）

from output_schema import (validate_compile_output, validate_entry_frontmatter,
                           validate_review_output)

VALIDATORS = {
    "review": (validate_review_output, "审核 Agent 六维度 JSON"),
    "compile": (validate_compile_output, "编译 Agent JSON（resource + concepts）"),
    "frontmatter": (validate_entry_frontmatter, "落盘条目 YAML Frontmatter"),
}


def _read_all(src: str) -> str:
    """读取输入（文件或 stdin）。统一 utf-8-sig：自动剥离 UTF-8 BOM
    （Windows 工具链常见，json/YAML 解析会拒绝 BOM）。"""
    if src == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig")
    return Path(src).read_text(encoding="utf-8-sig")


def _load_json(src: str) -> dict:
    return json.loads(_read_all(src))


def _load_frontmatter(src: str) -> dict:
    text = _read_all(src)
    if not text.startswith("---"):
        return {}
    _, fm_block, _rest = text.split("---", 2)
    parsed = yaml.safe_load(fm_block)
    return parsed if isinstance(parsed, dict) else {}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in VALIDATORS:
        print("用法: python tools/validate_llm_output.py <review|compile|frontmatter> <path|->", file=sys.stderr)
        return 2
    kind, src = argv[1], argv[2]
    load = _load_frontmatter if kind == "frontmatter" else _load_json
    try:
        data = load(src)
    except (json.JSONDecodeError, yaml.YAMLError, OSError) as e:
        print(f"[ERROR] 输入解析失败: {e}", file=sys.stderr)
        return 1
    validator, desc = VALIDATORS[kind]
    errs = validator(data)
    if not errs:
        print(f"[OK] {desc} 契约校验通过")
        return 0
    print(f"[FAIL] {desc} 存在 {len(errs)} 处契约违例：")
    for e in errs:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))