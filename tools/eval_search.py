"""SP4 混合检索评测：在黄金集上跑三通道对比（grep / vector / 融合），输出检索指标。

用法：
    python tools/eval_search.py
    python tools/eval_search.py --detail          # 逐查询打印 fused 排序前 K

环境：
- 完整评测需 PostgreSQL（pgvector 已回填 embedding）＋ DASHSCOPE_API_KEY
- 未配置 key：vector/fused 标注 N/A（fused 自动降级 grep-only），仅 grep 列有效
- DB 不可用/embedding 失败：_vector_search 返回 None（降级），评测不崩——与线上降级铁律一致

设计要点：
1. 评测直接复用 api.routers.search_router 的检索原语（_grep/_vector_search/_fuse），
   **不经过 /search 端点**——避免评测查询写入 search_logs 污染知识缺口看板
   （评测查询 ≠ 用户真实查询，且缺口判据要求"零命中才记缺口"）。
2. 黄金集见 docs/检索评测_黄金集.md；预期命中为人工标注的"知识上应命中"条目。
3. 指标：MRR@10（首个预期命中的位置倒数）、Recall@10（top10 中预期命中的比例）、
   缺口检出力（缺口查询被误报为命中的比例）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# search_router 内部 `import db` 指向 streamlit_app/（与 api/main.py 相同的兜底）
_SHARED = ROOT / "streamlit_app"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from api import embedding
from api.routers import search_router as sr

GOLD_PATH = ROOT / "docs" / "检索评测_黄金集.md"
K = 10  # MRR/Recall 的截断深度


def parse_gold() -> list[dict]:
    """解析黄金集 Markdown 表格 → [{n, query, type, expected, note}]。"""
    text = GOLD_PATH.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\| (\d+) \| (.+?) \| (精确|语义|缺口) \| (.*?) \| (.*?) \|$", line)
        if not m:
            continue
        expected = [p.strip() for p in m.group(4).split("；") if p.strip()]
        rows.append({"n": int(m.group(1)), "query": m.group(2).strip(),
                     "type": m.group(3), "expected": expected,
                     "note": m.group(5).strip()})
    return rows


def run_query(q: str) -> dict:
    """对单条查询跑三通道，返回排序后的 path 列表、通道可用性与向量最高相似度。"""
    grep_hits = sr._grep(q)
    vec_hits = sr._vector_search(q)          # None = 不可用/失败（降级）
    fused = sr._fuse(grep_hits, vec_hits)
    max_sim = max((v["similarity"] for v in vec_hits), default=None) if vec_hits else None
    return {
        "grep": list(grep_hits),
        "vector": [v["path"] for v in vec_hits] if vec_hits is not None else None,
        "vec_max_sim": max_sim,
        "fused": [e["path"] for e in fused],
    }


def mrr_at_k(rank: list[str], expected: list[str], k: int = K) -> float:
    for i, p in enumerate(rank[:k], 1):
        if p in expected:
            return 1.0 / i
    return 0.0


def recall_at_k(rank: list[str], expected: list[str], k: int = K) -> float:
    if not expected:
        return 0.0
    return len(set(rank[:k]) & set(expected)) / len(expected)


def avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main(detail: bool = False, check: bool = False) -> None:
    rows = parse_gold()
    if not rows:
        print(f"黄金集解析失败或无数据：{GOLD_PATH}")
        sys.exit(1)

    vec_ok = embedding.is_available()
    print("=== 检索评测 | 黄金集 ===")
    print(f"来源: {GOLD_PATH.name}")
    print(f"规模: {len(rows)} 条（精确 {sum(1 for r in rows if r['type']=='精确')} / "
          f"语义 {sum(1 for r in rows if r['type']=='语义')} / "
          f"缺口 {sum(1 for r in rows if r['type']=='缺口')}）")
    print(f"向量通道: {'可用（DASHSCOPE_API_KEY 已配置）' if vec_ok else 'N/A（未配置 key，融合自动 grep-only）'}")
    print("注：评测不经 /search 端点，不写 search_logs（避免污染知识缺口看板）")
    print()

    scored = [r for r in rows if r["type"] in ("精确", "语义")]
    gaps = [r for r in rows if r["type"] == "缺口"]
    results = {r["n"]: run_query(r["query"]) for r in rows}

    # ---- 逐查询明细 ----
    if detail:
        print("=== 逐查询明细（fused 排序前 K）===")
        for r in rows:
            rank = results[r["n"]]["fused"] or []
            marks = "".join(f"{i}.{p.split('/')[-1]}{'*' if p in r['expected'] else ''} "
                            for i, p in enumerate(rank[:K], 1))
            print(f"#{r['n']:02d} [{r['type']}] {r['query']}")
            print(f"     预期: {r['expected'] or '（零命中）'} | fused: {marks or '（空）'}")
        print()

    # ---- 指标汇总 ----
    print("=== MRR@10 / Recall@10（精确+语义）===")
    header = f"{'通道':<8}{'MRR@10':>9}{'Recall@10':>11}{'样本':>6}"
    print(header)
    print("-" * len(header))
    for mode in ("grep", "vector", "fused"):
        ranks = [results[r["n"]][mode] for r in scored]
        if all(x is None for x in ranks):
            print(f"{mode:<8}{'N/A':>9}{'N/A':>11}{len(scored):>6}")
            continue
        valid = [x for x in ranks if x is not None]
        mmr = avg([mrr_at_k(x, r["expected"]) for x, r in zip(valid, [x for x in scored if results[x['n']][mode] is not None])])
        rec = avg([recall_at_k(x, r["expected"]) for x, r in zip(valid, [x for x in scored if results[x['n']][mode] is not None])])
        print(f"{mode:<8}{mmr:>9.3f}{rec:>11.3f}{len(valid):>6}")

    # ---- 分类型（语义项单独看：向量通道是否补上 grep 盲区）----
    print()
    print("=== 语义类逐一命中（预期应中 / 实际中）===")
    sem_rows = [r for r in scored if r["type"] == "语义"]
    for r in sem_rows:
        res = results[r["n"]]
        exp = set(r["expected"])
        hit = lambda mode: (set(res[mode][:K]) & exp) if res[mode] is not None else None
        g, v, f = hit("grep"), hit("vector"), hit("fused")
        gs = f"{len(g or set())}/{len(exp)}" if g is not None else "N/A"
        vs = f"{len(v or set())}/{len(exp)}" if v is not None else "N/A"
        fs = f"{len(f or set())}/{len(exp)}" if f is not None else "N/A"
        print(f"#{r['n']:02d} {r['query']:<24} grep={gs} vector={vs} fused={fs}")

    # ---- 缺口检出力（与线上判据一致：grep 零命中 且 向量最高相似度 < τ；向量不可用退化为 grep 零命中）----
    print()
    print(f"=== 缺口检出力（判据：grep 零命中 且 max_sim < τ={sr.GAP_SIM_THRESHOLD}）===")
    correct = 0
    for r in gaps:
        res = results[r["n"]]
        pred = (len(res["grep"]) == 0) and (res["vec_max_sim"] is None
                                            or res["vec_max_sim"] < sr.GAP_SIM_THRESHOLD)
        if pred:
            correct += 1
        sim_txt = f"max_sim={res['vec_max_sim']:.3f}" if res["vec_max_sim"] is not None else "vector N/A"
        mark = "判为缺口[OK]" if pred else "[MISS] 漏判"
        print(f"#{r['n']:02d} {r['query']:<22} {sim_txt} grep={len(res['grep'])} → {mark}")
    print(f"缺口识别正确率: {correct}/{len(gaps)}"
          + ("（向量通道不可用时退化为旧语义）" if not vec_ok else ""))

    # ---- 简历可引用句式 ----
    print()
    print("—— 简历/面试可引用句式 ——")
    g_ranks = [results[r["n"]]["grep"] for r in scored]
    f_ranks = [results[r["n"]]["fused"] for r in scored]
    g_mrr = avg([mrr_at_k(x, r["expected"]) for x, r in zip(g_ranks, scored)])
    f_mrr = avg([mrr_at_k(x, r["expected"]) for x, r in zip(f_ranks, scored)])
    g_rec = avg([recall_at_k(x, r["expected"]) for x, r in zip(g_ranks, scored)])
    f_rec = avg([recall_at_k(x, r["expected"]) for x, r in zip(f_ranks, scored)])
    if vec_ok:
        v_ranks = [results[r["n"]]["vector"] for r in scored]
        v_mrr = avg([mrr_at_k(x, r["expected"]) for x, r in zip(v_ranks, scored)])
        v_rec = avg([recall_at_k(x, r["expected"]) for x, r in zip(v_ranks, scored)])
        print(f"在 {len(scored)} 条人工标注黄金查询上：融合 MRR@10={f_mrr:.2f}（grep {g_mrr:.2f} / 向量 {v_mrr:.2f}）、"
              f"Recall@10={f_rec:.2f}（grep {g_rec:.2f} / 向量 {v_rec:.2f}）；"
              f"语义改写查询中向量通道命中 {sum(1 for r in sem_rows if results[r['n']]['vector'] and set(results[r['n']]['vector'][:K]) & set(r['expected']))}/{len(sem_rows)}"
              f"（grep 仅 {sum(1 for r in sem_rows if set(results[r['n']]['grep'][:K]) & set(r['expected']))}/{len(sem_rows)}）。")
    else:
        print(f"在 {len(scored)} 条人工标注黄金查询上（向量通道未配置，融合=降级 grep-only）："
              f"fused MRR@10={f_mrr:.2f}、Recall@10={f_rec:.2f}。"
              f"配置 DASHSCOPE_API_KEY 后重跑获得向量与融合的独立数字。")

    # ---- 检索门禁（--check，CI 用）：断言失败 → 非零退出 ----
    if check:
        print()
        print("=== 检索门禁（--check）===")
        fails = []
        if correct != len(gaps):
            fails.append(f"缺口判据 {correct}/{len(gaps)}（应 100%）")
        exact_rows = [r for r in scored if r["type"] == "精确"]
        exact_f_rec = avg([recall_at_k(results[r["n"]]["fused"], r["expected"])
                           for r in exact_rows])
        if exact_f_rec < 0.5:
            fails.append(f"精确组 fused Recall@10 = {exact_f_rec:.2f}（基线 0.50，防 grep 精确匹配回归）")
        sem_fused = sum(1 for r in sem_rows
                        if set(results[r["n"]]["fused"][:K]) & set(r["expected"]))
        if vec_ok:
            if f_mrr < 0.95:
                fails.append(f"融合 MRR@10 = {f_mrr:.3f}（基线 0.95）")
            if sem_fused < 4:
                fails.append(f"语义组 fused 命中 {sem_fused}/{len(sem_rows)}（基线 4）")
        if fails:
            print("FAIL:")
            for f in fails:
                print(f" - {f}")
            sys.exit(1)
        mode_txt = (f"完整（向量通道，fused MRR@10={f_mrr:.2f}、语义命中 {sem_fused}/{len(sem_rows)}）"
                    if vec_ok else "grep 降级模式（未配置 key，向量侧由 tests/test_search_vector.py 的 mock 用例覆盖）")
        print(f"PASS: 缺口判据 {correct}/{len(gaps)}、精确组 Recall@10={exact_f_rec:.2f}、{mode_txt}")


if __name__ == "__main__":
    import db
    try:
        main(detail="--detail" in sys.argv, check="--check" in sys.argv)
    finally:
        db.close_pool()  # 优雅退出，避免 psycopg 连接池后台线程悬挂（conftest 同款纪律）