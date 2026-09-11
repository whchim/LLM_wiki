"""澄清会话编排：把脱敏证据、模型运行时和持久化轮次连接起来。"""
from __future__ import annotations

from typing import Any

import db
from sales_clarification_runtime import ClarificationRun, ModelPort, run_clarification_agent


def advance_clarification_session(port: ModelPort, *, session_id: str, system_prompt: str) -> ClarificationRun:
    """运行并追加下一轮；失败或预算耗尽后关闭会话转人工。"""
    session = db.get_clarification_session(session_id)
    if session is None:
        raise KeyError("澄清会话不存在")
    if session["status"] != "open":
        raise ValueError("澄清会话已关闭")
    context = db.clarification_context(session_id)
    prior_claims: list[dict[str, Any]] = []
    for turn in session["turns"]:
        prior_claims.extend(turn["agent_output"].get("claims", []))
    answers = {answer["answer_id"]: answer["answer_text_redacted"] for answer in session["answers"]}
    run = run_clarification_agent(
        port, customer_id=context["customer_id"], content_redacted=context["content_redacted"],
        current_state=context["current_state"], prior_claims=prior_claims, answer_contents=answers,
        system_prompt=system_prompt,
    )
    if run.status != "accepted" or run.output is None:
        db.close_clarification_session(session_id, "needs_human_review")
        return run
    db.append_clarification_turn(
        session_id, run.output["stop_reason"], run.output, len(run.output["questions"]),
        run.total_input_tokens, run.total_output_tokens, run.total_latency_ms)
    return run
