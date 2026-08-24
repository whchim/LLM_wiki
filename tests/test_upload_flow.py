import hashlib
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import db  # conftest 已重载并重置测试库 schema


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
    """隔离环境：KB_ROOT 指向临时目录，重载 upload/ops 使模块级 KB_ROOT 生效。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    import importlib
    import ops
    importlib.reload(ops)      # write_trigger 读 ops.KB_ROOT，需随 env 重载
    import upload
    return importlib.reload(upload)


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
    # 任务行：pending + 指纹与落盘内容一致（顺序无关，集合比较）
    tasks = db.list_recent_compile_tasks(50)
    assert {t["raw_path"] for t in tasks} == {"RAW/会议/概念说明.md", "RAW/会议/备忘.txt"}
    assert all(t["status"] == "pending" for t in tasks)
    for t in tasks:
        assert t["error_msg"] is None
        expected = hashlib.sha256((tmp_path / t["raw_path"]).read_bytes()).hexdigest()
        assert t["fingerprint"] == expected
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
    tasks = db.list_recent_compile_tasks(50)
    assert len(tasks) == 1 and tasks[0]["status"] == "failed"
    assert "触发文件写入失败" in (tasks[0]["error_msg"] or "")
    assert not list((tmp_path / "_triggers").glob("compile_*.md"))
    # RAW 文件保留（供重试按钮重新触发）
    assert (tmp_path / "RAW" / "项目" / "a.md").exists()
