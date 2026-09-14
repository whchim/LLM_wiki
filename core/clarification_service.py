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


def _pending_questions(session: dict) -> list[dict[str, Any]]:
    """最新一轮中**尚未回答**的问题（重复触发防护的判据）。

    若最新轮次仍是 needs_clarification 且存在未答问题，再推进只会用完全相同的上下文
    重复追问同一批问题（前端重复打开会话、GET 带副作用被重放都会触发），并提前烧掉
    轮次预算——第二轮会立刻把会话推进到 needs_human_review。

    注意字段名差异：问题对象的主键是 `id`，回答表记录的列名是 `question_id`
    （见 db.add_clarification_answer 的校验），两者对应同一取值。

    ⚠️ **回答必须按轮作用域比对**：`question_id` 只在轮内唯一（每轮都从 `question-1`
    开始编号），`clarification_answers` 的唯一约束也是 `(turn_id, question_id)`。
    若拿"整会话的回答"去比对最新轮的问题，第 N 轮答过的 `question-1` 会让第 N+1 轮的
    `question-1` 被误判为已答 → 未答问题被跳过、提前触发收尾判定（实测踩过）。
    """
    turns = session.get("turns") or []
    if not turns:
        return []
    latest = turns[-1]
    if latest.get("status") != "needs_clarification":
        return []
    output = latest.get("agent_output")
    questions = output.get("questions") if isinstance(output, dict) else None
    if not isinstance(questions, list):
        return []
    answered = {item.get("question_id") for item in (session.get("answers") or [])
                if isinstance(item, dict) and item.get("turn_id") == latest.get("turn_id")}
    return [q for q in questions if isinstance(q, dict) and q.get("id") not in answered]


def _append_turn(session_id: str, expected_round_count: int, status: str, output: dict,
                 question_count: int, run: ClarificationRun) -> tuple[dict | None, str | None]:
    """落轮次；轮次被并发/重复请求推进时返回 (None, 原因)，不抛错（可观测但非故障）。"""
    try:
        turn = db.append_clarification_turn(
            session_id, status, output, question_count=question_count,
            input_tokens=run.total_input_tokens or None,
            output_tokens=run.total_output_tokens or None,
            latency_ms=run.total_latency_ms or None,
            expected_round_count=expected_round_count,
        )
        return turn, None
    except ValueError as exc:
        return None, str(exc)


def _coerce_conclusion_output(output: dict) -> tuple[dict, bool]:
    """收尾判定轮的**确定性兜底**：只允许出结论，不允许再追问。

    不指望模型自律——若它仍返回追问或 needs_clarification，一律改写为
    insufficient_evidence 并清空 questions（可观测：标记 conclusion_coerced）。
    """
    questions = output.get("questions") or []
    stop = output.get("stop_reason")
    if not questions and stop in ("ready_for_proposal", "insufficient_evidence"):
        return output, False
    coerced = dict(output)
    coerced["questions"] = []
    coerced["stop_reason"] = "insufficient_evidence"
    coerced["conclusion_coerced"] = True
    return coerced, True


def advance_session(session_id: str, port: ModelPort, *,
                    system_prompt: str | None = None,
                    max_tokens: int | None = None,
                    on_attempt=None) -> dict:
    """推进一轮澄清：调模型 → 契约校验 → 落轮次。返回可审计摘要。

    轮次语义（方案 A）：`max_rounds` = 最多**追问**轮数；追问预算用尽后仍允许
    **一次收尾判定**（只出结论、不得再追问），因此 Agent 运行上限是 max_rounds + 1。

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

    # 重复触发防护：上一轮的问题还没答完就再推进，只会重复追问并烧掉轮次预算。
    pending = _pending_questions(session)
    if pending:
        return {"advanced": False,
                "reason": f"上一轮仍有 {len(pending)} 个问题未回答，不重复推进",
                "status": session.get("status"), "turn": None,
                "questions": [], "claims": 0, "audit": {}}

    round_count = session.get("round_count", 0)
    max_rounds = session.get("max_rounds", 2)
    if round_count > max_rounds:
        return {"advanced": False, "reason": "已达最大澄清轮次（含收尾判定）",
                "status": session.get("status"), "turn": None, "questions": [], "claims": 0, "audit": {}}
    # 追问预算已用尽 → 本次只能是收尾判定（结论轮）
    conclusion_only = round_count == max_rounds

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
        "conclusion_only": conclusion_only,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if on_attempt is not None:
        kwargs["on_attempt"] = on_attempt

    run: ClarificationRun = run_clarification_agent(port, **kwargs)
    audit = run.audit_dict()
    audit["conclusion_only"] = conclusion_only
    # 调模型前读到的轮次：落库时用它做乐观锁，防止并发/重复触发写入重复轮次
    expected_round = session.get("round_count", 0)

    if run.status != "accepted" or not isinstance(run.output, dict):
        # 模型失败或契约不达标：把最后一轮错误落成 human_review（可追溯），不改客户状态
        turn, conflict = _append_turn(
            session_id, expected_round, "human_review",
            {"error": audit.get("errors") or ["模型或契约失败"], "provider_audit": audit},
            0, run)
        if conflict:
            return {"advanced": False, "reason": conflict, "status": session.get("status"),
                    "turn": None, "questions": [], "claims": 0, "audit": audit}
        return {"advanced": True, "reason": "模型或契约失败，已转人工", "status": "needs_human_review",
                "turn": turn, "questions": [], "claims": 0, "audit": audit}

    output = run.output
    if conclusion_only:
        output, coerced = _coerce_conclusion_output(output)
        if coerced:
            audit["conclusion_coerced"] = True
    questions = [q for q in (output.get("questions") or []) if isinstance(q, dict)]
    turn, conflict = _append_turn(
        session_id, expected_round, output["stop_reason"], output, len(questions), run)
    if conflict:
        return {"advanced": False, "reason": conflict, "status": session.get("status"),
                "turn": None, "questions": [], "claims": 0, "audit": audit}
    refreshed = db.get_clarification_session(session_id) or {}
    return {"advanced": True, "reason": None, "status": turn.get("status"),
            "session_status": refreshed.get("status"),
            "turn": turn, "questions": questions,
            "claims": len(output.get("claims") or []), "audit": audit}


def resolve_session(session_id: str, decision: str, actor: str, *,
                    answer_text_redacted: str | None = None,
                    question_id: str | None = None,
                    note: str | None = None) -> dict:
    """人工处置转人工的会话（闭环）：关闭结束，或补充事实后重开继续。

    两种真实转人工场景与对应处置：
    - **模型/契约失败**（最新轮是 human_review，预算未用尽）→ 可 `reopened`
      （重试一轮，瞬时故障的主要出口），也可 `closed`。
    - **最后一轮追问未答完就转人工**（历史数据或人工关闭过）→ 也可 `reopened`：
      重开后能继续回答那一轮的问题，答完即触发**收尾判定**（方案 A：max_rounds 是
      "最多追问轮数"，预算用尽后仍允许一次结论轮）。
    - **收尾判定已完成**（`round_count > max_rounds`）→ 只能 `closed`。

    纪律：本函数只动**会话与回答**，绝不写客户状态——客户事实仍只能经
    state_proposals → 负责人确认 → state_events 写入（SA-02/SA-11 的事实边界）。
    """
    if decision not in {"closed", "reopened"}:
        raise ValueError("decision 只能是 closed 或 reopened")
    session = db.get_clarification_session(session_id)
    if session is None:
        raise KeyError("澄清会话不存在")

    if decision == "closed":
        if not (note or "").strip():
            raise ValueError("关闭会话必须填写原因")
        updated = db.resolve_clarification_session(session_id, "cancelled", actor, note.strip())
        return {"resolved": True, "decision": "closed", "session": updated, "answer": None}

    # reopened：**先校验、后落库**（避免请求被拒但会话已被重开的半成品状态）；
    # 落库顺序必须是"先重开、再记回答"——add_clarification_answer 要求会话为 open。
    answer_text = (answer_text_redacted or "").strip()
    target = None
    if answer_text:
        pending = _pending_questions(session)
        if not pending:
            raise ValueError("当前没有待回答的问题，无需补充事实（重开即重试）")
        if question_id:
            target = next((q for q in pending if q.get("id") == question_id), None)
            if target is None:
                raise ValueError("question_id 不属于当前待回答的问题")
        else:
            target = pending[0]

    updated = db.resolve_clarification_session(session_id, "open", actor, (note or "").strip() or None)
    answer = None
    if answer_text and target is not None:
        answer = db.add_clarification_answer(
            session_id, session["turns"][-1]["turn_id"], target["id"], answer_text, actor)
    return {"resolved": True, "decision": "reopened", "session": updated, "answer": answer}
