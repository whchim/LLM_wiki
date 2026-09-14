"""CI 检索回归门禁测试（**合成语料**，离线，零 API key，零外网）。

背景：真实黄金集（`docs/VAL-03`）基于真实业务内容，是本地/面试资产、不进公开仓库，
因此原来"检索门禁"只在本地跑得起来。本测试用可公开的合成集把门禁搬进 CI：

- 语料：`tests/fixtures/retrieval_kb/`（6 个 active 合成页面 + 1 个 draft 页面）
- 黄金集：`tests/fixtures/retrieval_gold.md`（精确 6 / 语义 2 / 缺口 3）

覆盖四件事：① 门禁在合成集上确实能通过（离线、grep 降级模式）；
② 门禁**真的会拦**（标注写错时返回失败）；③ draft 页面绝不参与检索；
④ 黄金集标注与语料互相锁定（预期路径存在、精确项 grep 必中、缺口项零命中）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("tools", "core"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import eval_search  # noqa: E402
from api.routers import search_router as sr  # noqa: E402

KB = ROOT / "tests" / "fixtures" / "retrieval_kb"
GOLD = ROOT / "tests" / "fixtures" / "retrieval_gold.md"

pytestmark = pytest.mark.no_db

# 故意写错的黄金集：精确项指向不存在的页面（门禁必须拦下）
BROKEN_GOLD = """
| # | 查询 | 类型 | 预期命中（KB 相对路径） | 设计意图 |
|---|------|------|--------------------------|----------|
| 1 | 供应商准入评估 | 精确 | NEXUS/概念/并不存在的页面.md | 标注错误：门禁应失败 |
| 2 | 员工年会的抽奖奖品清单 | 缺口 | （零命中） | 缺口样本 |
"""


@pytest.fixture()
def fixture_kb(monkeypatch) -> Path:
    """把检索根指向合成语料（search_router._kb_root() 每次调用动态读 env）。"""
    monkeypatch.setenv("KB_ROOT", str(KB))
    return KB


def test_synthetic_gate_passes_offline(fixture_kb):
    """CI 门禁路径：合成语料 + 关闭向量通道 → 门禁返回 0（不依赖 PG / API key）。"""
    assert eval_search.main(check=True, kb=str(KB), gold=str(GOLD), no_vector=True) == 0


def test_synthetic_gate_blocks_wrong_expectation(fixture_kb, tmp_path):
    """门禁本身要能被信任：标注与语料不符时必须失败（否则门禁只是装饰）。"""
    broken = tmp_path / "broken_gold.md"
    broken.write_text(BROKEN_GOLD, encoding="utf-8")
    assert eval_search.main(check=True, kb=str(KB), gold=str(broken), no_vector=True) == 1


def test_synthetic_gate_reports_missing_gold(fixture_kb, tmp_path):
    """黄金集缺失/解析不出内容 → 失败而非静默通过。"""
    empty = tmp_path / "empty_gold.md"
    empty.write_text("# 空黄金集\n", encoding="utf-8")
    assert eval_search.main(check=True, kb=str(KB), gold=str(empty), no_vector=True) == 1


def test_draft_pages_are_not_searchable(fixture_kb):
    """未发布（status: draft）页面绝不参与检索——该词只存在于 draft 页。"""
    draft = KB / "NEXUS" / "概念" / "绩效改进计划.md"
    assert "绩效改进计划" in draft.read_text(encoding="utf-8")   # 词确实在 draft 页里
    assert sr._grep("绩效改进计划") == []                          # 但检索不到


def test_empty_corpus_fails_fast_with_actionable_hint(tmp_path, monkeypatch, capsys):
    """语料为空时不能说"检索退化"——公开仓库的 vault 是空骨架，必须给出可执行的下一步。"""
    empty_kb = tmp_path / "empty_kb"
    (empty_kb / "NEXUS").mkdir(parents=True)
    monkeypatch.setenv("KB_ROOT", str(empty_kb))
    assert eval_search.main(check=True, kb=str(empty_kb), gold=str(GOLD), no_vector=True) == 1
    out = capsys.readouterr().out
    assert "语料为空" in out and "retrieval_kb" in out


def test_no_vector_mode_keeps_grep_only_order(fixture_kb):
    """`--no-vector` 下融合结果必须等于 grep 结果（合成语料不对应 PG 向量索引）。"""
    result = eval_search.run_query("供应商准入评估", allow_vector=False)
    assert result["vector"] is None and result["vec_max_sim"] is None
    assert set(result["fused"]) == set(result["grep"])


def test_gold_and_corpus_are_mutually_locked(fixture_kb):
    """黄金集标注与合成语料互相锁定：路径存在、精确项 grep 必中、缺口项零命中。"""
    rows = eval_search.parse_gold(GOLD)
    assert rows, "合成黄金集解析为空"
    assert {r["type"] for r in rows} == {"精确", "语义", "缺口"}
    for row in rows:
        for path in row["expected"]:
            assert (KB / path).exists(), f"#{row['n']} 预期路径不存在：{path}"
        if row["type"] == "精确":
            hits = set(sr._grep(row["query"]))
            assert set(row["expected"]) <= hits, f"#{row['n']} 精确项未命中：{sorted(hits)}"
        if row["type"] == "缺口":
            assert row["expected"] == [], f"#{row['n']} 缺口项不应标注预期命中"
            assert sr._grep(row["query"]) == [], f"#{row['n']} 缺口项被检索命中"
