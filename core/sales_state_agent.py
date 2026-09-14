"""销售状态 Agent 输出的解析、校验、落建议适配器，以及模型运行入口。

Agent 的边界止于 ``StateProposal``：本模块可以把合法输出写成建议，
但不会调用确认接口，也不会修改 ``CurrentState``。

运行入口 ``run_state_agent``：调模型 → 解析 → 按 quote 重定位证据偏移 → 契约校验 →
失败最多重试一次。**永不写库**，是否落建议由服务层决定（并可与确定性规则互为回退）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import customer_state
from sales_clarification_runtime import ModelResponse, locate_quote

DEFAULT_MAX_TOKENS = 1200
DEFAULT_MAX_RETRIES = 1
MAX_OUTPUT_CHARS = 8000
MAX_CLAIMS_CHARS = 4000
PROMPT_NAME = "sales_state_prompt.md"
PROMPT_VERSION = "sales-state-v1"

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
                                content_redacted: str | None = None,
                                current_state: str | None = None,
                                contents: Mapping[str, str] | None = None) -> list[str]:
    """验证 Agent 输出；返回错误列表，空列表表示可创建 StateProposal。

    `contents`：可引用来源 → 文本（`initial_note` 与追问回答）。传入则按证据条目的
    `source` 取对应文本校验偏移；只传 `content_redacted` 时等价于仅允许 `initial_note`（向后兼容）。
    """
    sources: dict[str, str] = dict(contents) if contents else {"initial_note": content_redacted or ""}
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
            source_key = str(item.get("source") or "initial_note")
            text = sources.get(source_key)
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"evidence[{index}].quote 不能为空")
            if text is None:
                errors.append(f"evidence[{index}].source 不在可引用来源内：{source_key}")
            elif (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 0 or end <= start or end > len(text)):
                errors.append(f"evidence[{index}] 的 start/end 越界或非法")
            elif isinstance(quote, str) and text[start:end] != quote:
                errors.append(f"evidence[{index}] 无法精确定位到来源原文（{source_key}）")
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
        {"quote": item["quote"], "start": item["start"], "end": item["end"],
         "meaning": item["meaning"], "source": item.get("source") or "initial_note"}
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


@dataclass
class StateAgentRun:
    """状态 Agent 单次运行结果（与 ClarificationRun 同构，便于服务层统一处理）。"""

    status: str
    output: dict | None = None
    errors: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    model_version: str | None = None
    prompt_version: str = PROMPT_VERSION
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: int = 0

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def audit_dict(self) -> dict:
        """不含原文的成本/失败摘要，供上层写审计或 trace。"""
        return {"status": self.status, "errors": list(self.errors), "attempts": list(self.attempts),
                "model_version": self.model_version, "prompt_version": self.prompt_version,
                "input_tokens": self.total_input_tokens, "output_tokens": self.total_output_tokens,
                "latency_ms": self.total_latency_ms}


def build_state_user_prompt(*, customer_id: str, content_redacted: str, current_state: str | None,
                            claims: list[Mapping[str, Any]] | None = None,
                            answer_texts: Mapping[str, str] | None = None,
                            allowed_sources: list[str] | None = None) -> str:
    """构造状态判定所需的最小脱敏上下文（不含精确金额与原始客户身份）。"""
    if not isinstance(content_redacted, str) or not content_redacted.strip():
        raise ValueError("content_redacted 不能为空")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id 不能为空")
    claims_json = json.dumps(claims or [], ensure_ascii=False, separators=(",", ":"))
    if len(claims_json) > MAX_CLAIMS_CHARS:            # 控制上下文预算，超出则截断声明数量
        claims_json = claims_json[:MAX_CLAIMS_CHARS] + "…(已截断)"
    sources = allowed_sources or ["initial_note", *(answer_texts or {})]
    return (
        f"customer_id={customer_id.strip()}\n"
        f"current_state={current_state or 'none'}\n"
        f"initial_note={content_redacted}\n"
        f"clarification_answers={json.dumps(answer_texts or {}, ensure_ascii=False, separators=(',', ':'))}\n"
        f"confirmed_claims={claims_json}\n"
        f"allowed_sources={json.dumps(sources, ensure_ascii=False)}\n"
        "请严格按 system prompt 输出 JSON 对象：不要输出 Markdown，不要新增字段；"
        "evidence 的 quote 必须与来源原文逐字一致（start/end 由服务端重新定位）。"
    )


def normalize_state_evidence_offsets(parsed: dict, contents: Mapping[str, str]) -> dict:
    """按 quote 在来源文本中重新定位 start/end——模型给的偏移不可信（服务端定位为准）。

    来源取 evidence[i].source（缺省 initial_note）；定位失败则保留原值，交由契约校验拒绝。
    """
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        return parsed
    for item in evidence:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        text = contents.get(str(item.get("source") or "initial_note"))
        if not isinstance(quote, str) or not isinstance(text, str):
            continue
        located = locate_quote(text, quote)
        if located is not None:
            item["start"], item["end"] = located
    return parsed


def _retry_feedback(errors: list[str]) -> str:
    """重试时把服务端拒绝原因回灌给模型——盲重试几乎必然再错一次（实测：从不允许的状态跳转开始）。"""
    detail = "；".join(errors)[:400]
    return ("上一次输出未通过服务端校验，请**只修正下列问题**后重新输出同一个 JSON 对象"
            f"（不要解释、不要输出 Markdown）：{detail}")


def run_state_agent(port, *, customer_id: str, content_redacted: str, current_state: str | None,
                    claims: list[Mapping[str, Any]] | None = None,
                    answer_texts: Mapping[str, str] | None = None,
                    allowed_sources: list[str] | None = None,
                    system_prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                    max_retries: int = DEFAULT_MAX_RETRIES,
                    on_attempt: Callable[[dict], None] | None = None) -> StateAgentRun:
    """调用模型并校验输出，失败最多重试一次；**永不写库**。

    重试不是盲目重复：把上一次的契约错误作为反馈追加进 user prompt，让模型定向修正。
    """
    if max_tokens < 1:
        raise ValueError("max_tokens 必须为正")
    if max_retries < 0 or max_retries > 2:
        raise ValueError("max_retries 必须在 0 到 2 之间")
    contents = {"initial_note": content_redacted, **(answer_texts or {})}
    base_prompt = build_state_user_prompt(
        customer_id=customer_id, content_redacted=content_redacted, current_state=current_state,
        claims=claims, answer_texts=answer_texts, allowed_sources=allowed_sources)
    result = StateAgentRun(status="failed")
    feedback: str | None = None
    for attempt_number in range(1, max_retries + 2):
        user_prompt = base_prompt if feedback is None else f"{base_prompt}\n{feedback}"
        started = time.perf_counter()
        try:
            response = port.complete(system_prompt=system_prompt, user_prompt=user_prompt,
                                     max_tokens=max_tokens)
            if not isinstance(response, ModelResponse):
                raise TypeError("ModelPort 必须返回 ModelResponse")
            if not isinstance(response.raw_text, str) or len(response.raw_text) > MAX_OUTPUT_CHARS:
                raise ValueError(f"模型输出为空或超过 {MAX_OUTPUT_CHARS} 字符")
            parsed = parse_state_agent_output(response.raw_text)
            parsed.setdefault("model_version", response.model_version)
            parsed.setdefault("prompt_version", PROMPT_VERSION)
            normalize_state_evidence_offsets(parsed, contents)
            errors = validate_state_agent_output(parsed, customer_id=customer_id,
                                                current_state=current_state, contents=contents)
            latency_ms = int((time.perf_counter() - started) * 1000)
            attempt = {"attempt": attempt_number,
                       "status": "accepted" if not errors else "contract_error",
                       "latency_ms": latency_ms,
                       "error": "；".join(errors) if errors else None}
            result.attempts.append(attempt)
            result.total_input_tokens += response.input_tokens or 0
            result.total_output_tokens += response.output_tokens or 0
            result.total_latency_ms += latency_ms
            result.model_version = response.model_version
            result.errors = errors
            if not errors:
                result.status = "accepted"
                result.output = parsed
                if on_attempt:
                    on_attempt(attempt)
                return result
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            attempt = {"attempt": attempt_number, "status": "error", "latency_ms": latency_ms,
                       "error": str(exc)}
            result.attempts.append(attempt)
            result.total_latency_ms += latency_ms
            result.errors = [str(exc)]
        if on_attempt:
            on_attempt(result.attempts[-1])
        feedback = _retry_feedback(result.errors)
    result.status = "needs_human_review"
    return result
