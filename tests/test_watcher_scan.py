"""SP3 增量编译测试：RAW 增量判定 / 纸条合并 / 忽略规则 / 断点续跑去重。"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "streamlit_app"))

pytestmark = pytest.mark.usefixtures("_env")


@pytest.fixture()
def watcher():
    import importlib
    import trigger_watcher
    return importlib.reload(trigger_watcher)


def _raw_file(tmp_path: Path, rel: str, content: str = "# 测试\n") -> Path:
    p = tmp_path / "vault" / "RAW" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_new_raw_file_detected(watcher, tmp_path):
    """新 RAW 文件（compile_tasks 无记录）被判定为增量。"""
    _raw_file(tmp_path, "项目/new_doc.md")
    assert watcher.scan_raw_increment() == ["RAW/项目/new_doc.md"]


def test_done_file_not_rescanned(watcher, tmp_path):
    """已 done 编译的文件不再出现在增量里。"""
    import db
    _raw_file(tmp_path, "项目/done_doc.md")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status) "
            "VALUES ('RAW/项目/done_doc.md','abc','done')")
    assert watcher.scan_raw_increment() == []


def test_failed_file_not_rescanned(watcher, tmp_path):
    """failed 不算增量（防纸条风暴；重试走 UI/指纹变化）。"""
    import db
    _raw_file(tmp_path, "项目/fail_doc.md")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status) "
            "VALUES ('RAW/项目/fail_doc.md','abc','failed')")
    assert watcher.scan_raw_increment() == []


def test_cached_file_not_rescanned(watcher, tmp_path):
    import db
    _raw_file(tmp_path, "项目/cached_doc.md")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO compile_tasks (raw_path, fingerprint, status) "
            "VALUES ('RAW/项目/cached_doc.md','abc','cached')")
    assert watcher.scan_raw_increment() == []


def test_multiple_new_files_merged(watcher, tmp_path):
    """多个新文件都进增量（消费侧合并为一张纸条一次会话编译）。"""
    _raw_file(tmp_path, "项目/a.md")
    _raw_file(tmp_path, "会议/b.md")
    _raw_file(tmp_path, "个人_notes/c.txt")
    assert sorted(watcher.scan_raw_increment()) == [
        "RAW/个人_notes/c.txt", "RAW/会议/b.md", "RAW/项目/a.md"]


def test_non_md_txt_ignored(watcher, tmp_path):
    """非 .md/.txt（如 .pdf 原文件、.tmp 临时文件）不进增量。"""
    _raw_file(tmp_path, "项目/photo.jpg")
    _raw_file(tmp_path, "项目/notes.docx")
    assert watcher.scan_raw_increment() == []


def test_write_raw_paper_creates_trigger(watcher, tmp_path):
    """增量文件生成一张 compile 纸条，含全部路径，source=watcher_scan。"""
    import re
    paths = ["RAW/项目/a.md", "RAW/会议/b.md"]
    _raw_file(tmp_path, "项目/a.md")
    _raw_file(tmp_path, "会议/b.md")
    paper = watcher.write_raw_paper(paths)
    assert paper is not None and paper.exists()
    text = paper.read_text(encoding="utf-8")
    assert "source: watcher_scan" in text
    assert "RAW/项目/a.md" in text and "RAW/会议/b.md" in text
    # 原子写无残留 tmp
    assert not list(paper.parent.glob(".tmp_*"))


def test_db_unreachable_graceful(watcher, tmp_path, monkeypatch):
    """数据库不可达时增量扫描优雅降级（返回空 + 不崩溃）。"""
    import db
    def boom():
        raise RuntimeError("PG down")
    monkeypatch.setattr(db, "get_conn", boom)
    assert watcher.scan_raw_increment() == []
