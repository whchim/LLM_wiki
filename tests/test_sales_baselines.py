"""阶段 2 传统基线测试：纯函数、无 DB/网络。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "streamlit_app"))

from sales_baselines import run_keyword_rule_baseline, run_structured_form_baseline

pytestmark = pytest.mark.no_db


def form_payload(**changes):
    payload = {
        "customer_id": "customer-demo-001",
        "customer_need": "客户需要验证多租户能力",
        "customer_commitment": "客户同意安排技术评审",
        "timeline": "下周二",
        "next_step": "双方进行演示",
        "proposed_state": "solution_eval",
        "evidence_note": "会议纪要第 1 段",
    }
    payload.update(changes)
    return payload


def test_structured_form_valid_result():
    result = run_structured_form_baseline(form_payload(), current_state="need_confirmed")
    assert result.accepted is True
    assert result.proposed_state == "solution_eval"
    assert result.operation_count == 6
    assert result.evidence_mode == "manual_note"


def test_structured_form_missing_field_transfers_to_human():
    result = run_structured_form_baseline(form_payload(next_step=""), current_state="need_confirmed")
    assert result.accepted is False
    assert "next_step" in result.missing_fact_types
    assert result.proposed_state is None


def test_structured_form_invalid_transition_is_rejected():
    result = run_structured_form_baseline(form_payload(proposed_state="won"), current_state="new_lead")
    assert result.accepted is False
    assert any("状态机" in note for note in result.notes)


def test_structured_form_serializes_to_common_result():
    result = run_structured_form_baseline(form_payload())
    data = result.to_dict()
    assert data["baseline"] == "structured_form"
    assert isinstance(data["extracted_facts"], tuple)


def test_keyword_rules_extract_exact_spans():
    content = "客户需要验证多租户能力，同意安排技术评审，下周二进行演示。"
    result = run_keyword_rule_baseline(content, current_state="need_confirmed")
    assert result.accepted is True
    assert result.proposed_state == "solution_eval"
    assert result.evidence_mode == "exact_span"
    for fact in result.extracted_facts:
        assert content[fact["start"]:fact["end"]] == fact["value"]


def test_keyword_rules_missing_required_fact_transfers_to_human():
    result = run_keyword_rule_baseline("客户同意安排技术评审。", current_state="need_confirmed")
    assert result.accepted is False
    assert "customer_need" in result.missing_fact_types


def test_keyword_rules_negation_does_not_create_fact():
    result = run_keyword_rule_baseline("客户尚未同意试用，目前只是内部讨论。", current_state="contacted")
    assert all(fact["type"] != "customer_commitment" for fact in result.extracted_facts)
    assert result.accepted is False


def test_keyword_rules_unknown_text_transfers_to_human():
    result = run_keyword_rule_baseline("客户表示回去再看看。", current_state="contacted")
    assert result.accepted is False
    assert result.proposed_state is None


def test_keyword_rules_won_still_respects_state_machine():
    result = run_keyword_rule_baseline("客户已签合同。", current_state="commercial_negotiation")
    assert result.accepted is True
    assert result.proposed_state == "won"
