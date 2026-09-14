"""「同一份纪要重复提交」防重复测试。

实测问题：前端每次读文件都换一个 `idempotency_key`（`file-${uid()}`），所以幂等键只防
"同一个键重放"，防不了"同一份内容再提交一次"——同一客户堆出多条看起来一模一样的会话，
销售在「我的澄清会话」里看到的就是"重复"。

本文件锁住修复后的契约：
1. 同客户 + 同正文（sha256 指纹）→ **复用**原洽谈与会话，响应带 `duplicate`，不新建；
2. `force_new=true` 才显式新建（确实要再谈一次）；
3. 正文不同（哪怕同客户）→ 正常新建（不误伤）；
4. 别人提交的同内容不因"去重"被暴露（不能越权读到他人会话）；
5. 已归档的原会话不复用（否则会把人引回已归档记录）；
6. `/mine` 列表带摘要与来源文件名 + 提交时间，让同一客户的多次洽谈可分辨。
"""
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

CONTENT = "客户确认正在评估方案，销售将在下周跟进预算反馈。"
OTHER_CONTENT = "客户表示先内部讨论，下周再给答复。"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def admin_headers(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _intake(client, headers, key: str, customer: str, content: str = CONTENT, **extra) -> dict:
    response = client.post("/clarifications/intake", headers=headers, json={
        "idempotency_key": key, "customer_id": customer, "content": content,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_type": "meeting_note",
        "source_ref": extra.pop("source_ref", "1月研讨会纪要.md"), **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _user_headers(client, username: str) -> dict:
    import db
    from api import auth as auth_mod
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,'user')",
                     (username, auth_mod.hash_password("plain123")))
    token = client.post("/auth/login", json={"username": username, "password": "plain123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _session_count(client, headers, customer: str) -> int:
    return len(_mine_rows(client, headers, customer))


def _mine_rows(client, headers, customer: str) -> list[dict]:
    rows = client.get("/clarifications/mine", headers=headers).json()
    return [r for r in rows if r["customer_id"] == customer]


def test_same_content_reuses_conversation(client, admin_headers):
    """同一份纪要再提交一次：复用原会话、明确告知原因，不产生第二条记录。"""
    first = _intake(client, admin_headers, "dup-001-a", "customer-dup-001")
    assert first["duplicate"] is None
    second = _intake(client, admin_headers, "dup-001-b", "customer-dup-001")   # 新的幂等键，同内容

    assert second["duplicate"] is not None
    assert second["duplicate"]["reason"] == "same_content"
    assert second["duplicate"]["session_id"] == first["session"]["session_id"]
    assert second["duplicate"]["source_ref"] == "1月研讨会纪要.md"
    assert second["session"]["session_id"] == first["session"]["session_id"]
    assert second["conversation"]["conversation_id"] == first["conversation"]["conversation_id"]
    assert second["gate"]["idempotent_replay"] is False          # 不是幂等键重放，是内容去重
    assert _session_count(client, admin_headers, "customer-dup-001") == 1


def test_force_new_creates_second_conversation(client, admin_headers):
    """确实要再谈一次：显式 force_new 才新建（默认不再堆会话）。"""
    first = _intake(client, admin_headers, "dup-002-a", "customer-dup-002")
    forced = _intake(client, admin_headers, "dup-002-b", "customer-dup-002", force_new=True)
    assert forced["duplicate"] is None
    assert forced["session"]["session_id"] != first["session"]["session_id"]
    assert _session_count(client, admin_headers, "customer-dup-002") == 2


def test_different_content_creates_new_conversation(client, admin_headers):
    """内容不同（同客户）→ 正常新建，去重不误伤真实的新洽谈。"""
    first = _intake(client, admin_headers, "dup-003-a", "customer-dup-003")
    other = _intake(client, admin_headers, "dup-003-b", "customer-dup-003", content=OTHER_CONTENT)
    assert other["duplicate"] is None
    assert other["session"]["session_id"] != first["session"]["session_id"]
    assert _session_count(client, admin_headers, "customer-dup-003") == 2


def test_same_content_from_another_sales_does_not_leak(client, admin_headers):
    """别人提交过的同一份纪要：不能因为"内容相同"就把他人会话交出去，只能新建自己的。"""
    first = _intake(client, admin_headers, "dup-004-a", "customer-dup-004")
    other = _user_headers(client, "dup-sales-other")
    second = _intake(client, other, "dup-004-b", "customer-dup-004")
    assert second["duplicate"] is None
    assert second["session"]["session_id"] != first["session"]["session_id"]
    # 他人只能看到自己的记录
    own = client.get("/clarifications/mine", headers=other).json()
    assert [r["session_id"] for r in own] == [second["session"]["session_id"]]


def test_archived_conversation_is_not_reused(client, admin_headers):
    """原会话已归档：不复用（否则会把人引回已归档会话），改为新建。"""
    first = _intake(client, admin_headers, "dup-005-a", "customer-dup-005")
    archived = client.delete(f"/clarifications/sessions/{first['session']['session_id']}", headers=admin_headers)
    assert archived.status_code == 200, archived.text

    again = _intake(client, admin_headers, "dup-005-b", "customer-dup-005")
    assert again["duplicate"] is None
    assert again["session"]["session_id"] != first["session"]["session_id"]
    visible = client.get("/clarifications/mine", headers=admin_headers).json()
    assert [r["session_id"] for r in visible if r["customer_id"] == "customer-dup-005"] == \
        [again["session"]["session_id"]]


def test_fingerprint_is_stable_across_numeric_encryption(client, admin_headers, monkeypatch):
    """回归：正文含精确金额时，指纹必须**在数值加密之前**算。

    实测根因——加密给同一数值生成随机密文与新占位符 id，若用加密后的正文算指纹，
    同一份纪要每提交一次指纹都不同，去重完全失效（用户看到的就是"重复的会话"）。
    这里断言：两次提交的脱敏正文**不同**（占位符 id 随机），但按指纹判定为同一份内容。
    """
    import db
    import sensitive_cipher
    monkeypatch.setenv(sensitive_cipher.ENV_KEY, sensitive_cipher.generate_key())   # 允许正文带精确金额
    content = "客户确认采购 5 架无人机，预算 80 万元，要求月底前签约。"
    first = _intake(client, admin_headers, "dup-007-a", "customer-dup-007", content=content)
    second = _intake(client, admin_headers, "dup-007-b", "customer-dup-007", content=content)

    assert second["duplicate"] is not None, "含金额的同一份纪要必须被识别为重复"
    assert second["session"]["session_id"] == first["session"]["session_id"]

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT e.content_redacted, e.content_hash FROM evidence e "
            "JOIN conversations c ON c.conversation_id=e.conversation_id "
            "WHERE c.customer_id='customer-dup-007' ORDER BY e.created_at").fetchall()
    assert len(rows) == 1, "重复提交不应追加第二条证据"
    assert "[AMOUNT_REF:" in rows[0][0] and "80 万元" not in rows[0][0]     # 精确金额仍被占位符替代
    assert rows[0][1] == first["evidence"]["content_hash"]
    # 列表摘要里也不能出现占位符 id / Markdown 标记这种技术噪音，只说"数值已隐藏"
    preview = _mine_rows(client, admin_headers, "customer-dup-007")[0]["content_preview"]
    assert "【数值已隐藏】" in preview and "nv-" not in preview
    assert preview.startswith("客户确认采购") and "**" not in preview and "#" not in preview


def test_mine_exposes_preview_and_source_for_disambiguation(client, admin_headers):
    """列表要能分辨同一客户的多次洽谈：摘要 + 来源文件名 + 提交时间都在。"""
    first = _intake(client, admin_headers, "dup-006-a", "customer-dup-006", source_ref="一月纪要.md")
    _intake(client, admin_headers, "dup-006-b", "customer-dup-006", content=OTHER_CONTENT,
            source_ref="二月纪要.md")
    rows = [r for r in client.get("/clarifications/mine", headers=admin_headers).json()
            if r["customer_id"] == "customer-dup-006"]
    assert len(rows) == 2
    for row in rows:
        assert row["content_preview"] and row["created_at"] and row["source_ref"]
    assert {r["source_ref"] for r in rows} == {"一月纪要.md", "二月纪要.md"}
    assert first["session"]["session_id"] in {r["session_id"] for r in rows}
