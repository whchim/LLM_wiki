"""SP5 健康巡检测试：四类检测 / 周报落库 / 幂等。"""
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "streamlit_app"))

import health_check as hc  # noqa: E402

pytestmark = pytest.mark.usefixtures("_env")


def _concept(tmp_path: Path, name: str, body: str,
             updated: str | None = None, created: str = "2026-08-01",
             description: str = "") -> None:
    """在隔离 vault 里造一个概念页。"""
    p = tmp_path / "vault" / "NEXUS" / "概念"
    p.mkdir(parents=True, exist_ok=True)
    fm = ["---", "type: concept", f"title: {name}", "status: active"]
    if description:
        fm.append(f"description: {description}")
    if updated:
        fm.append(f"updated: \"{updated}\"")
    fm.append(f"created: \"{created}\"")
    fm += ["---", ""]
    (p / f"{name}.md").write_text("\n".join(fm) + body, encoding="utf-8")


def _kb(tmp_path: Path) -> Path:
    return tmp_path / "vault"


# ---------- 孤立节点 ----------

def test_orphan_zero_links(tmp_path):
    _concept(tmp_path, "孤岛概念", "## 定义\n\n没有任何链接。")
    result = hc.run(_kb(tmp_path))
    assert result["orphans"] == ["NEXUS/概念/孤岛概念.md"]


def test_not_orphan_with_outlink(tmp_path):
    _concept(tmp_path, "甲", "链接到 [[概念-乙]]")
    _concept(tmp_path, "乙", "正文")
    result = hc.run(_kb(tmp_path))
    assert result["orphans"] == []


def test_not_orphan_when_referenced(tmp_path):
    """被别的概念指向（有入链）不算孤立。"""
    _concept(tmp_path, "甲", "见 [[概念-孤指针]]")
    _concept(tmp_path, "孤指针", "正文")  # 自身无出链，但有入链
    result = hc.run(_kb(tmp_path))
    assert result["orphans"] == []


# ---------- 断链 ----------

def test_broken_link_detected(tmp_path):
    _concept(tmp_path, "甲", "指向不存在的 [[概念-幽灵]]")
    result = hc.run(_kb(tmp_path))
    assert result["broken"] == [
        {"from": "NEXUS/概念/甲.md", "link": "概念-幽灵"}]


def test_valid_link_not_broken(tmp_path):
    _concept(tmp_path, "甲", "见 [[概念-乙]]")
    _concept(tmp_path, "乙", "正文")
    assert hc.run(_kb(tmp_path))["broken"] == []


# ---------- 过期 ----------

def test_stale_over_180_days(tmp_path):
    old = (date.today() - timedelta(days=200)).isoformat()
    _concept(tmp_path, "老概念", "正文", updated=old)
    result = hc.run(_kb(tmp_path))
    assert len(result["stale"]) == 1
    assert result["stale"][0]["days"] == 200


def test_fresh_not_stale(tmp_path):
    _concept(tmp_path, "新概念", "正文", created="2026-08-01")
    assert hc.run(_kb(tmp_path))["stale"] == []


def test_stale_fallback_to_created(tmp_path):
    """updated 缺失时用 created 兜底（Demo 编译未写 updated 的已知缺口）。"""
    old = (date.today() - timedelta(days=365)).isoformat()
    _concept(tmp_path, "古董", "正文", created=old)
    result = hc.run(_kb(tmp_path))
    assert len(result["stale"]) == 1


# ---------- 相似候选 ----------

def test_similar_pair_detected(tmp_path):
    """同名 -2 后缀（同名冲突产物）与原概念高度相似 → 入候选。"""
    _concept(tmp_path, "示例监测产品", "正文",
             description="多灾种监测预警设备，四层架构")
    p = tmp_path / "vault" / "NEXUS" / "概念" / "示例监测产品-2.md"
    p.write_text("---\ntype: concept\ntitle: 示例监测产品-2\nstatus: active\n"
                 "description: 多灾种监测预警设备，四层架构\n---\n\n正文",
                 encoding="utf-8")
    result = hc.run(_kb(tmp_path))
    assert len(result["similar"]) == 1
    assert result["similar"][0]["ratio"] >= hc.SIMILAR_THRESHOLD


def test_dissimilar_not_candidate(tmp_path):
    _concept(tmp_path, "水文监测", "## 定义\n\n水情数据采集与分析。")
    _concept(tmp_path, "财务管理", "## 定义\n\n报销流程与预算管理。")
    assert hc.run(_kb(tmp_path))["similar"] == []


# ---------- 落库与周报 ----------

def test_report_saved_and_markdown_written(tmp_path):
    import db
    _concept(tmp_path, "甲", "指向 [[概念-幽灵]]")
    result = hc.run(_kb(tmp_path))
    hc.save_report(db, result)
    out = hc.write_markdown(_kb(tmp_path), result, None)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT report_date, orphan_count, broken_link_count, total_entries "
            "FROM health_reports ORDER BY id DESC LIMIT 1").fetchone()
    assert row[2] == 1          # 1 条断链
    assert row[3] == 1          # 1 个概念
    assert out.exists() and "健康周报" in out.read_text(encoding="utf-8")


def test_growth_rate_on_second_run(tmp_path):
    import db
    _concept(tmp_path, "甲", "正文")
    r1 = hc.run(_kb(tmp_path))
    hc.save_report(db, r1)
    _concept(tmp_path, "乙", "正文")   # 库 +1
    r2 = hc.run(_kb(tmp_path))
    hc.save_report(db, r2)
    with db.get_conn() as conn:
        growth = conn.execute(
            "SELECT growth_rate FROM health_reports ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert growth is not None and growth > 0


def test_archive_done(tmp_path):
    """done/ 归档清理：>90 天移动到 archive_<月>，新文件不动。"""
    import shutil
    sys.path.insert(0, str(ROOT / "tools"))
    done = tmp_path / "vault" / "_triggers" / "done"
    done.mkdir(parents=True, exist_ok=True)  # conftest 的 ensure_schema 已建好目录树
    old = done / "old_trigger.md"
    old.write_text("old", encoding="utf-8")
    new = done / "new_trigger.md"
    new.write_text("new", encoding="utf-8")
    two_hundred_days_ago = time.time() - 200 * 86400
    os.utime(old, (two_hundred_days_ago, two_hundred_days_ago))

    import importlib
    import archive_done
    importlib.reload(archive_done)
    moved = archive_done.archive(done)
    assert moved == 1
    assert not old.exists()
    archives = list(done.glob("archive_*/old_trigger.md"))
    assert len(archives) == 1
    assert new.exists()