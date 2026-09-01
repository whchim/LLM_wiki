"""构建 mini 效果评估底表：概念页清单 + 机器可算指标预填（纯文件扫描，零 DB 依赖）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "vault"


def main() -> None:
    concepts = sorted((KB / "NEXUS" / "概念").glob("*.md"))
    print(f"概念页总数: {len(concepts)}")

    lines = ["| # | 概念页 | 字数 | wikilink 数 | 结构完整(定义/背景/细节/关联) | 事实正确 | 信息点召回 | 可独立阅读 | 备注 |",
             "|---|--------|------|------------|------------------------------|---------|-----------|-----------|------|"]
    for i, p in enumerate(concepts, 1):
        text = p.read_text(encoding="utf-8", errors="replace")
        chars = len(text)
        links = text.count("[[")
        structure = all(s in text for s in ("## 定义", "## 背景", "## 关键细节", "## 关联知识"))
        title = p.stem
        lines.append(f"| {i} | {title} | {chars} | {links} | "
                     f"{'✓' if structure else '✗'} |  |  |  |  |")

    out = ROOT / "docs" / "评估_概念页质量标注表.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        "# 概念页编译质量标注表（mini 评估集）\n\n"
        "> 标注方法：对照 `vault/RAW/` 原文档，逐页判断三列——\n"
        "**事实正确**（无编造，✓/✗/**部分**）；**信息点召回**（原文关键信息点被提取的比例，估 %）；\n"
        "**可独立阅读**（脱离原文档后能否被新人读懂，✓/✗）。\n"
        "标注完成后肉眼汇总：准确率 = 事实正确✓数/总数；平均召回 = 各页召回均值。\n\n"
        + "\n".join(lines) + "\n",
        encoding="utf-8")
    print(f"标注表已生成: {out}")


if __name__ == "__main__":
    main()