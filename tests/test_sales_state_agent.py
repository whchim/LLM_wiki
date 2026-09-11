from datetime import datetime, timezone

import pytest

import sales_state_agent


CONTENT = "客户确认正在评估方案，销售将在下周跟进。"


def _output(**changes):
    value = {
        "customer_id": "customer-demo-001",
        "current_state": None,
        "proposed_state": "new_lead",
        "decision": "propose",
        "confidence": 0.9,
        "evidence": [{"quote": "客户确认正在评估方案", "start": 0, "end": 10, "meaning": "客户表达了明确需求"}],
        "reasoning_summary": "客户已出现明确业务需求，但仍需负责人确认。",
        "next_action": "跟进客户反馈。",
        "valid_until": "2099-09-08T10:00:00+00:00",
        "needs_human_confirmation": True,
        "risk_flags": [],
        "model_version": "sales-model-v1",
        "prompt_version": "sales-state-v1",
    }
    value.update(changes)
    return value


def test_valid_output_passes_and_evidence_is_locatable():
    assert sales_state_agent.validate_state_agent_output(
        _output(), customer_id="customer-demo-001", content_redacted=CONTENT
    ) == []


def test_unknown_fields_and_customer_mismatch_are_rejected():
    errors = sales_state_agent.validate_state_agent_output(
        _output(customer_id="other", unexpected="write_current_state"),
        customer_id="customer-demo-001", content_redacted=CONTENT,
    )
    assert any("越界字段" in error for error in errors)
    assert any("不一致" in error for error in errors)


def test_unlocatable_evidence_is_rejected():
    errors = sales_state_agent.validate_state_agent_output(
        _output(evidence=[{"quote": "不存在的引用", "start": 0, "end": 7, "meaning": "幻觉"}]),
        customer_id="customer-demo-001", content_redacted=CONTENT,
    )
    assert any("无法精确定位" in error for error in errors)


def test_low_confidence_and_won_require_human_confirmation():
    low = sales_state_agent.validate_state_agent_output(
        _output(confidence=0.3, needs_human_confirmation=False),
        customer_id="customer-demo-001", content_redacted=CONTENT,
    )
    assert any("低置信度" in error for error in low)
    won = sales_state_agent.validate_state_agent_output(
        _output(proposed_state="won", needs_human_confirmation=False),
        customer_id="customer-demo-001", content_redacted=CONTENT,
    )
    assert any("won" in error for error in won)


def test_invalid_transition_and_expired_time_are_rejected():
    errors = sales_state_agent.validate_state_agent_output(
        _output(current_state="new_lead", proposed_state="won", valid_until="2020-01-01T00:00:00+00:00"),
        customer_id="customer-demo-001", content_redacted=CONTENT, current_state="new_lead",
    )
    assert any("当前投影" in error or "valid_until" in error or "不允许状态转移" in error for error in errors)


def test_parser_rejects_non_json_and_markdown_fence():
    with pytest.raises(ValueError):
        sales_state_agent.parse_state_agent_output("```json\n{}\n```")
    with pytest.raises(ValueError):
        sales_state_agent.parse_state_agent_output("[]")


def test_create_adapter_only_writes_proposal(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "proposal-1"

    monkeypatch.setattr(sales_state_agent.customer_state, "create_proposal", fake_create)
    proposal_id = sales_state_agent.create_proposal_from_agent_output(
        _output(), conversation_id="conv-1", customer_id="customer-demo-001", content_redacted=CONTENT
    )
    assert proposal_id == "proposal-1"
    assert captured["proposed_state"] == "new_lead"
    assert captured["decision"] == "propose"
