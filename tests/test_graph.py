"""知识图谱测试（`core/graph.py`）：节点/边抽取、解析顺序、待建页面、租户隔离、安全阀。

图谱的价值在两件事上：**关系可见**（哪些概念互链）与**缺口可见**（被引用却没建——红链）。
后者是本项目"自增长"的一条新燃料：比"搜不到"更精确地指出该补哪一页。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import db  # noqa: E402
import graph  # noqa: E402
import paths  # noqa: E402


def _write(root: Path, rel: str, *, title: str, body: str, related=None, status="active",
           type_="concept", department="共享层") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"type: {type_}", f"title: {title}", f"status: {status}",
             f"department: {department}", "version: V1.0", "source: 测试"]
    if related is not None:
        lines.append("related_to:")
        lines += [f"- {r}" for r in related]
    lines += ["---", "", body, ""]
    target.write_text("\n".join(lines), encoding="utf-8")


def _fixture(tmp_path, monkeypatch, tenant=None) -> Path:
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree(tenant) if tenant else paths.ensure_tenant_tree("default")
    _write(root, "NEXUS/概念/新质生产力.md", title="新质生产力",
           related=["中国式现代化", "高质量发展"],
           body="## 定义\n\n见 [[科技创新]] 与 [[高质量发展|高质量]]。\n\n"
                "另见 [文件资产数字化](NEXUS/概念/文件资产数字化.md) 与 "
                "[外部链接](https://example.com/a.md)。")
    _write(root, "NEXUS/概念/文件资产数字化.md", title="文件资产数字化",
           body="## 定义\n\n数字化。\n\n## 关联知识\n- [[新质生产力]]\n")
    _write(root, "NEXUS/资源/某政策文件.md", title="某政策文件", type_="resource",
           body="正文提到 [[新质生产力]] 与 [文件资产数字化](NEXUS/概念/文件资产数字化.md)。")
    return root


def _node(g, path):
    return next((n for n in g["nodes"] if n["id"] == path), None)


def test_nodes_carry_metadata_and_degree(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    g = graph.build_graph()
    n = _node(g, "NEXUS/概念/新质生产力.md")
    assert n["title"] == "新质生产力" and n["type"] == "concept"
    assert n["department"] == "共享层" and n["status"] == "active"
    assert n["degree"] >= 4          # related_to 两条 + wikilink 两条 + markdown 链接一条
    assert g["stats"]["nodes"] == 3


def test_edges_from_related_to_wikilink_and_markdown_link(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    g = graph.build_graph()
    edges = {(e["source"], e["target"], e["kind"]) for e in g["edges"] if e["resolved"]}
    assert ("NEXUS/概念/新质生产力.md", "NEXUS/概念/文件资产数字化.md", "wikilink") not in edges
    assert ("NEXUS/概念/新质生产力.md", "NEXUS/概念/文件资产数字化.md", "link") in edges
    assert ("NEXUS/概念/文件资产数字化.md", "NEXUS/概念/新质生产力.md", "wikilink") in edges
    assert ("NEXUS/资源/某政策文件.md", "NEXUS/概念/新质生产力.md", "wikilink") in edges


def test_unresolved_references_become_missing_pages(tmp_path, monkeypatch):
    """被引用但没建的条目 → `missing`（按引用次数排序，带来源），不是静默丢弃。"""
    _fixture(tmp_path, monkeypatch)
    g = graph.build_graph()
    titles = {m["title"]: m for m in g["missing"]}
    assert "中国式现代化" in titles and "高质量发展" in titles and "科技创新" in titles
    assert titles["高质量发展"]["count"] == 2          # related_to + wikilink 各一次
    assert "NEXUS/概念/新质生产力.md" in titles["高质量发展"]["sources"]
    assert all(not e["resolved"] for e in g["edges"] if e["target"].startswith("missing:"))
    assert g["stats"]["missing"] == len(g["missing"]) == 3


def test_external_links_ignored_and_self_links_dropped(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    g = graph.build_graph()
    assert not any("example.com" in (e["target"] or "") for e in g["edges"])
    assert not any(e["source"] == e["target"] for e in g["edges"])


def test_duplicate_edges_deduped(tmp_path, monkeypatch):
    """同一对条目、同一关系类型重复出现只留一条（否则度数被灌水）。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    _write(root, "NEXUS/概念/A.md", title="A", body="[[B]] [[B]] [[B|B]]")
    _write(root, "NEXUS/概念/B.md", title="B", body="B 的正文。")
    g = graph.build_graph()
    assert [e for e in g["edges"] if e["source"].endswith("A.md")] == [
        {"source": "NEXUS/概念/A.md", "target": "NEXUS/概念/B.md",
         "kind": "wikilink", "resolved": True}]


def test_orphans_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    _write(root, "NEXUS/概念/孤岛.md", title="孤岛", body="没有任何链接。")
    g = graph.build_graph()
    assert g["stats"]["orphans"] == 1 and g["nodes"][0]["degree"] == 0


def test_pending_review_excluded_by_default(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    root = paths.kb_root()
    _write(root, "pending_review/待审概念.md", title="待审概念", status="pending",
           body="[[新质生产力]]")
    assert graph.build_graph()["stats"]["nodes"] == 3
    with_pending = graph.build_graph(include_pending=True)
    assert with_pending["stats"]["nodes"] == 4
    assert _node(with_pending, "pending_review/待审概念.md")["status"] == "pending"


def test_graph_is_tenant_scoped(tmp_path, monkeypatch):
    """L3.5：同名条目在两个租户里各成一张图，互不可见。"""
    _fixture(tmp_path, monkeypatch, tenant="tenant-a")
    root_b = paths.ensure_tenant_tree("tenant-b")
    _write(root_b, "NEXUS/概念/新质生产力.md", title="新质生产力", body="B 租户正文，只链 [[B专属]]。")

    token = db.bind_tenant("tenant-a")
    try:
        g_a = graph.build_graph()
    finally:
        db.reset_tenant(token)
    token = db.bind_tenant("tenant-b")
    try:
        g_b = graph.build_graph()
    finally:
        db.reset_tenant(token)

    assert g_a["stats"]["nodes"] == 3 and g_b["stats"]["nodes"] == 1
    assert {m["title"] for m in g_a["missing"]} == {"中国式现代化", "高质量发展", "科技创新"}
    assert [m["title"] for m in g_b["missing"]] == ["B专属"]


def test_neighbors_returns_one_hop(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    nb = graph.neighbors("NEXUS/概念/新质生产力.md")
    assert nb["found"] is True
    ids = {n["id"] for n in nb["neighbors"]}
    assert "NEXUS/概念/文件资产数字化.md" in ids and "NEXUS/资源/某政策文件.md" in ids
    assert {m["title"] for m in nb["missing"]} == {"中国式现代化", "高质量发展", "科技创新"}
    assert graph.neighbors("NEXUS/概念/不存在.md")["found"] is False


def test_max_nodes_is_a_loud_safety_valve(tmp_path, monkeypatch):
    """超限截断要**如实标注**（truncated=True），不静默丢数据。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    for i in range(6):
        _write(root, f"NEXUS/概念/条目{i}.md", title=f"条目{i}", body="[[新质生产力]]")
    g = graph.build_graph(max_nodes=4)
    assert len(g["nodes"]) == 4 and g["stats"]["truncated"] is True
    assert graph.build_graph(max_nodes=50)["stats"]["truncated"] is False


# ---------- 真机语料暴露的三个解析问题（都已修，锁住）----------

def test_reserved_files_are_first_class_nodes(tmp_path, monkeypatch):
    """**保留文件必须出现在图谱里**（PRD WIKI-00 §Reserved Files：index.md 是渐进式目录、
    log.md 是操作审计日志、SCHEMA.md 是知识库规范）。

    早先版本把它们当"非条目"排除了：图上"索引与日志不存在"，与编译范式理念不符
    （用户实测反馈）。正确做法是**保留 + 分类**（`kind=index/log/schema`）交给前端着色/过滤——
    索引因列出全部条目而度数高，这是事实，不该用隐藏来抹平。
    """
    _fixture(tmp_path, monkeypatch)
    root = paths.kb_root()
    (root / "NEXUS").mkdir(parents=True, exist_ok=True)
    (root / "NEXUS/index.md").write_text(
        "# 知识库索引\n\n## 概念\n- [[新质生产力]]\n- [[概念-文件资产数字化]]\n", encoding="utf-8")
    (root / "NEXUS/log.md").write_text(
        "# 编译日志\n\n- 2026-09-15 12:03 · 编译 · RAW/x.md → NEXUS/资源/x.md\n", encoding="utf-8")
    (root / "SCHEMA.md").write_text("# 知识库规范\n\n标签体系见 [[新质生产力]]。\n", encoding="utf-8")

    g = graph.build_graph()
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["NEXUS/index.md"]["kind"] == "index" and by_id["NEXUS/index.md"]["is_meta"]
    assert by_id["NEXUS/log.md"]["kind"] == "log"
    assert by_id["SCHEMA.md"]["kind"] == "schema"
    # 保留文件用中文显示名（项目约定），并带自己的类型/状态（前端过滤与着色用）
    assert by_id["NEXUS/index.md"]["title"] == "知识库索引"
    assert by_id["NEXUS/log.md"]["title"] == "编译日志" and by_id["NEXUS/log.md"]["type"] == "log"
    assert by_id["SCHEMA.md"]["status"] == "meta"
    assert g["stats"]["by_kind"]["concept"] == 2 and g["stats"]["by_kind"]["resource"] == 1
    # 索引写的是带类型前缀的 wikilink：必须解析到真实条目，**不产生假缺口**
    assert ("NEXUS/index.md", "NEXUS/概念/文件资产数字化.md", "wikilink") in {
        (e["source"], e["target"], e["kind"]) for e in g["edges"] if e["resolved"]}
    assert {m["title"] for m in g["missing"]} == {"中国式现代化", "高质量发展", "科技创新"}

    # 也可以只要知识条目（前端过滤开关的语义）
    only_entries = graph.build_graph(include_meta=False)
    assert all(not n["is_meta"] for n in only_entries["nodes"])


def test_raw_scope_is_opt_in(tmp_path, monkeypatch):
    """RAW 原始语料默认不上图（会淹没知识层），显式打开时 `kind=raw` 以便另行着色。"""
    _fixture(tmp_path, monkeypatch)
    root = paths.kb_root()
    (root / "RAW/会议").mkdir(parents=True, exist_ok=True)
    (root / "RAW/会议/周会.md").write_text(
        "周会纪要：讨论了 [[新质生产力]] 与 [[还没建的概念]]。\n", encoding="utf-8")

    assert all(n["kind"] != "raw" for n in graph.build_graph()["nodes"])
    with_raw = graph.build_graph(include_raw=True)
    raw = next(n for n in with_raw["nodes"] if n["id"] == "RAW/会议/周会.md")
    assert raw["kind"] == "raw" and raw["scope"] == "raw" and raw["status"] == "raw"
    edges = {(e["source"], e["target"]) for e in with_raw["edges"] if e["resolved"]}
    assert ("RAW/会议/周会.md", "NEXUS/概念/新质生产力.md") in edges
    # RAW 里的悬空引用同样进"待建页面"
    assert "还没建的概念" in {m["title"] for m in with_raw["missing"]}


def test_pending_review_excluded_by_default(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    root = paths.kb_root()
    _write(root, "pending_review/待审概念.md", title="待审概念", status="pending",
           body="[[新质生产力]]")
    assert graph.build_graph()["stats"]["by_kind"].get("concept") == 2
    with_pending = graph.build_graph(include_pending=True)
    assert with_pending["stats"]["nodes"] == 4
    assert _node(with_pending, "pending_review/待审概念.md")["status"] == "pending"


def test_wikilink_with_type_prefix_resolves(tmp_path, monkeypatch):
    """索引工具会写 `[[概念-文件资产数字化]]`——带类型前缀也必须能解析到同一页。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    _write(root, "NEXUS/概念/文件资产数字化.md", title="文件资产数字化", body="正文")
    _write(root, "NEXUS/资源/文.md", title="文", body="见 [[概念-文件资产数字化]] 与 [[资源-不存在]]。")
    g = graph.build_graph()
    edges = [e for e in g["edges"] if e["source"] == "NEXUS/资源/文.md"]
    assert edges[0]["target"] == "NEXUS/概念/文件资产数字化.md" and edges[0]["resolved"] is True
    assert [m["title"] for m in g["missing"]] == ["资源-不存在"]


def test_relative_and_partial_path_links_resolve(tmp_path, monkeypatch):
    """Markdown 链接按**当前文件目录**解析；前缀不完整时用末段兜底。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    _write(root, "NEXUS/概念/文件资产数字化.md", title="文件资产数字化", body="正文")
    _write(root, "NEXUS/研究/报告.md", title="报告",
           body="见 [上跳](../概念/文件资产数字化.md) 与 [根相对](NEXUS/概念/文件资产数字化.md) "
                "与 [缺前缀](概念/文件资产数字化.md)。")
    g = graph.build_graph()
    resolved = {e["target"] for e in g["edges"] if e["resolved"]}
    assert resolved == {"NEXUS/概念/文件资产数字化.md"}
    assert g["missing"] == []
