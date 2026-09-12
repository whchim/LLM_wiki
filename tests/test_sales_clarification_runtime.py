"""阶段 3 模型端口与运行时测试：使用 fake port，不访问网络或数据库。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

from sales_clarification_runtime import ModelResponse, build_user_prompt, run_clarification_agent

pytestmark = pytest.mark.no_db

CONTENT = "客户同意安排技术评审，下周二双方进行演示。"


def valid_output() -> dict:
    quote = "客户同意安排技术评审"
    return {
        "schema_version": "clarification.v1",
        "claims": [{
            "id": "claim-1", "type": "customer_commitment", "attribution": "customer_quote",
            "certainty": "explicit", "value": quote,
            "evidence": [{"source": "initial_note", "quote": quote, "start": 0, "end": len(quote)}],
        }],
        "missing_facts": [], "questions": [], "stop_reason": "ready_for_proposal", "can_propose": True,
        "model_version": "fake-v1", "prompt_version": "sales-clarification-v1",
    }


class FakePort:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def test_build_prompt_contains_only_redacted_context():
    prompt = build_user_prompt(customer_id="customer-1", content_redacted=CONTENT, current_state="contacted")
    assert "customer-1" in prompt
    assert CONTENT in prompt
    assert "原始" not in prompt


def test_valid_model_output_is_accepted():
    port = FakePort([ModelResponse(json.dumps(valid_output(), ensure_ascii=False), "fake-v1", 100, 80, "req-1")])
    result = run_clarification_agent(port, customer_id="customer-1", content_redacted=CONTENT, current_state="contacted", system_prompt="system")
    assert result.status == "accepted"
    assert result.output["schema_version"] == "clarification.v1"
    assert result.total_input_tokens == 100
    assert result.total_output_tokens == 80
    assert len(port.calls) == 1


def test_contract_error_retries_once_then_accepts():
    invalid = valid_output()
    invalid["claims"][0]["evidence"][0]["quote"] = "错误证据"
    port = FakePort([
        ModelResponse(json.dumps(invalid, ensure_ascii=False), "fake-v1", 10, 20),
        ModelResponse(json.dumps(valid_output(), ensure_ascii=False), "fake-v1", 12, 22),
    ])
    attempts = []
    result = run_clarification_agent(port, customer_id="customer-1", content_redacted=CONTENT, system_prompt="system", on_attempt=attempts.append)
    assert result.status == "accepted"
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "contract_error"
    assert [item.attempt for item in attempts] == [1, 2]


def test_model_error_transfers_to_human_after_retry():
    class FailingPort:
        def complete(self, **kwargs):
            raise TimeoutError("provider timeout")

    result = run_clarification_agent(FailingPort(), customer_id="customer-1", content_redacted=CONTENT, system_prompt="system")
    assert result.status == "needs_human_review"
    assert result.needs_human_review is True
    assert len(result.attempts) == 2
    assert "timeout" in result.errors[0]


def test_no_retry_can_be_disabled():
    invalid = valid_output()
    invalid["can_propose"] = False
    port = FakePort([ModelResponse(json.dumps(invalid, ensure_ascii=False), "fake-v1")])
    result = run_clarification_agent(port, customer_id="customer-1", content_redacted=CONTENT, system_prompt="system", max_retries=0)
    assert result.status == "needs_human_review"
    assert len(result.attempts) == 1


def test_input_and_token_limits_are_rejected_before_model_call():
    port = FakePort([])
    with pytest.raises(ValueError, match="超过"):
        run_clarification_agent(port, customer_id="customer-1", content_redacted="中" * 12_001, system_prompt="system")
    with pytest.raises(ValueError, match="max_tokens"):
        run_clarification_agent(port, customer_id="customer-1", content_redacted=CONTENT, system_prompt="system", max_tokens=2_001)
    assert port.calls == []


def test_audit_summary_excludes_content():
    port = FakePort([ModelResponse(json.dumps(valid_output(), ensure_ascii=False), "fake-v1", 1, 2)])
    result = run_clarification_agent(port, customer_id="customer-1", content_redacted=CONTENT, system_prompt="system")
    audit = result.audit_dict()
    assert CONTENT not in json.dumps(audit, ensure_ascii=False)
    assert audit["status"] == "accepted"
