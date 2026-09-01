"""评估汇总：解析标注表，输出简历可用的质量数字。

用法：人工在 docs/评估_概念页质量标注表.md 填完三列后运行：
    python tools/eval_summary.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "docs" / "评估_概念页质量标注表.md"


def main() -> None:
    text = SHEET.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\| (\d+) \| (.+?) \| (\d+) \| (\d+) \| (✓|✗) \| (.*?) \| (.*?) \| (.*?) \|", line)
        if not m:
            continue
        rows.append({
            "n": int(m.group(1)), "title": m.group(2).strip(),
            "chars": int(m.group(3)), "links": int(m.group(4)),
            "structure": m.group(5) == "✓",
            "fact": m.group(6).strip(),
            "recall": m.group(7).strip(),
            "readable": m.group(8).strip(),
        })

    if not rows:
        print("未解析到标注行——请确认表格已填写")
        return

    total = len(rows)
    fact_ok = sum(1 for r in rows if r["fact"] == "✓")
    fact_partial = sum(1 for r in rows if r["fact"] == "部分")
    recalls = []
    for r in rows:
        raw = r["recall"].rstrip("%").strip()
        if raw and raw.replace(".", "").isdigit():
            v = float(raw)
            recalls.append(v / 100 if v > 1 else v)   # 95 → 0.95，0.95 → 0.95
    readable_ok = sum(1 for r in rows if r["readable"] == "✓")
    struct_ok = sum(1 for r in rows if r["structure"])

    print(f"样本量: {total} 个概念页")
    print(f"结构完整率: {struct_ok}/{total} = {struct_ok / total:.0%}")
    print(f"事实正确率: {fact_ok}/{total} = {fact_ok / total:.0%}"
          + (f"（另有 {fact_partial} 条'部分'）" if fact_partial else ""))
    if recalls:
        print(f"平均信息点召回: {sum(recalls) / len(recalls):.0%}"
              f"（标注 {len(recalls)} 页，最低 {min(recalls):.0%}，最高 {max(recalls):.0%}）")
    print(f"可独立阅读率: {readable_ok}/{total} = {readable_ok / total:.0%}")
    print(f"平均字数: {sum(r['chars'] for r in rows) // total} 字 | "
          f"平均 wikilink: {sum(r['links'] for r in rows) / total:.1f} 条/页")
    print("\n—— 简历/面试可引用句式 ——")
    print(f"· 对 {total} 个 LLM 编译概念页的人工评估：结构完整率 {struct_ok / total:.0%}、"
          f"事实正确率 {fact_ok / total:.0%}"
          + (f"、平均信息点召回 {sum(recalls) / len(recalls):.0%}" if recalls else "")
          + f"、可独立阅读率 {readable_ok / total:.0%}")


if __name__ == "__main__":
    main()