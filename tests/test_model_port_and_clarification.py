"""模型适配器与澄清编排的测试（不依赖真实模型服务，也不写真实数据库）。

覆盖三件在真实运行中踩到的事：
1. 证据偏移由服务端定位（模型不数位置）
2. 模型自造 source 别名（answer-1）要被纠正到真实 question_id
3. 编排层：模型失败/契约失败 → 落 human_review，且不伪造 claims
"""
from __future__ import annotations

import json

import pytest

from clarification_schema import locate_quote, validate_clarification_output
from sales_clarification_runtime import (
    ModelResponse,
    build_user_prompt,
    normalize_evidence_offsets,
    run_clarification_agent,
)

NOTE = "客户明确表示需要在本季度完成方案评估，并要求下周提供技术对照材料。"


def _output(**over):
    base = {
        "schema_version": "clarification.v1",
        "claims": [{
            "id": "claim-1", "type": "customer_need",
            "attribution": "customer_quote", "certainty": "explicit",
            "value": "客户要求本季度完成方案评估",
            "evidence": [{"source": "initial_note", "quote": "需要在本季度完成方案评估"}],
        }],
        "missing_facts": [], "questions": [],
        "stop_reason": "ready_for_proposal", "can_propose": True,
        "model_version": "test-model", "prompt_version": "sales-clarification-v1",
    }
    base.update(over)
    return base


# ---- 服务端定位 ----

def test_locate_quote_returns_codepoint_offsets():
    start, end = locate_quote(NOTE, "本季度完成方案评估")
    assert NOTE[start:end] == "本季度完成方案评估"
    assert (start, end) == (NOTE.index("本季度完成方案评估"), NOTE.index("本季度完成方案评估") + 9)


def test_locate_quote_missing_returns_none():
    assert locate_quote(NOTE, "不存在的片段") is None
    assert locate_quote("", "x") is None
    assert locate_quote(NOTE, "") is None


def test_offset_is_filled_server_side_without_model_input():
    """模型只给 source+quote（无 start/end）时，契约仍应通过，且偏移被补齐。"""
    parsed = _output()
    assert "start" not in parsed["claims"][0]["evidence"][0]
    normalize_evidence_offsets(parsed, {"initial_note": NOTE})
    ev = parsed["claims"][0]["evidence"][0]
    assert NOTE[ev["start"]:ev["end"]] == ev["quote"]
    assert validate_clarification_output(parsed, content_redacted=NOTE) == []


def test_offset_is_corrected_when_model_counts_wrong():
    """模型给出的偏移错误时以服务端定位为准（不再是违例）。"""
    parsed = _output()
    parsed["claims"][0]["evidence"][0].update({"start": 0, "end": 1})
    normalize_evidence_offsets(parsed, {"initial_note": NOTE})
    ev = parsed["claims"][0]["evidence"][0]
    assert (ev["start"], ev["end"]) == locate_quote(NOTE, ev["quote"])


def test_quote_not_in_source_still_fails_contract():
    """quote 不是原文片段时必须报错——服务端定位不做"猜"的兜底。"""
    parsed = _output()
    parsed["claims"][0]["evidence"][0]["quote"] = "客户答应下周签约"
    normalize_evidence_offsets(parsed, {"initial_note": NOTE})
    errors = validate_clarification_output(parsed, content_redacted=NOTE)
    assert any("无法精确定位" in e for e in errors)


# ---- source 别名纠正 ----

def test_answer_alias_resolved_to_question_id():
    """模型写成 answer-1 时，按内容反查纠正为真实 question_id。"""
    answer = "客户确认收到材料后立即启动评估。"
    parsed = _output(
        claims=[{
            "id": "claim-1", "type": "customer_commitment",
            "attribution": "customer_quote", "certainty": "explicit",
            "value": "客户确认启动评估",
            "evidence": [{"source": "answer-1", "quote": "立即启动评估"}],
        }])
    contents = {"initial_note": NOTE, "question-1": answer}
    normalize_evidence_offsets(parsed, contents)
    ev = parsed["claims"][0]["evidence"][0]
    assert ev["source"] == "question-1"
    assert answer[ev["start"]:ev["end"]] == ev["quote"]
    assert validate_clarification_output(
        parsed, content_redacted=NOTE, answer_contents={"question-1": answer}) == []


def test_unknown_source_alias_is_not_silently_accepted():
    """无法对应到任何已答问题时，别名不改写，交由契约报错。"""
    parsed = _output(
        claims=[{
            "id": "claim-1", "type": "customer_need",
            "attribution": "customer_quote", "certainty": "explicit", "value": "x",
            "evidence": [{"source": "answer-9", "quote": "本季度"}],
        }])
    normalize_evidence_offsets(parsed, {"initial_note": NOTE, "question-1": "另一段回答"})
    errors = validate_clarification_output(
        parsed, content_redacted=NOTE, answer_contents={"question-1": "另一段回答"})
    assert any("不在可引用内容范围内" in e for e in errors)


# ---- user prompt 明确可引用来源 ----

def test_user_prompt_lists_allowed_sources():
    prompt = build_user_prompt(
        customer_id="c1", content_redacted=NOTE, current_state=None,
        answer_contents={"question-1": "回答文本"}, allowed_sources=["initial_note", "question-1"])
    assert "allowed_evidence_sources" in prompt
    assert "question-1" in prompt
    assert "initial_note" in prompt


# ---- 编排层（假端口，不联网） ----

class FakePort:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, *, system_prompt, user_prompt, max_tokens):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_run_agent_accepts_output_after_server_normalization():
    port = FakePort([ModelResponse(json.dumps(_output(), ensure_ascii=False), "fake-v1", 10, 20)])
    run = run_clarification_agent(port, customer_id="c1", content_redacted=NOTE,
                                  system_prompt="sys", max_retries=0)
    assert run.status == "accepted"
    ev = run.output["claims"][0]["evidence"][0]
    assert NOTE[ev["start"]:ev["end"]] == ev["quote"]


def test_run_agent_marks_needs_human_review_on_persistent_contract_failure():
    bad = _output()
    bad["claims"][0]["evidence"][0]["quote"] = "原文里没有这句"
    port = FakePort([
        ModelResponse(json.dumps(bad, ensure_ascii=False), "fake-v1"),
        ModelResponse(json.dumps(bad, ensure_ascii=False), "fake-v1"),
    ])
    run = run_clarification_agent(port, customer_id="c1", content_redacted=NOTE,
                                  system_prompt="sys")
    assert run.status == "needs_human_review"
    assert run.output is None
    assert len(run.attempts) == 2
    assert all(a.status == "contract_error" for a in run.attempts)


def test_run_agent_retries_once_then_accepts():
    bad = _output()
    bad["claims"][0]["evidence"][0]["quote"] = "不存在"
    port = FakePort([
        ModelResponse(json.dumps(bad, ensure_ascii=False), "fake-v1"),
        ModelResponse(json.dumps(_output(), ensure_ascii=False), "fake-v1"),
    ])
    run = run_clarification_agent(port, customer_id="c1", content_redacted=NOTE, system_prompt="sys")
    assert run.status == "accepted"
    assert [a.status for a in run.attempts] == ["contract_error", "accepted"]
