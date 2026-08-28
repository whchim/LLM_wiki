#!/usr/bin/env python3
"""SP5 健康巡检引擎（确定性，TDD）：孤立节点 / wikilink 断链 / 过期 / 相似候选。

输出三件套：
1. health_reports 表落一行（五指标 + detail 明细）
2. NEXUS/研究/健康周报_<date>.md（人类可读，Obsidian 可看）
3. vault/_triggers/.similarity_candidates_<date>.json（相似候选，供 Claude 轨复核）

用法：python tools/health_check.py [--kb <vault路径>]
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

STALE_DAYS = 180          # 过期阈值（PRD 6.4）
SIMILAR_THRESHOLD = 0.85  # 相似候选判定（字符基线，LLM 复核语义）
WIKILINK_RE = re.compile(r"\[\[概念-([^\]\|#]+)")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _kb_root(args) -> Path:
    return Path(args.kb or os.environ.get("KB_ROOT", str(ROOT / "vault")))


def _parse_frontmatter(text: str) -> dict:
    """极简 frontmatter 解析（key: value 行式，够用且零依赖）。"""
    m = FRONTMATTER_RE.match(text)
    meta = {}
    if not m:
        return meta
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta


def _load_concepts(kb: Path) -> list[dict]:
    """加载 NEXUS/概念/ 全部条目：路径/标题/正文/出链集合。"""
    concepts = []
    cdir = kb / "NEXUS" / "概念"
    if not cdir.exists():
        return concepts
    for p in sorted(cdir.glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        meta = _parse_frontmatter(text)
        title = meta.get("title") or p.stem
        out_links = {m.group(1).strip() for m in WIKILINK_RE.finditer(text)}
        concepts.append({
            "path": f"NEXUS/概念/{p.name}",
            "title": title,
            "meta": meta,
            "text": text,
            "out_links": out_links,
        })
    return concepts


def check_orphans(concepts: list[dict]) -> list[str]:
    """孤立节点：零入链且零出链。"""
    # 入链表：被哪些概念的出链引用（按标题匹配）
    referenced: set[str] = set()
    for c in concepts:
        referenced |= c["out_links"]
    return [c["path"] for c in concepts
            if c["title"] not in referenced and not c["out_links"]]


def check_broken_links(concepts: list[dict]) -> list[dict]:
    """wikilink 断链：出链目标标题在概念集中不存在。"""
    titles = {c["title"] for c in concepts}
    broken = []
    for c in concepts:
        for link in c["out_links"]:
            if link not in titles:
                broken.append({"from": c["path"], "link": f"概念-{link}"})
    return broken


def check_stale(concepts: list[dict], today: date) -> list[dict]:
    """过期：updated（缺则 created）距今 > STALE_DAYS。"""
    out = []
    for c in concepts:
        raw = c["meta"].get("updated") or c["meta"].get("created")
        if not raw:
            continue
        try:
            d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days > STALE_DAYS:
            out.append({"path": c["path"], "last": raw[:10],
                        "days": (today - d).days})
    return out


def check_similar(concepts: list[dict]) -> list[dict]:
    """相似候选：title+description 两两 SequenceMatcher ≥ 阈值。"""
    pairs = []
    n = len(concepts)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = concepts[i], concepts[j]
            sig_a = f"{a['title']} {a['meta'].get('description', '')}"
            sig_b = f"{b['title']} {b['meta'].get('description', '')}"
            ratio = SequenceMatcher(None, sig_a, sig_b).ratio()
            if ratio >= SIMILAR_THRESHOLD:
                pairs.append({"a": a["path"], "b": b["path"],
                              "a_title": a["title"], "b_title": b["title"],
                              "ratio": round(ratio, 3)})
    return pairs


def run(kb: Path) -> dict:
    """执行全量巡检，返回结果字典（供落库/周报/测试复用）。"""
    today = date.today()
    concepts = _load_concepts(kb)
    orphans = check_orphans(concepts)
    broken = check_broken_links(concepts)
    stale = check_stale(concepts, today)
    similar = check_similar(concepts)
    return {
        "date": today.isoformat(),
        "total": len(concepts),
        "orphans": orphans,
        "broken": broken,
        "stale": stale,
        "similar": similar,
    }


def save_report(db_mod, result: dict) -> None:
    """落 health_reports 一行（含相比上次的增长率）。"""
    import json as _json
    prev = db_mod.get_conn()
    with prev as conn:
        last = conn.execute(
            "SELECT total_entries, report_date FROM health_reports "
            "ORDER BY id DESC LIMIT 1").fetchone()
        growth = None
        if last and last[0]:
            growth = round((result["total"] - last[0]) / last[0], 4)
        conn.execute(
            "INSERT INTO health_reports (report_date, orphan_count, broken_link_count, "
            "stale_count, conflict_count, total_entries, growth_rate, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (result["date"], len(result["orphans"]), len(result["broken"]),
             len(result["stale"]), len(result["similar"]), result["total"],
             growth, _json.dumps(result, ensure_ascii=False)))


def write_markdown(kb: Path, result: dict, growth) -> Path:
    """写人类可读周报到 NEXUS/研究/。"""
    rd = kb / "NEXUS" / "研究"
    rd.mkdir(parents=True, exist_ok=True)
    out = rd / f"健康周报_{result['date']}.md"
    lines = [
        f"---", "type: research", f"title: 健康周报 {result['date']}",
        'status: active', f"created: \"{result['date']}\"", 'tags: [健康巡检]',
        "---", "",
        f"# 健康周报 {result['date']}", "",
        f"> 概念总数 {result['total']} · 孤立 {len(result['orphans'])} · "
        f"断链 {len(result['broken'])} · 过期 {len(result['stale'])} · "
        f"相似候选 {len(result['similar'])} 对"
        + (f" · 较上次增长 {growth:.0%}" if growth is not None else ""), "",
    ]
    if result["orphans"]:
        lines += ["## 孤立节点（零链接）", ""]
        lines += [f"- `{p}`" for p in result["orphans"]] + [""]
    if result["broken"]:
        lines += ["## wikilink 断链", ""]
        lines += [f"- `{b['from']}` → {b['link']}" for b in result["broken"]] + [""]
    if result["stale"]:
        lines += [f"## 过期（>{STALE_DAYS} 天未更新）", ""]
        lines += [f"- `{s['path']}`（最后更新 {s['last']}，{s['days']} 天）"
                  for s in result["stale"]] + [""]
    if result["similar"]:
        lines += ["## 相似候选（待 LLM 复核）", ""]
        lines += [f"- `{p['a']}` ↔ `{p['b']}`（相似度 {p['ratio']}）"
                  for p in result["similar"]] + [""]
    if not (result["orphans"] or result["broken"] or result["stale"] or result["similar"]):
        lines += ["全部指标健康 🎉", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库健康巡检")
    parser.add_argument("--kb", default=None, help="vault 路径（默认 KB_ROOT/项目 vault）")
    args = parser.parse_args()
    kb = _kb_root(args)

    result = run(kb)
    growth = None
    try:
        import db
        prev = db.get_conn()
        with prev as conn:
            last = conn.execute(
                "SELECT total_entries FROM health_reports ORDER BY id DESC LIMIT 1").fetchone()
        growth = round((result["total"] - last[0]) / last[0], 4) if last and last[0] else None
        save_report(db, result)
    except Exception as e:
        print(f"[health] 数据库不可用，仅写周报文件：{e}", file=sys.stderr)

    out = write_markdown(kb, result, growth)
    print(f"[health] 孤立 {len(result['orphans'])} | 断链 {len(result['broken'])} | "
          f"过期 {len(result['stale'])} | 相似 {len(result['similar'])} 对 | "
          f"周报: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())