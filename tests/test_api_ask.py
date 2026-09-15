"""`POST /ask` 接口测试（对话窗口）：鉴权、响应形状、409（未配模型）、422（空问题）。

服务层纪律（门禁先于模型 / 引用溯源 / 缺口回流）已在 `tests/test_answer_service.py` 锁死，
这里只锁 HTTP 层的契约：谁能调、返回什么、错怎么报。
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

import answer_service  # noqa: E402
from api.main import app  # noqa: E402
from test_answer_service import BODY, PATH_A, QUOTE, _Port, _entry, _retriever, _valid_payload  # noqa: E402

pytestmark = pytest.mark.usefixtures("_env")


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def headers(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_ask_requires_login(client):
    assert client.post("/ask", json={"question": "部署模式有哪些"}).status_code == 401


def test_ask_rejects_empty_question(client, headers):
    assert client.post("/ask", headers=headers, json={"question": ""}).status_code == 422
    assert client.post("/ask", headers=headers, json={"question": "x" * 400}).status_code == 422


def test_ask_returns_answer_with_traceable_citations(client, headers, monkeypatch):
    monkeypatch.setattr(answer_service.retrieval, "run_search", _retriever([_entry()]))
    monkeypatch.setattr(answer_service.model_port, "for_tenant", lambda *a, **k: _Port([_valid_payload()]))

    response = client.post("/ask", headers=headers, json={"question": "部署模式有哪些"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["citations"][0]["path"] == PATH_A
    assert payload["citations"][0]["quote"] == QUOTE
    assert payload["retrieved"][0]["path"] == PATH_A
    assert payload["usage"] == {"input_tokens": 120, "output_tokens": 60}
    assert payload["engine"] == "api" and payload["trace_id"]
    assert response.headers.get("X-Trace-Id")


def test_ask_reports_gap_without_calling_model(client, headers, monkeypatch):
    port = _Port([_valid_payload()])
    monkeypatch.setattr(answer_service.retrieval, "run_search", _retriever([], gap=True))
    monkeypatch.setattr(answer_service.model_port, "for_tenant", lambda *a, **k: port)

    payload = client.post("/ask", headers=headers, json={"question": "内部报销额度是多少"}).json()
    assert payload["status"] == "no_hits"
    assert payload["gap"] is True and payload["insufficient"] is True
    assert port.calls == 0
    assert payload["citations"] == []


def test_ask_returns_409_when_model_not_configured(client, headers, monkeypatch):
    monkeypatch.setattr(answer_service.retrieval, "run_search", _retriever([_entry()]))
    monkeypatch.setattr(answer_service.model_port, "for_tenant", lambda *a, **k: None)

    response = client.post("/ask", headers=headers, json={"question": "部署模式有哪些"})
    assert response.status_code == 409
    assert "未配置" in response.json()["detail"]


def test_ask_fails_loudly_on_contract_violation(client, headers, monkeypatch):
    """两次编造引用 → 明确 failed（**不返回答案**），但检索依据仍照实返回给用户。"""
    bad = _valid_payload(citations=[{"path": "NEXUS/概念/编的.md", "quote": QUOTE, "note": ""}])
    monkeypatch.setattr(answer_service.retrieval, "run_search", _retriever([_entry()]))
    monkeypatch.setattr(answer_service.model_port, "for_tenant", lambda *a, **k: _Port([bad, bad]))

    payload = client.post("/ask", headers=headers, json={"question": "部署模式有哪些"}).json()
    assert payload["status"] == "failed"
    assert payload["contract_ok"] is False
    assert payload["answer"] is None
    assert "契约" in payload["error"]
    assert payload["retrieved"][0]["path"] == PATH_A
    assert BODY.strip().startswith("# 示例监测产品")          # 检索到的依据本身是真的
