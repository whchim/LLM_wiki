"""L2 编译队列测试（`compile_tasks` 从"记录表"升级为可并发认领的工作队列）。

锁住的契约：
1. **认领互斥**：一个任务同一时刻只能被一个 worker 认领（`FOR UPDATE SKIP LOCKED`）；
2. **租约自愈**：worker 崩溃后租约过期，任务自动回到可认领态；
3. **指数退避**：失败重排（status 回 pending + next_retry_at），attempts 用尽才落终态 failed；
4. **优先级**：交互式上传（10）先于批量扫描（100）被认领；
5. **人工重试**：`requeue_compile_task` 清空 attempts/退避，可再次认领；
6. **队列观测**：`queue_stats` 给出各状态计数与最老待处理年龄。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import db  # noqa: E402

WORKER_A = "worker-a"
WORKER_B = "worker-b"


def _enqueue(raw_path: str = "RAW/会议/a.md", fingerprint: str = "fp-a", **kwargs) -> int:
    return db.enqueue_compile_task(raw_path, fingerprint, **kwargs)


def _force_next_retry_to_past(task_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE compile_tasks SET next_retry_at = now() - interval '1 minute' "
                     "WHERE id=%s", (task_id,))


def _force_lease_to_past(task_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE compile_tasks SET lease_until = now() - interval '1 minute' "
                     "WHERE id=%s", (task_id,))


def test_claim_is_exclusive():
    """认领互斥：两次认领拿到不同任务；任务被占用时不会被第二个 worker 抢走。"""
    first_id = _enqueue("RAW/会议/a.md", "fp-a")
    first = db.claim_compile_task(WORKER_A)
    assert first["id"] == first_id and first["attempts"] == 1

    assert db.claim_compile_task(WORKER_B) is None       # 唯一任务已被占用

    second_id = _enqueue("RAW/会议/b.md", "fp-b")
    second = db.claim_compile_task(WORKER_B)
    assert second["id"] == second_id and second["id"] != first["id"]


def test_lease_expiry_returns_task_to_queue():
    """租约自愈：worker 崩溃后（租约过期）任务可被重新认领，attempts 自增。"""
    task_id = _enqueue()
    claimed = db.claim_compile_task(WORKER_A, lease_seconds=600)
    assert db.claim_compile_task(WORKER_B) is None

    _force_lease_to_past(claimed["id"])
    again = db.claim_compile_task(WORKER_B)
    assert again["id"] == task_id and again["attempts"] == 2


def test_retry_backoff_then_terminal_when_exhausted():
    """退避重排：失败回 pending 且带 next_retry_at；attempts 用尽后不再被认领。"""
    task_id = _enqueue()
    task = db.claim_compile_task(WORKER_A)
    db.finish_compile_task(task_id, "pending", error_msg="模型超时", retry_backoff_seconds=300)

    assert db.claim_compile_task(WORKER_B) is None        # 退避未到，不可认领
    _force_next_retry_to_past(task_id)
    again = db.claim_compile_task(WORKER_B)
    assert again["id"] == task_id and again["attempts"] == 2

    # 把 attempts 推到上限 → 不再可认领
    with db.get_conn() as conn:
        conn.execute("UPDATE compile_tasks SET attempts = %s WHERE id=%s",
                     (db.DEFAULT_MAX_ATTEMPTS, task_id))
        conn.execute("UPDATE compile_tasks SET status='pending', lease_until=NULL WHERE id=%s", (task_id,))
    assert db.claim_compile_task(WORKER_A) is None

    db.finish_compile_task(task_id, "failed", error_msg="尝试次数用尽")
    row = db.latest_compile_task("RAW/会议/a.md")
    assert row["status"] == "failed" and row["error_msg"] == "尝试次数用尽"


def test_priority_orders_claims():
    """优先级：交互式上传（10）先于批量扫描（100）。"""
    batch_id = _enqueue("RAW/会议/batch.md", "fp-batch", priority=100)
    upload_id = _enqueue("RAW/会议/upload.md", "fp-upload", priority=10)
    assert db.claim_compile_task(WORKER_A)["id"] == upload_id
    assert db.claim_compile_task(WORKER_A)["id"] == batch_id


def test_tenant_filter_isolates_claims():
    """租户隔离：指定 tenant 只认领本租户任务（L3 的前置）。"""
    _enqueue("RAW/会议/t1.md", "fp-t1", tenant_id="tenant-a")
    t2_id = _enqueue("RAW/会议/t2.md", "fp-t2", tenant_id="tenant-b")
    claimed = db.claim_compile_task(WORKER_A, tenant_id="tenant-b")
    assert claimed["id"] == t2_id and claimed["tenant_id"] == "tenant-b"
    assert db.claim_compile_task(WORKER_A, tenant_id="tenant-b") is None
    assert db.claim_compile_task(WORKER_A, tenant_id="tenant-a")["raw_path"] == "RAW/会议/t1.md"


def test_finish_done_is_terminal_and_requeue_resets():
    """终态不再被认领；人工重试 requeue 清空 attempts/退避后可再认领。"""
    task_id = _enqueue()
    db.claim_compile_task(WORKER_A)
    db.finish_compile_task(task_id, "done", nexus_path="NEXUS/资源/x.md")
    assert db.claim_compile_task(WORKER_B) is None
    row = db.latest_compile_task("RAW/会议/a.md")
    assert row["status"] == "done" and row["nexus_path"] == "NEXUS/资源/x.md"

    db.finish_compile_task(task_id, "failed", error_msg="模型 500")
    assert db.claim_compile_task(WORKER_B) is None        # failed 不自动重试（等人工）
    db.requeue_compile_task(task_id)
    claimed = db.claim_compile_task(WORKER_B)
    assert claimed["id"] == task_id and claimed["attempts"] == 1


def test_queue_stats_reports_backlog():
    """队列观测：各状态计数 + 最老待处理年龄。"""
    _enqueue("RAW/会议/a.md", "fp-a")                  # 保持 pending
    done_id = _enqueue("RAW/会议/b.md", "fp-b")
    processing_id = _enqueue("RAW/会议/c.md", "fp-c")
    db.finish_compile_task(done_id, "done", nexus_path="NEXUS/资源/b.md")
    with db.get_conn() as conn:                        # 直接置 processing，避免认领顺序影响断言
        conn.execute("UPDATE compile_tasks SET status='processing' WHERE id=%s", (processing_id,))

    stats = db.queue_stats()
    assert stats.get("pending") == 1 and stats.get("processing") == 1 and stats.get("done") == 1
    assert stats["oldest_pending_seconds"] >= 0
    assert db.queue_stats(tenant_id="no-such-tenant") == {"oldest_pending_seconds": 0}


def test_requeued_file_is_not_recompiled(tmp_path, monkeypatch):
    """回归（真实数据暴露）：同一文件被**重复入队**时不得重复编译、不得产出 `-2` 重复条目。

    实测问题：队列模式原先只看"该路径的最新任务"（此时是自己这条 pending），
    漏掉历史 done 记录 → 重新编译 → 生成 `xxx-2.md` 与重复 knowledge_entries。
    """
    import compile_service

    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = tmp_path / "vault"
    for rel in ("RAW/会议", "NEXUS/资源", "NEXUS/概念", "pending_review", "_triggers/done"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "NEXUS/index.md").write_text("# 知识库索引\n\n## 资源\n\n## 概念\n", encoding="utf-8")
    raw_rel = "RAW/会议/重复入队.md"
    (root / raw_rel).write_text("会议纪要：客户确认下月联调。\n" * 5, encoding="utf-8")

    import json
    from sales_clarification_runtime import ModelResponse

    class Port:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system_prompt, user_prompt, max_tokens=4000, **kwargs):
            self.calls += 1
            payload = {
                "resource": {"title": "重复入队纪要", "description": "测试用", "tags": ["会议纪要"],
                             "department": "售前", "source_type": "会议",
                             "summary": "## 摘要\n\n测试。\n\n## 关键信息\n\n- 一点", "key_points": ["一点"]},
                "concepts": [],
            }
            return ModelResponse(json.dumps(payload, ensure_ascii=False), "fake", 10, 10, "r")

    first = compile_service.compile_one(raw_rel, port=Port())
    assert first.status == "done"

    # 模拟"同一文件被再次入队 → worker 认领后编译"
    second_task = db.enqueue_compile_task(raw_rel, db.latest_compile_task(raw_rel)["fingerprint"])
    claimed = db.claim_compile_task("worker-replay")
    assert claimed["id"] == second_task
    port = Port()
    replay = compile_service.compile_one(raw_rel, port=port, task_id=claimed["id"])
    assert replay.status == "skipped" and port.calls == 0          # 不再重复烧 token
    assert len(list((root / "NEXUS/资源").glob("*.md"))) == 1      # 没有 `-2` 重复条目
    with db.get_conn() as conn:
        count = conn.execute("SELECT count(*) FROM knowledge_entries WHERE path LIKE 'NEXUS/资源/%'"
                             ).fetchone()[0]
    assert count == 1


def test_upload_enqueues_with_high_priority_and_no_trigger_for_api_engine(tmp_path, monkeypatch):
    """上传即入队：交互式优先级 10；engine=api 时不写触发纸条（队列 worker 认领）。"""
    from fastapi.testclient import TestClient
    import os

    os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
    os.environ.setdefault("ADMIN_INIT_USER", "admin")
    os.environ.setdefault("ADMIN_INIT_PASS", "admin123")
    from api.main import app

    client = TestClient(app)
    token = client.post("/auth/login", json={"username": "admin", "password": "admin123"}
                        ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setenv("COMPILE_ENGINE", "api")
    response = client.post("/uploads", headers=headers,
                           files={"files": ("上传测试.md", "客户确认下周评审方案。".encode(), "text/markdown")},
                           data={"category": "会议"})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_ids"][0]
    with db.get_conn() as conn:
        row = conn.execute("SELECT priority, status, tenant_id FROM compile_tasks WHERE id=%s",
                           (task_id,)).fetchone()
    assert row[0] == 10 and row[1] == "pending" and row[2] == "default"
    assert not list((tmp_path / "vault" / "_triggers").glob("compile_*.md"))
