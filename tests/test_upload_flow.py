import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))


class FakeUploadedFile:
    """最小模拟 st.file_uploader 返回的 UploadedFile（.name/.size/.getbuffer()）。"""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    def getbuffer(self) -> bytes:
        return self._data


def _setup(tmp_path, monkeypatch):
    """隔离环境：KB_ROOT/DB_PATH 指向临时目录，重载 db/ops/upload 使模块级路径生效。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "meta.db"))
    sqlite3.connect(tmp_path / "meta.db").executescript(
        (ROOT / "schema.sql").read_text(encoding="utf-8"))
    import importlib
    import db, ops
    db = importlib.reload(db)
    ops = importlib.reload(ops)
    import upload
    upload = importlib.reload(upload)
    return upload


def test_process_upload_happy_path(tmp_path, monkeypatch):
    """合法文件落盘+入库+触发；非法/超大文件报错且不产生任务与触发副作用。"""
    upload = _setup(tmp_path, monkeypatch)
    files = [
        FakeUploadedFile("概念说明.md", "# 概念\n".encode("utf-8")),
        FakeUploadedFile("备忘.txt", b"memo"),
        FakeUploadedFile("photo.jpg", b"not allowed"),           # 白名单外
        FakeUploadedFile("big.pdf", b"x" * (11 * 1024 * 1024)),  # 超 10MB
    ]
    ok, errs = upload._process_upload(files, "会议")
    assert ok == 2
    assert len(errs) == 2
    assert any("photo.jpg" in e and "不支持" in e for e in errs)
    assert any("big.pdf" in e and "10MB" in e for e in errs)
    # RAW 落盘：仅合法文件
    assert (tmp_path / "RAW" / "会议" / "概念说明.md").exists()
    assert (tmp_path / "RAW" / "会议" / "备忘.txt").exists()
    assert not (tmp_path / "RAW" / "会议" / "photo.jpg").exists()
    assert not (tmp_path / "RAW" / "会议" / "big.pdf").exists()
    # 任务行：pending + 指纹与落盘内容一致
    with sqlite3.connect(tmp_path / "meta.db") as conn:
        rows = conn.execute(
            "SELECT raw_path, status, fingerprint FROM compile_tasks ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["RAW/会议/概念说明.md", "RAW/会议/备忘.txt"]
        assert all(r[1] == "pending" for r in rows)
        for raw_path, _, fp in rows:
            assert fp == hashlib.sha256((tmp_path / raw_path).read_bytes()).hexdigest()
    # 触发文件：恰好一个，含本批全部合法路径
    triggers = list((tmp_path / "_triggers").glob("compile_*.md"))
    assert len(triggers) == 1
    text = triggers[0].read_text(encoding="utf-8")
    assert "RAW/会议/概念说明.md" in text and "RAW/会议/备忘.txt" in text
    assert "photo.jpg" not in text


def test_process_upload_trigger_failure_compensates(tmp_path, monkeypatch):
    """触发文件写入失败：已插任务成对补偿为 failed，不残留无触发的 pending。"""
    upload = _setup(tmp_path, monkeypatch)

    def boom(kind, paths, source):
        raise OSError("模拟 _triggers 目录不可写")

    monkeypatch.setattr(upload, "write_trigger", boom)
    files = [FakeUploadedFile("a.md", b"# a\n")]
    ok, errs = upload._process_upload(files, "项目")
    assert ok == 0
    assert len(errs) == 1
    # PRD 9.3 三要素：问题描述/可能原因/建议操作 + 受影响文件
    assert "触发文件写入失败" in errs[0] and "可能原因" in errs[0] and "建议" in errs[0]
    assert "RAW/项目/a.md" in errs[0]
    # 一致性：任务已补偿置 failed，无 pending 残留；无触发文件
    with sqlite3.connect(tmp_path / "meta.db") as conn:
        row = conn.execute("SELECT status, error_msg FROM compile_tasks").fetchone()
        assert row[0] == "failed"
        assert "触发文件写入失败" in row[1]
    assert not list((tmp_path / "_triggers").glob("compile_*.md"))
    # RAW 文件保留（供重试按钮重新触发）
    assert (tmp_path / "RAW" / "项目" / "a.md").exists()
