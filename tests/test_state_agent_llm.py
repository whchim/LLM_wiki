"""状态 Agent（LLM 版）接线测试。

覆盖三层：
1. 运行时（纯函数/mock port）：解析、偏移按 quote 重定位、来源作用域、契约拒绝与重试；
2. 服务模式：auto 模式下**规则能判就不调模型**（省成本）、模糊情形才请模型复核；
3. 回退与降级：模型不可用/输出不合法 → 回退规则并可观测；转人工会话强制 needs_review。
"""
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

from api.main import app  # noqa: E402

import model_port  # noqa: E402
import sales_state_agent  # noqa: E402
from sales_clarification_runtime import ModelResponse  # noqa: E402

CONTENT = "客户确认正在评估方案，销售将在下周跟进预算反馈。"


class ScriptedPort:
    """按脚本返回模型输出；记录调用次数（用于断言"该不该调模型"）。"""

    def __init__(self, outputs: list[dict | str]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._outputs.pop(0) if self._outputs else {}
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ModelResponse(text, "fake-state-v1", 12, 9, "req-1")


def _valid_output(state: str = "new_lead", current: str | None = None,
                  decision: str = "propose", quote: str = "客户确认正在评估方案",
                  customer: str = "customer-llm-001") -> dict:
    return {
        "customer_id": customer, "current_state": current,
        "proposed_state": state, "decision": decision, "confidence": 0.86,
        "evidence": [{"quote": quote, "start": 0, "end": 3, "meaning": "客户已进入需求确认"}],
        "reasoning_summary": "客户明确表达了需求。", "next_action": "安排方案沟通",
        "needs_human_confirmation": True, "risk_flags": [],
        "model_version": "fake-state-v1", "prompt_version": sales_state_agent.PROMPT_VERSION,
    }


# ---------- 1. 运行时（不连 DB） ----------

@pytest.mark.no_db
def test_runtime_accepts_valid_output_and_relocates_offsets():
    """模型给的偏移不可信：服务端按 quote 重新定位。"""
    port = ScriptedPort([_valid_output()])
    run = sales_state_agent.run_state_agent(
        port, customer_id="customer-llm-001", content_redacted=CONTENT, current_state=None,
        system_prompt="system")
    assert run.accepted is True
    item = run.output["evidence"][0]
    start, end = item["start"], item["end"]
    assert CONTENT[start:end] == item["quote"]      # 偏移被重定位为真实位置
    assert run.total_input_tokens == 12 and run.total_output_tokens == 9


@pytest.mark.no_db
def test_runtime_supports_answer_sourced_evidence():
    """证据可以来自追问回答（source=question-N），按该来源文本校验。"""
    answer = "客户说预算已经批下来了"
    output = _valid_output(quote="预算已经批下来了")
    output["evidence"][0]["source"] = "question-1"
    port = ScriptedPort([output])
    run = sales_state_agent.run_state_agent(
        port, customer_id="customer-llm-001", content_redacted=CONTENT, current_state=None,
        answer_texts={"question-1": answer}, system_prompt="system")
    assert run.accepted is True
    item = run.output["evidence"][0]
    assert answer[item["start"]:item["end"]] == item["quote"]


@pytest.mark.no_db
def test_runtime_rejects_unknown_field_and_unlocatable_quote():
    bad = _valid_output()
    bad["extra_field"] = "不该出现"
    port = ScriptedPort([bad, bad])       # 重试一次仍失败
    run = sales_state_agent.run_state_agent(
        port, customer_id="customer-llm-001", content_redacted=CONTENT, current_state=None,
        system_prompt="system")
    assert run.status == "needs_human_review"
    assert len(port.calls) == 2           # 最多一次重试
    assert any("越界字段" in e for e in run.errors)

    not_locatable = _valid_output(quote="原文里没有这句话")
    port2 = ScriptedPort([not_locatable, not_locatable])
    run2 = sales_state_agent.run_state_agent(
        port2, customer_id="customer-llm-001", content_redacted=CONTENT, current_state=None,
        system_prompt="system")
    assert run2.status == "needs_human_review"
    assert any("无法精确定位" in e for e in run2.errors)


@pytest.mark.no_db
def test_retry_carries_contract_error_back_to_model():
    """重试必须带上拒绝原因（实测：真模型在无当前阶段时给出 need_confirmed，盲重试会再错一次）。

    第一次跳跃（none → need_confirmed）被拒 → 第二次 prompt 里出现具体错误 → 改判 new_lead → 通过。
    """
    port = ScriptedPort([_valid_output(state="need_confirmed"), _valid_output(state="new_lead")])
    run = sales_state_agent.run_state_agent(
        port, customer_id="customer-llm-001", content_redacted=CONTENT, current_state=None,
        system_prompt="system")
    assert run.accepted is True
    assert run.output["proposed_state"] == "new_lead"
    assert len(port.calls) == 2
    assert "未通过服务端校验" not in port.calls[0]["user_prompt"]
    feedback = port.calls[1]["user_prompt"]
    assert "未通过服务端校验" in feedback and "不允许状态转移" in feedback


@pytest.mark.no_db
def test_runtime_rejects_illegal_state_jump():
    """new_lead 不能直接给 solution_eval（状态机逐级推进）。"""
    bad = _valid_output(state="solution_eval", current="new_lead")
    port = ScriptedPort([bad, bad])
    run = sales_state_agent.run_state_agent(
        port, customer_id="customer-llm-001", content_redacted=CONTENT, current_state="new_lead",
        system_prompt="system")
    assert run.status == "needs_human_review"
    assert any("不允许状态转移" in e for e in run.errors)


# ---------- 2/3. 服务与 API（真 PG） ----------

@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def admin_headers(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _intake(client, headers, key: str, customer: str, content: str = CONTENT) -> dict:
    """提交一次洽谈。

    注意：同一客户 + **完全相同正文**会被内容级幂等复用（防重复提交），
    所以测试里"同一客户的多次洽谈"必须给不同的正文（现实中每次洽谈本来就不是同一份纪要）。
    """
    response = client.post("/clarifications/intake", headers=headers, json={
        "idempotency_key": key, "customer_id": customer, "content": content,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source_type": "meeting_note",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duplicate"] is None, "测试用的多次洽谈不应被判为重复提交（请给不同正文）"
    return {"session_id": body["session"]["session_id"], "customer_id": customer,
            "conversation_id": body["conversation"]["conversation_id"]}


def _ready(client, headers, key: str, customer: str, claims: list[dict],
           content: str = CONTENT) -> dict:
    """建一条 ready_for_proposal 会话，claims 决定规则能否自行判定。"""
    import db
    ctx = _intake(client, headers, key, customer, content)
    output = {"schema_version": "clarification.v1", "claims": claims, "missing_facts": [],
              "questions": [], "stop_reason": "ready_for_proposal", "can_propose": True,
              "model_version": "test-model", "prompt_version": "v1"}
    db.append_clarification_turn(ctx["session_id"], "ready_for_proposal", output, 0)
    return ctx


def _claim(value: str, ctype: str = "next_step", quote: str = "客户确认正在评估方案") -> dict:
    return {"id": "claim-1", "type": ctype, "attribution": "customer_quote", "certainty": "explicit",
            "value": value, "evidence": [{"source": "initial_note", "quote": quote}]}


def _set_current_state(client, headers, customer: str, target: str = "contacted") -> None:
    """走正常链路（生成建议 → 负责人确认）把客户推进到 target。

    歧义场景需要"当前已有阶段"才成立：无阶段时规则总能判定 new_lead，轮不到模型复核。
    每次推进用的是**不同的洽谈纪要**（同一客户可以有多次洽谈）。
    """
    plan = [
        ("a", "首次电话沟通，客户要了产品介绍。", [], "new_lead"),
        ("b", "第二次沟通，客户正在做技术评估。", [_claim("客户正在做技术评估", quote="客户正在做技术评估")], "contacted"),
    ]
    for suffix, content, claims, expected in plan:
        ctx = _ready(client, headers, f"prime-{suffix}-{customer}", customer, claims, content)
        proposal_id = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                                  headers=headers, params={"mode": "rules"}).json()["proposal_id"]
        decided = client.post(f"/customer-states/proposals/{proposal_id}/decision", headers=headers,
                              json={"decision": "approved", "reason": "负责人确认"})
        assert decided.status_code == 200, decided.text
        assert decided.json()["state"] == expected
        if expected == target:
            return
    raise AssertionError(f"未到达目标阶段：{target}")


def test_auto_mode_skips_llm_when_rules_can_conclude(client, admin_headers, monkeypatch):
    """规则能判就不调模型——避免为每条建议白付一次 LLM 成本。"""
    def _boom():
        raise AssertionError("规则已能判定时不应调用模型")
    monkeypatch.setattr(model_port, "for_tenant", lambda **_: _boom())

    ctx = _ready(client, admin_headers, "llm-001", "customer-llm-001", [_claim("客户确认正在评估方案")])
    body = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                       headers=admin_headers).json()
    assert body["generated"] is True
    assert body["used"] == "rules"
    assert body["proposed_state"] == "new_lead"


def test_auto_mode_asks_llm_for_ambiguous_case(client, admin_headers, monkeypatch):
    """规则判不出（证据不足/表述超出关键词）时，请 LLM 复核并采用其结论。"""
    customer = "customer-llm-002"
    _set_current_state(client, admin_headers, customer, "contacted")

    port = ScriptedPort([_valid_output(state="need_confirmed", current="contacted",
                                       customer=customer)])
    monkeypatch.setattr(model_port, "for_tenant", lambda **_: port)

    # claims 里没有规则认识的关键词 → 规则只给 needs_review → 触发 LLM 复核
    ctx = _ready(client, admin_headers, "llm-002", customer, [_claim("客户想再了解一下我们的方案")])
    body = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                       headers=admin_headers).json()
    assert body["used"] == "llm"
    assert body["decision"] == "propose"
    assert body["proposed_state"] == "need_confirmed"
    assert len(port.calls) == 1


def test_auto_mode_falls_back_to_rules_when_model_fails(client, admin_headers, monkeypatch):
    """模型输出不合法 → 回退规则结论，且失败原因可观测（不把请求打挂）。"""
    customer = "customer-llm-003"
    _set_current_state(client, admin_headers, customer, "contacted")
    port = ScriptedPort(["这不是 JSON", "这也不是 JSON"])
    monkeypatch.setattr(model_port, "for_tenant", lambda **_: port)

    ctx = _ready(client, admin_headers, "llm-003", customer, [_claim("客户想再了解一下我们的方案")])
    response = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["used"] == "rules"
    assert body["llm_error"] and "模型判定失败" in body["llm_error"]
    assert body["generated"] is True


def test_llm_mode_errors_when_model_unavailable(client, admin_headers, monkeypatch):
    monkeypatch.setattr(model_port, "for_tenant", lambda **_: None)
    ctx = _ready(client, admin_headers, "llm-004", "customer-llm-004", [_claim("客户确认正在评估方案")])
    response = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                           headers=admin_headers, params={"mode": "llm"})
    assert response.status_code == 409
    assert "未配置模型" in response.json()["detail"]


def test_human_review_session_forces_needs_review_in_llm_path(client, admin_headers, monkeypatch):
    """转人工会话即使模型给出 propose，也必须降级为 needs_review（不冒充自动结论）。"""
    import db
    customer = "customer-llm-005"
    port = ScriptedPort([_valid_output(state="new_lead", current=None, customer=customer)])
    monkeypatch.setattr(model_port, "for_tenant", lambda **_: port)

    ctx = _intake(client, admin_headers, "llm-005", customer)
    db.append_clarification_turn(ctx["session_id"], "human_review", {"error": ["模型或契约失败"]}, 0)
    body = client.post(f"/clarifications/sessions/{ctx['session_id']}/proposal",
                       headers=admin_headers, params={"mode": "llm"}).json()
    assert body["used"] == "llm+human_review"
    assert body["decision"] == "needs_review"
    assert body["confidence"] <= 0.5
    assert "low_confidence" in body["risk_flags"]
