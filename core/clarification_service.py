"""销售事实澄清 Agent 的编排：把会话、脱敏上下文、模型端口与轮次持久化串起来。

职责边界（与设计文档一致）：
- 本模块**只**产出一轮 agent 输出并落 clarification_turns
- **不**创建 StateProposal、**不**修改客户状态——状态写入仍属 customer_state 状态机与负责人
- 模型失败或契约失败 → 状态置 human_review（转人工），不静默降级、不伪造结果

为什么需要答案映射：澄清契约要求每条证据能精确定位到"可引用内容"的字符区间，
因此把已答问题按 question_id 组成 {question_id: answer_text} 传给模型，
它才能用 source=<question_id> + start/end 引用某条回答。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import db
from sales_clarification_runtime import (
    ClarificationRun,
    ModelPort,
    run_clarification_agent,
)

PROMPT_NAME = "sales_clarification_prompt.md"
# 一条回答最多 2000 字符（与 db.add_clarification_answer 的上限一致），
# system + user + 回答全量进入上下文，故这里再设一个总预算，避免超长请求。
MAX_ANSWERS_CHARS = 8_000


def _prompt_dir() -> Path:
    """prompts/ 在仓库根（容器内 /app/prompts）。可用 PROMPTS_DIR 覆盖（测试用）。"""
    override = os.environ.get("PROMPTS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "prompts"


def load_system_prompt(name: str = PROMPT_NAME) -> str:
    path = _prompt_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"缺少系统提示词：{path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"系统提示词为空：{path}")
    return text


def _answer_contents(session: dict) -> dict[str, str]:
    """已答问题 → {question_id: answer_text}，供契约校验定位证据。"""
    contents: dict[str, str] = {}
    used = 0
    for answer in session.get("answers") or []:
        qid = answer.get("question_id")
        text = answer.get("answer_text_redacted")
        if not isinstance(qid, str) or not isinstance(text, str):
            continue
        if used + len(text) > MAX_ANSWERS_CHARS:
            break
        contents[qid] = text
        used += len(text)
    return contents


def _prior_claims(session: dict) -> list[dict[str, Any]]:
    """前几轮已提出的事实声明（用于跨轮去重与状态判断）。"""
    claims: list[dict[str, Any]] = []
    for turn in session.get("turns") or []:
        output = turn.get("agent_output")
        if isinstance(output, dict) and isinstance(output.get("claims"), list):
            claims.extend(c for c in output["claims"] if isinstance(c, dict))
    return claims


def advance_session(session_id: str, port: ModelPort, *,
                    system_prompt: str | None = None,
                    max_tokens: int | None = None,
                    on_attempt=None) -> dict:
    """推进一轮澄清：调模型 → 契约校验 → 落轮次。返回可审计摘要。

    返回 dict：
        {"advanced": bool, "reason": str|None, "status": ..., "turn": {...}|None,
         "questions": [...], "claims": int, "audit": {...}}
    """
    session = db.get_clarification_session(session_id)
    if session is None:
        raise KeyError("澄清会话不存在")
    if session.get("status") != "open":
        return {"advanced": False, "reason": f"会话状态为 {session.get('status')}，不可推进",
                "status": session.get("status"), "turn": None, "questions": [], "claims": 0, "audit": {}}
    if session.get("round_count", 0) >= session.get("max_rounds", 2):
        return {"advanced": False, "reason": "已达最大澄清轮次", "status": session.get("status"),
                "turn": None, "questions": [], "claims": 0, "audit": {}}

    context = db.clarification_context(session["session_id"])
    content_redacted = context.get("content_redacted") or ""
    if not content_redacted.strip():
        raise ValueError("脱敏证据为空，无法澄清")

    answers = _answer_contents(session)
    kwargs: dict[str, Any] = {
        "customer_id": context["customer_id"],
        "content_redacted": content_redacted,
        "current_state": context.get("current_state"),
        "prior_claims": _prior_claims(session) or None,
        "answer_contents": answers or None,
        # 把"可引用的 source 键"显式告知模型，避免它自造 answer-* 键名
        "allowed_sources": ["initial_note", *answers.keys()] if answers else ["initial_note"],
        "system_prompt": system_prompt or load_system_prompt(),
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if on_attempt is not None:
        kwargs["on_attempt"] = on_attempt

    run: ClarificationRun = run_clarification_agent(port, **kwargs)
    audit = run.audit_dict()

    if run.status != "accepted" or not isinstance(run.output, dict):
        # 模型失败或契约不达标：把最后一轮错误落成 human_review（可追溯），不改客户状态
        turn = db.append_clarification_turn(
            session_id, "human_review",
            {"error": audit.get("errors") or ["模型或契约失败"], "provider_audit": audit},
            question_count=0,
            input_tokens=run.total_input_tokens or None,
            output_tokens=run.total_output_tokens or None,
            latency_ms=run.total_latency_ms or None,
        )
        return {"advanced": True, "reason": "模型或契约失败，已转人工", "status": "needs_human_review",
                "turn": turn, "questions": [], "claims": 0, "audit": audit}

    output = run.output
    questions = [q for q in (output.get("questions") or []) if isinstance(q, dict)]
    turn = db.append_clarification_turn(
        session_id, output["stop_reason"], output,
        question_count=len(questions),
        input_tokens=run.total_input_tokens or None,
        output_tokens=run.total_output_tokens or None,
        latency_ms=run.total_latency_ms or None,
    )
    refreshed = db.get_clarification_session(session_id) or {}
    return {"advanced": True, "reason": None, "status": turn.get("status"),
            "session_status": refreshed.get("status"),
            "turn": turn, "questions": questions,
            "claims": len(output.get("claims") or []), "audit": audit}
