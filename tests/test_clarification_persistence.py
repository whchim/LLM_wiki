"""阶段 4 会话持久化测试：使用真实 PostgreSQL，单测追加、幂等和轮次门禁。"""
from datetime import datetime, timezone

import pytest

import customer_state
import db


def _output_with_question() -> dict:
    return {
        "schema_version": "clarification.v1",
        "claims": [],
        "missing_facts": [{
            "id": "missing-1", "type": "next_step", "priority": "high",
            "why_needed": "判断是否进入方案评估", "impact_states": ["solution_eval"],
        }],
        "questions": [{
            "id": "question-1", "missing_fact_id": "missing-1",
            "question": "下一步是否已约定？", "answer_type": "yes_no",
        }],
        "stop_reason": "needs_clarification", "can_propose": False,
        "model_version": "test-model", "prompt_version": "sales-clarification-v1",
    }


def _conversation() -> dict:
    conversation = customer_state.create_conversation(
        customer_id="clar-cust-1", idempotency_key="clar-idem-1", source_type="meeting_note",
        occurred_at=datetime.now(timezone.utc), submitted_by="sales-1", owner_user_id="sales-1",
    )
    customer_state.add_evidence(conversation["conversation_id"], "客户表示需要进一步确认方案。")
    return conversation


def test_session_turn_answer_idempotency_and_round_limit():
    conversation = _conversation()
    first = db.create_clarification_session(conversation["conversation_id"], "sales-1")
    second = db.create_clarification_session(conversation["conversation_id"], "sales-1")
    assert second["session_id"] == first["session_id"]
    assert db.can_access_conversation(conversation["conversation_id"], "sales-1") is True
    assert db.can_access_clarification_session(first["session_id"], "sales-1") is True

    turn = db.append_clarification_turn(first["session_id"], "needs_clarification", _output_with_question(), 1, 10, 8, 12)
    answer = db.add_clarification_answer(first["session_id"], turn["turn_id"], "question-1", "是，下周二安排演示", "sales-1")
    duplicate = db.add_clarification_answer(first["session_id"], turn["turn_id"], "question-1", "是，下周二安排演示", "sales-1")
    assert duplicate["answer_id"] == answer["answer_id"]

    with pytest.raises(ValueError, match="question_id"):
        db.add_clarification_answer(first["session_id"], turn["turn_id"], "question-unknown", "回答", "sales-1")

    second_turn = db.append_clarification_turn(first["session_id"], "needs_clarification", _output_with_question(), 1, 10, 8, 12)
    assert second_turn["turn_no"] == 2
    session = db.get_clarification_session(first["session_id"])
    assert session["status"] == "needs_human_review"
    with pytest.raises(ValueError, match="已关闭"):
        db.add_clarification_answer(first["session_id"], second_turn["turn_id"], "question-1", "回答", "sales-1")
