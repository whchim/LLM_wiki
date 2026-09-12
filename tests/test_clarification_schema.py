"""阶段 1 事实与追问契约测试：纯函数、无 DB/网络。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

from clarification_schema import parse_clarification_output, validate_clarification_output

pytestmark = pytest.mark.no_db


CONTENT = "客户同意安排技术评审，下周二双方进行演示。"


def valid_ready() -> dict:
    quote = "客户同意安排技术评审"
    return {
        "schema_version": "clarification.v1",
        "claims": [{
            "id": "claim-1", "type": "customer_commitment", "attribution": "customer_quote",
            "certainty": "explicit", "value": quote,
            "evidence": [{"source": "initial_note", "quote": quote, "start": 0, "end": len(quote)}],
        }],
        "missing_facts": [], "questions": [], "stop_reason": "ready_for_proposal",
        "can_propose": True, "model_version": "test-model", "prompt_version": "test-prompt",
    }


def test_valid_ready_output():
    assert validate_clarification_output(valid_ready(), content_redacted=CONTENT) == []


def test_evidence_must_match_exact_text():
    output = valid_ready()
    output["claims"][0]["evidence"][0]["quote"] = "客户同意安排技术评审。"
    assert any("精确定位" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_customer_quote_cannot_be_unknown():
    output = valid_ready()
    output["claims"][0]["certainty"] = "unknown"
    assert any("customer_quote" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_clarification_question_must_reference_missing_fact():
    output = valid_ready()
    output.update({
        "questions": [{"id": "q-1", "missing_fact_id": "missing-1", "question": "客户是否同意试用？", "answer_type": "yes_no"}],
        "stop_reason": "needs_clarification", "can_propose": False,
    })
    assert any("missing_fact_id" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_clarification_requires_question():
    output = valid_ready()
    output["stop_reason"] = "needs_clarification"
    output["can_propose"] = False
    assert any("至少需要一个追问" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_question_requires_clarification_stop_reason():
    output = valid_ready()
    output["missing_facts"] = [{"id": "missing-1", "type": "next_step", "priority": "high", "why_needed": "判断是否已约定后续动作", "impact_states": ["solution_eval"]}]
    output["questions"] = [{"id": "q-1", "missing_fact_id": "missing-1", "question": "下一步是否已约定？", "answer_type": "yes_no"}]
    assert any("只有 stop_reason" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_max_two_questions():
    output = valid_ready()
    output["missing_facts"] = [{"id": f"m-{i}", "type": "next_step", "priority": "high", "why_needed": "影响状态", "impact_states": ["solution_eval"]} for i in range(3)]
    output["questions"] = [{"id": f"q-{i}", "missing_fact_id": f"m-{i}", "question": "是否已约定下一步？", "answer_type": "yes_no"} for i in range(3)]
    output["stop_reason"] = "needs_clarification"
    output["can_propose"] = False
    assert any("最多 2" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_sensitive_question_is_rejected():
    output = valid_ready()
    output["missing_facts"] = [{"id": "m-1", "type": "objection", "priority": "high", "why_needed": "判断商务阻塞", "impact_states": ["commercial_negotiation"]}]
    output["questions"] = [{"id": "q-1", "missing_fact_id": "m-1", "question": "请提供客户的具体报价和详细预算？", "answer_type": "short_text"}]
    output["stop_reason"] = "needs_clarification"
    output["can_propose"] = False
    assert any("敏感信息" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_expired_cannot_be_impact_state():
    output = valid_ready()
    output["missing_facts"] = [{"id": "m-1", "type": "timeline", "priority": "medium", "why_needed": "判断有效期", "impact_states": ["expired"]}]
    assert any("expired" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_ready_requires_claim():
    output = valid_ready()
    output["claims"] = []
    assert any("至少需要一条事实" in error for error in validate_clarification_output(output, content_redacted=CONTENT))


def test_answer_evidence_can_reference_answer_content():
    output = valid_ready()
    output["claims"][0]["evidence"] = [{"source": "answer-1", "quote": "客户同意", "start": 0, "end": 4}]
    assert validate_clarification_output(output, content_redacted=CONTENT, answer_contents={"answer-1": "客户同意试用"}) == []


def test_parse_rejects_markdown_fence():
    try:
        parse_clarification_output("```json\n{}\n```")
    except ValueError as exc:
        assert "合法 JSON" in str(exc)
    else:
        raise AssertionError("markdown fence must be rejected")
