"""SP4 融合权重 + 缺口阈值标定：在黄金集上网格搜索，输出可落地的默认参数。

用法：
    python tools/tune_search.py          # 完整标定（需 PG + DASHSCOPE_API_KEY）
    python tools/tune_search.py --keep   # 附带输出各查询 vector 最高相似度明细

设计要点：
1. 权重扫描：_fuse(grep, vec, w_grep, w_vec) 已支持权重参数，多组合只算一次通道结果。
2. 缺口阈值 τ：收集"缺口样本"（grep 零命中的查询）与"命中样本"的 vector 最高相似度，
   建议取两者的分隔值——gap = grep 零命中 且 max_sim < τ。
3. 方法论声明：14 条黄金集上标定会过拟合，结果作默认参数并保留可调；
   扩集后应切 train/test 再标定（当前规模以工程可用为先）。
4. 不写 search_logs（同 eval_search 纪律）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SHARED = ROOT / "streamlit_app"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
_TOOLS = ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import eval_search as ev
from api.routers import search_router as sr

# 权重网格（w_grep × w_vec）
WG = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
WV = [0.2, 0.3, 0.4, 0.5, 0.6]


def channel_data(rows: list[dict]) -> dict:
    """每查询只跑一次通道，返回 {n: (grep_hits, vec_hits)}。"""
    out = {}
    for r in rows:
        gh = sr._grep(r["query"])
        vh = sr._vector_search(r["query"])
        out[r["n"]] = (gh, vh)
    return out


def fused_rank(gh: list[str], vh: list[dict] | None, wg: float, wv: float) -> list[str]:
    fused = sr._fuse(gh, vh, wg, wv)
    return [e["path"] for e in fused]


def main() -> None:
    rows = ev.parse_gold()
    scored = [r for r in rows if r["type"] in ("精确", "语义")]
    gaps = [r for r in rows if r["type"] == "缺口"]
    data = channel_data(rows)
    vec_ok = data[scored[0]["n"]][1] is not None

    print("=== 融合权重网格（MRR@10，精确+语义）===")
    print("w_vec \\ w_grep" + "".join(f"{wg:>8.1f}" for wg in WG))
    best = None
    grid = []
    for wv in WV:
        line = f"{wv:<14.1f}"
        for wg in WG:
            mmrs = []
            recs = []
            for r in scored:
                gh, vh = data[r["n"]]
                rank = fused_rank(gh, vh, wg, wv)
                mmrs.append(ev.mrr_at_k(rank, r["expected"]))
                recs.append(ev.recall_at_k(rank, r["expected"]))
            m, rc = sum(mmrs) / len(mmrs), sum(recs) / len(recs)
            grid.append((m, rc, wg, wv))
            line += f"{m:>8.2f}"
        print(line)
    best = max(grid, key=lambda x: (x[0], x[1]))
    print(f"\n最优: w_grep={best[2]} w_vec={best[3]}  MRR@10={best[0]:.3f} Recall@10={best[1]:.3f}")
    cur = next(x for x in grid if x[2] == 0.5 and x[3] == 0.3)
    print(f"现状: w_grep=0.5 w_vec=0.3                  MRR@10={cur[0]:.3f} Recall@10={cur[1]:.3f}")

    # ---- 缺口阈值 τ ----
    print()
    print("=== 缺口阈值 τ 标定（vector 最高相似度）===")
    gap_sims, hit_sims = [], []
    for r in rows:
        gh, vh = data[r["n"]]
        if vh is None:
            continue
        max_sim = max(v["similarity"] for v in vh) if vh else None
        if r["type"] == "缺口":
            gap_sims.append(max_sim)
        elif r["type"] in ("精确", "语义"):
            hit_sims.append(max_sim)
    if gap_sims:
        print(f"缺口样本 max_sim: {['%.3f' % s for s in sorted(gap_sims)]}")
    if hit_sims:
        print(f"命中样本 max_sim: {['%.3f' % s for s in sorted(hit_sims)]}")
    if gap_sims and hit_sims:
        lo = max(gap_sims)
        hi = min(hit_sims)
        if lo < hi:
            tau = (lo + hi) / 2
            print(f"分隔区间: ({lo:.3f}, {hi:.3f}) → 建议 τ={tau:.3f}")
        else:
            print(f"⚠️ 区间重叠（缺口最高 {lo:.3f} ≥ 命中最低 {hi:.3f}）——"
                  f"单阈值无法完全分隔，需结合 grep 命中条件或扩样本")


if __name__ == "__main__":
    import db
    try:
        main()
    finally:
        db.close_pool()