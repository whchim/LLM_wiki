"""销售状态 Agent 输出的解析、校验和落建议适配器。

Agent 的边界止于 ``StateProposal``：本模块可以把合法输出写成建议，
但不会调用确认接口，也不会修改 ``CurrentState``。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import customer_state

ALLOWED_FIELDS = {
    "customer_id", "current_state", "proposed_state", "decision", "confidence",
    "evidence", "reasoning_summary", "next_action", "valid_until",
    "needs_human_confirmation", "risk_flags", "model_version", "prompt_version",
}
DECISIONS = {"propose", "needs_review", "reject"}
RISK_FLAGS = {
    "low_confidence", "conflicting_signals", "missing_strong_evidence",
    "customer_identity_uncertain", "sensitive_content_detected",
}
MAX_SUMMARY_CHARS = 500
MAX_ACTION_CHARS = 500
MIN_HUMAN_CONFIDENCE = 0.75


def _parse_time(value: Any, field: str, errors: list[str]) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{field} 必须是 ISO-8601 字符串")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field} 不是合法 ISO-8601 时间")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{field} 必须包含时区")
        return None
    return parsed.astimezone(timezone.utc)


def validate_state_agent_output(output: Mapping[str, Any], *, customer_id: str,
                                content_redacted: str,
                                current_state: str | None = None) -> list[str]:
    """验证 Agent 输出；返回错误列表，空列表表示可创建 StateProposal。"""
    errors: list[str] = []
    if not isinstance(output, Mapping):
        return ["Agent 输出必须是 JSON 对象"]
    unknown = set(output) - ALLOWED_FIELDS
    if unknown:
        errors.append(f"Agent 输出含越界字段：{sorted(unknown)}")

    if output.get("customer_id") != customer_id:
        errors.append("customer_id 与当前洽谈不一致")
    proposed = output.get("proposed_state")
    if proposed not in customer_state.STATES or proposed == "expired":
        errors.append("proposed_state 不在允许的业务状态集合内")
    declared_current = output.get("current_state")
    if declared_current is not None and declared_current not in customer_state.STATES:
        errors.append("current_state 不在允许的业务状态集合内")
    if declared_current != current_state:
        errors.append("current_state 与系统当前投影不一致")
    if (proposed in customer_state.STATES and proposed != "expired"
            and declared_current == current_state):
        # 在写 StateProposal 前先做同一套状态机校验，避免把确定性错误交给人工队列。
        try:
            customer_state.validate_transition(current_state, proposed)
        except ValueError as exc:
            errors.append(str(exc))

    decision = output.get("decision")
    if decision not in DECISIONS:
        errors.append("decision 必须是 propose、needs_review 或 reject")
    confidence = output.get("confidence")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not 0 <= confidence <= 1):
        errors.append("confidence 必须是 0 到 1 之间的数字")

    evidence = output.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence 至少需要一条可定位证据")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                errors.append(f"evidence[{index}] 必须是对象")
                continue
            quote = item.get("quote")
            start = item.get("start")
            end = item.get("end")
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"evidence[{index}].quote 不能为空")
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 0 or end <= start or end > len(content_redacted)):
                errors.append(f"evidence[{index}] 的 start/end 越界或非法")
            elif isinstance(quote, str) and content_redacted[start:end] != quote:
                errors.append(f"evidence[{index}] 无法精确定位到脱敏原文")
            if not isinstance(item.get("meaning"), str) or not item["meaning"].strip():
                errors.append(f"evidence[{index}].meaning 不能为空")

    for field, limit in (("reasoning_summary", MAX_SUMMARY_CHARS), ("next_action", MAX_ACTION_CHARS)):
        value = output.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} 必须是非空字符串")
        elif len(value) > limit:
            errors.append(f"{field} 超过 {limit} 字符")

    valid_until = _parse_time(output.get("valid_until"), "valid_until", errors)
    if valid_until is not None and valid_until <= datetime.now(timezone.utc):
        errors.append("valid_until 必须晚于当前时间")

    human = output.get("needs_human_confirmation")
    if not isinstance(human, bool):
        errors.append("needs_human_confirmation 必须是布尔值")
    flags = output.get("risk_flags")
    if not isinstance(flags, list) or not all(isinstance(flag, str) and flag in RISK_FLAGS for flag in flags):
        errors.append("risk_flags 必须是预定义风险标识数组")
    elif isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        if confidence < MIN_HUMAN_CONFIDENCE and "low_confidence" not in flags:
            errors.append("低置信度输出必须包含 low_confidence 风险标识")
    if proposed == "won" and human is not True:
        errors.append("won 必须进入人工确认")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < MIN_HUMAN_CONFIDENCE and human is not True:
        errors.append("低置信度输出必须进入人工确认")
    if not isinstance(output.get("model_version"), str) or not output["model_version"].strip():
        errors.append("model_version 必填")
    if not isinstance(output.get("prompt_version"), str) or not output["prompt_version"].strip():
        errors.append("prompt_version 必填")
    return errors


def parse_state_agent_output(raw: str) -> dict:
    """只接受纯 JSON 对象，拒绝 Markdown 代码围栏和 JSON 后拼接文本。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Agent 输出为空")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Agent 输出不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Agent 输出必须是 JSON 对象")
    return value


def create_proposal_from_agent_output(raw_output: str | Mapping[str, Any], *,
                                      conversation_id: str, customer_id: str,
                                      content_redacted: str,
                                      current_state: str | None = None) -> str:
    """校验并创建建议；任何校验错误都会在写库前失败。"""
    output = parse_state_agent_output(raw_output) if isinstance(raw_output, str) else dict(raw_output)
    errors = validate_state_agent_output(
        output, customer_id=customer_id, content_redacted=content_redacted,
        current_state=current_state)
    if errors:
        raise ValueError("Agent 输出校验失败：" + "；".join(errors))
    refs = [
        {"quote": item["quote"], "start": item["start"], "end": item["end"], "meaning": item["meaning"]}
        for item in output["evidence"]
    ]
    valid_until = _parse_time(output.get("valid_until"), "valid_until", [])
    return customer_state.create_proposal(
        conversation_id=conversation_id,
        proposed_state=output["proposed_state"],
        confidence=float(output["confidence"]),
        evidence_refs=refs,
        current_state=current_state,
        decision=output["decision"],
        reasoning_summary=output["reasoning_summary"],
        next_action=output["next_action"],
        valid_until=valid_until,
        risk_flags=output["risk_flags"],
        model_version=output["model_version"],
        prompt_version=output["prompt_version"],
    )
