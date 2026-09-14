"""本地专用：把实习期 RAW 语料灌进「仓库外」的独立 vault（源目录只读，不改动）。

- 源：D:\\桌面\\项目学习\\华泰智远知识库\\RAW（**只读**）
- 目标：D:\\桌面\\项目学习\\LLM_wiki_local\\vault\\RAW\\<分类>\\（保持原有分类目录）
- 跳过门禁 blocked 的文件（身份证/密钥/明文密码/内部标记），并打印清单
- 分批灌入：--limit 控制本次拷入篇数（首批试跑用），默认全部
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\桌面\LLM_wiki\core")
import rules  # noqa: E402

SRC = Path(r"D:\桌面\项目学习\华泰智远知识库\RAW")
VAULT = Path(r"D:\桌面\项目学习\LLM_wiki_local\vault")

DIRS = ["RAW", "pending_review", "NEXUS/资源", "NEXUS/概念", "NEXUS/研究", "_triggers/done"]


def ensure_tree() -> None:
    for rel in DIRS:
        (VAULT / rel).mkdir(parents=True, exist_ok=True)
    (VAULT / "SCHEMA.md").write_text(
        "# 知识库 Schema\n## 1. 合法 Type 列表\n- concept / resource / research / glossary\n"
        "## 2. 合法 Status 列表\n- draft / pending / active / stale / deprecated\n"
        "## 3. 合法 Tags 命名空间\n- 部门: 销售/售前/产品/实施交付/开发/财务/人事/行政/共享层\n"
        "- 领域: AI/大数据/云计算/安全/项目管理/产品设计/应急管理/智慧城市/物联网/数字孪生\n"
        "- 类型: 实战经验/技术方案/产品文档/会议纪要/复盘总结/行业研究/标准规范/培训材料\n"
        "## 4. Frontmatter 字段规范（详见设计文档 4.2）\n## 5. 文件名与 Wikilink 约定（详见设计文档 5.4）\n"
        "## 6. 版本号规则\n- 格式 V{major}.{minor}，首次入库 V1.0；正文微调 V1.1；核心定义改写 V2.0\n",
        encoding="utf-8")
    index = VAULT / "NEXUS/index.md"
    if not index.exists():
        index.write_text("# 知识库索引\n\n（编译时由 Claude Code 逐次更新）\n", encoding="utf-8")
    log = VAULT / "NEXUS/log.md"
    if not log.exists():
        log.write_text("", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="本次最多拷入篇数（0=全部）")
    args = parser.parse_args()

    ensure_tree()
    files = sorted(p for p in SRC.rglob("*.md") if p.is_file())
    copied, skipped = [], []
    for path in files:
        if args.limit and len(copied) >= args.limit:
            break
        text = path.read_text(encoding="utf-8", errors="replace")
        if rules.check_sensitive(text) == "blocked":
            skipped.append(path)
            continue
        rel = path.relative_to(SRC)
        target = VAULT / "RAW" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)

    print(f"源文件: {len(files)}")
    print(f"本次拷入: {len(copied)} → {VAULT / 'RAW'}")
    print(f"跳过（门禁 blocked）: {len(skipped)}")
    for path in skipped:
        print(f"   - {path.parent.name}/{path.name}")
    print(f"vault 内累计 RAW 篇数: {len([p for p in (VAULT / 'RAW').rglob('*.md')])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
