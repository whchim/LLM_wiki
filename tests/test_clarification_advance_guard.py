"""重复推进防护测试：上一轮问题未答完时不得再追问。

真实场景：前端每次打开会话都会带 `advance=true`（SalesPage.openSession），GET 带副作用
被重复触发（重复点击/刷新/双标签）就会连续追加两轮内容相同的追问，并把 max_rounds=2
的预算一次烧光、直接把会话推进到 needs_human_review。

修复对应两处：clarification_service._pending_questions 守卫 + db.append_clarification_turn 乐观锁。
"""
from datetime import datetime, timezone

import clarification_service
import customer_state
import db
from sales_clarification_runtime import ModelResponse


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


def _session_with_pending_question() -> dict:
    conversation = customer_state.create_conversation(
        customer_id="guard-cust-1", idempotency_key="guard-idem-1", source_type="meeting_note",
        occurred_at=datetime.now(timezone.utc), submitted_by="sales-1", owner_user_id="sales-1")
    customer_state.add_evidence(conversation["conversation_id"], "客户表示需要进一步确认方案。")
    session = db.create_clarification_session(conversation["conversation_id"], "sales-1")
    db.append_clarification_turn(session["session_id"], "needs_clarification", _output_with_question(), 1)
    return db.get_clarification_session(session["session_id"])


class ExplodingPort:
    """被调用即失败：证明守卫在调模型之前就返回（重复触发不重复计费）。"""
    def complete(self, **kwargs):
        raise AssertionError("存在未答问题时不应调用模型")


class RecordingPort:
    """记录调用次数；返回空 JSON——契约不达标会落 human_review 轮次，无需构造完整契约。"""
    def __init__(self):
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return ModelResponse("{}", "fake-v1", 1, 1, "req-1")


def test_pending_questions_detects_unanswered():
    session = _session_with_pending_question()
    assert [q["id"] for q in clarification_service._pending_questions(session)] == ["question-1"]


def test_pending_questions_empty_after_answer():
    session = _session_with_pending_question()
    turn = session["turns"][-1]
    db.add_clarification_answer(session["session_id"], turn["turn_id"], "question-1", "是，下周二", "sales-1")
    refreshed = db.get_clarification_session(session["session_id"])
    assert clarification_service._pending_questions(refreshed) == []


def test_repeated_advance_is_noop_and_does_not_call_model():
    """重复打开会话（GET ?advance=true 重放）不得重复追问、不得烧掉轮次预算。"""
    session = _session_with_pending_question()
    outcome = clarification_service.advance_session(
        session["session_id"], ExplodingPort(), system_prompt="system")
    assert outcome["advanced"] is False
    assert "未回答" in outcome["reason"]
    assert len(db.list_clarification_turns(session["session_id"])) == 1        # 未新增轮次
    assert db.get_clarification_session(session["session_id"])["status"] == "open"  # 预算未被烧掉


def test_advance_proceeds_after_answer():
    """问题答完后推进是合法的：守卫必须放行，不能把正常流程一起堵死。"""
    session = _session_with_pending_question()
    turn = session["turns"][-1]
    db.add_clarification_answer(session["session_id"], turn["turn_id"], "question-1", "是，下周二", "sales-1")
    port = RecordingPort()
    outcome = clarification_service.advance_session(session["session_id"], port, system_prompt="system")
    assert port.calls >= 1          # 确实调了模型（守卫已放行）
    assert outcome["advanced"] is True
    assert len(db.list_clarification_turns(session["session_id"])) == 2
