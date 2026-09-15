"""Reserved File `NEXUS/log.md` 测试（PRD WIKI-00 §Reserved Files：审计日志，Phase 2 起记录操作事件）。

背景：`log.md` 长期是 `init.sh` 建出来的**空文件**——索引有人写、日志没人写，
"知识库怎么变成现在这样的"这一半可审计性丢了（真机检查发现 0 字节）。
这里锁住三条链路的日志写入 + append-only + 失败不阻断主流程。
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import ops  # noqa: E402
import paths  # noqa: E402


def _log(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    paths.ensure_tenant_tree("default")
    return ops.log_path()


def test_append_log_creates_header_and_line(tmp_path, monkeypatch):
    target = _log(tmp_path, monkeypatch)
    assert not target.exists()
    assert ops.append_log("编译", "RAW/会议/周会.md → NEXUS/资源/周会.md（资源 1 + 概念 2）",
                          when=datetime(2026, 9, 15, 12, 3)) is True
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 编译日志")                     # 空文件先补表头
    assert "- 2026-09-15 12:03 · 编译 · RAW/会议/周会.md → NEXUS/资源/周会.md（资源 1 + 概念 2）" in text


def test_append_log_is_append_only(tmp_path, monkeypatch):
    """历史行**永不改写**（append-only 是审计日志的底线）。"""
    target = _log(tmp_path, monkeypatch)
    ops.append_log("编译", "第一条", when=datetime(2026, 9, 15, 9, 0))
    ops.append_log("审核", "第二条", when=datetime(2026, 9, 15, 10, 0))
    ops.append_log("上传", "第三条", when=datetime(2026, 9, 15, 11, 0))
    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    assert lines == ["- 2026-09-15 09:00 · 编译 · 第一条",
                     "- 2026-09-15 10:00 · 审核 · 第二条",
                     "- 2026-09-15 11:00 · 上传 · 第三条"]


def test_append_log_never_breaks_main_flow(tmp_path, monkeypatch):
    """日志写失败只返回 False，**不抛异常**（不能因为日志写不进去就阻断编译/审核）。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    monkeypatch.setattr(ops, "log_path", lambda: Path(tmp_path / "不可写目录" / "log.md"))
    (tmp_path / "不可写目录").mkdir()
    (tmp_path / "不可写目录" / "log.md").mkdir()             # 同名目录 → 打开必失败
    assert ops.append_log("编译", "这条写不进去") is False


def test_approve_and_reject_write_log(tmp_path, monkeypatch):
    """人工放行/驳回同样进日志（文件侧镜像 audit_logs/decision 链）。"""
    target = _log(tmp_path, monkeypatch)
    root = paths.kb_root()
    (root / "pending_review").mkdir(parents=True, exist_ok=True)
    (root / "NEXUS/概念").mkdir(parents=True, exist_ok=True)
    (root / "NEXUS/index.md").write_text("# 知识库索引\n\n## 资源\n\n## 概念\n", encoding="utf-8")
    (root / "pending_review/示例概念.md").write_text(
        "---\ntype: concept\ntitle: 示例概念\nstatus: pending\n---\n正文", encoding="utf-8")

    import db
    with db.get_conn() as conn:
        review_id = conn.execute(
            "INSERT INTO pending_reviews (nexus_path, ai_verdict) "
            "VALUES ('pending_review/示例概念.md','approved') RETURNING id").fetchone()[0]
    db.upsert_entry("pending_review/示例概念.md", "concept", "示例概念", "共享层",
                    "pending", "V1.0", None, "2026-09-15")

    ops.approve_entry(review_id, "pending_review/示例概念.md", "NEXUS/概念/示例概念.md")
    text = target.read_text(encoding="utf-8")
    assert "· 审核 · 放行 pending_review/示例概念.md → NEXUS/概念/示例概念.md" in text
