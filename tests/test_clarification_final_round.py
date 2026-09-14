"""方案 A 轮次语义测试：max_rounds = 最多**追问**轮数 + 1 次**收尾判定**。

修复背景（实测发现）：旧逻辑在 `turn_no == max_rounds` 生成追问的同时把会话置为
`needs_human_review`，导致**问了却不给答**——用户只经历一轮问答，却被判定"两轮用尽转人工"。

新语义：
- 最后一轮追问生成后会话保持 `open`，这一轮的问题**可以回答**；
- 该轮问题全部答完后，允许一次**收尾判定**（只出结论、不得再追问）；
- 服务端对收尾轮做确定性兜底：模型若仍返回追问 → 强制改写为 insufficient_evidence。
"""
import json
from datetime import datetime, timezone

import clarification_service
import customer_state
import db
from sales_clarification_runtime import ModelResponse

CONTENT = "客户确认正在评估方案，销售将在下周跟进预算反馈。"


def _question_output() -> dict:
    """合法契约输出：needs_clarification + 1 个追问。"""
    return {
        "schema_version": "clarification.v1", "claims": [],
        "missing_facts": [{"id": "missing-1", "type": "next_step", "priority": "high",
                           "why_needed": "判断是否进入方案评估", "impact_states": ["solution_eval"]}],
        "questions": [{"id": "question-1", "missing_fact_id": "missing-1",
                       "question": "下一步是否已约定？", "answer_type": "yes_no"}],
        "stop_reason": "needs_clarification", "can_propose": False,
    }


def _insufficient_output() -> dict:
    """合法契约输出：证据不足结论。"""
    return {
        "schema_version": "clarification.v1", "claims": [],
        "missing_facts": [{"id": "missing-1", "type": "next_step", "priority": "high",
                           "why_needed": "判断是否进入方案评估", "impact_states": ["solution_eval"]}],
        "questions": [], "stop_reason": "insufficient_evidence", "can_propose": False,
    }


def _ready_output() -> dict:
    """合法契约输出：可生成建议（ready_for_proposal 至少需要一条事实声明）。"""
    quote = CONTENT[:6]
    return {
        "schema_version": "clarification.v1",
        "claims": [{"id": "claim-1", "type": "next_step", "attribution": "customer_quote",
                    "certainty": "explicit", "value": quote,
                    "evidence": [{"source": "initial_note", "quote": quote}]}],
        "missing_facts": [], "questions": [], "stop_reason": "ready_for_proposal", "can_propose": True,
    }


class ScriptedPort:
    """按脚本返回模型输出（不访问网络）。"""

    def __init__(self, outputs: list[dict]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._outputs.pop(0) if self._outputs else {}
        return ModelResponse(json.dumps(payload, ensure_ascii=False), "fake-v1", 10, 8, "req-1")


def _session() -> dict:
    conversation = customer_state.create_conversation(
        customer_id="final-cust-1", idempotency_key="final-idem-1", source_type="meeting_note",
        occurred_at=datetime.now(timezone.utc), submitted_by="sales-1", owner_user_id="sales-1")
    customer_state.add_evidence(conversation["conversation_id"], CONTENT)
    return db.create_clarification_session(conversation["conversation_id"], "sales-1")


def _advance(session_id: str, port) -> dict:
    return clarification_service.advance_session(session_id, port, system_prompt="system")


def _answer_latest_turn(session_id: str, turn_id: str) -> None:
    db.add_clarification_answer(session_id, turn_id, "question-1", "是，下周二演示", "sales-1")


def test_last_question_round_is_answerable():
    """最后一轮追问（turn_no == max_rounds）不得再"问了不给答"。"""
    session = _session()
    port = ScriptedPort([_question_output(), _question_output()])

    first = _advance(session["session_id"], port)
    assert first["advanced"] is True and first["session_status"] == "open"
    _answer_latest_turn(session["session_id"], first["turn"]["turn_id"])

    second = _advance(session["session_id"], port)
    assert second["questions"], "第 2 轮应当有追问"
    assert second["session_status"] == "open", "最后一轮追问必须保持可回答"

    # 关键回归：这一轮的问题真的能答（旧逻辑此处会话已 closed → 409）
    answered = db.add_clarification_answer(
        session["session_id"], second["turn"]["turn_id"], "question-1", "是，下周二演示", "sales-1")
    assert answered["question_id"] == "question-1"
    assert db.get_clarification_session(session["session_id"])["round_count"] == 2


def test_conclusion_run_coerces_questions_to_insufficient():
    """收尾轮模型仍想追问 → 服务端强制改写为 insufficient_evidence（不指望模型自律）。"""
    session = _session()
    port = ScriptedPort([_question_output(), _question_output(), _question_output()])

    first = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], first["turn"]["turn_id"])
    second = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], second["turn"]["turn_id"])

    conclusion = _advance(session["session_id"], port)   # 收尾判定轮
    assert conclusion["advanced"] is True
    assert conclusion["audit"]["conclusion_only"] is True
    assert conclusion["audit"]["conclusion_coerced"] is True
    assert conclusion["turn"]["status"] == "insufficient_evidence"
    assert conclusion["questions"] == []
    assert conclusion["session_status"] == "needs_human_review"

    stored = db.list_clarification_turns(session["session_id"])[-1]["agent_output"]
    assert stored["conclusion_coerced"] is True and stored["questions"] == []


def test_conclusion_run_can_conclude_ready_for_proposal():
    """收尾轮给出结论（证据充分）→ 会话进入可生成建议，不做改写。"""
    session = _session()
    port = ScriptedPort([_question_output(), _question_output(), _ready_output()])

    first = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], first["turn"]["turn_id"])
    second = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], second["turn"]["turn_id"])

    conclusion = _advance(session["session_id"], port)
    assert conclusion["turn"]["status"] == "ready_for_proposal"
    assert conclusion["session_status"] == "ready_for_proposal"
    assert "conclusion_coerced" not in conclusion["audit"]


def test_no_agent_run_after_conclusion():
    """收尾判定用掉后，不再有 Agent 运行预算（上限 = max_rounds + 1）。"""
    session = _session()
    port = ScriptedPort([_question_output(), _question_output(), _insufficient_output(), _question_output()])

    first = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], first["turn"]["turn_id"])
    second = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], second["turn"]["turn_id"])
    conclusion = _advance(session["session_id"], port)
    assert conclusion["turn"]["status"] == "insufficient_evidence"

    blocked = _advance(session["session_id"], port)
    assert blocked["advanced"] is False
    # 会话已转人工 → 状态守卫先拦住；更硬的证据是模型根本没有被再调用
    assert "不可推进" in blocked["reason"]
    assert len(port.calls) == 3, "收尾判定之后不得再调用模型"
    assert db.get_clarification_session(session["session_id"])["status"] == "needs_human_review"


def test_answer_in_earlier_turn_does_not_mark_later_turn_answered():
    """跨轮 `question_id` 会重复（每轮都从 question-1 编号）：回答必须按轮作用域比对。

    实测 bug：第 1 轮答过 question-1 后，第 2 轮的 question-1 被误判为已答 →
    未答问题被跳过、直接触发收尾判定（用户视角："我只答了一轮却被当成答完了"）。
    """
    session = _session()
    port = ScriptedPort([_question_output(), _question_output(), _insufficient_output()])

    first = _advance(session["session_id"], port)
    _answer_latest_turn(session["session_id"], first["turn"]["turn_id"])   # 第 1 轮 question-1 已答

    second = _advance(session["session_id"], port)                          # 第 2 轮同样提出 question-1
    assert second["questions"], "第 2 轮应有追问"

    # 第 2 轮的 question-1 尚未回答 → 不得推进（旧逻辑因跨轮同名而误判已答）
    blocked = _advance(session["session_id"], port)
    assert blocked["advanced"] is False
    assert "未回答" in blocked["reason"]
    assert len(port.calls) == 2, "问题未答完不得调用模型"


def test_prompt_marks_conclusion_round():
    """收尾轮的 user prompt 必须显式声明"不得再追问"。"""
    from sales_clarification_runtime import build_user_prompt

    normal = build_user_prompt(customer_id="c-1", content_redacted=CONTENT, current_state=None)
    final = build_user_prompt(customer_id="c-1", content_redacted=CONTENT, current_state=None,
                              conclusion_only=True)
    assert "收尾判定轮" not in normal
    assert "收尾判定轮" in final and "questions 必须为空数组" in final
