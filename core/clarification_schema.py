"""销售事实澄清 Agent 的阶段 1 契约校验。

本模块只判断模型输出是否符合业务边界，不调用模型、不写数据库，也不改变客户状态。
状态写入仍由既有 customer_state 状态机和负责人决定接口负责。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

import customer_state

SCHEMA_VERSION = "clarification.v1"

# 第一版只保留能影响销售状态的事实类型，避免把 Agent 变成开放世界的信息抽取器。
FACT_TYPES = {
    "customer_need",
    "customer_commitment",
    "objection",
    "decision_maker",
    "timeline",
    "next_step",
    "competitor_signal",
}
ATTRIBUTIONS = {"customer_quote", "salesperson_interpretation", "external_fact", "unknown"}
CERTAINTIES = {"explicit", "ambiguous", "unknown"}
ANSWER_TYPES = {"yes_no", "short_text", "date", "choice"}
STOP_REASONS = {"ready_for_proposal", "needs_clarification", "insufficient_evidence", "human_review"}
PRIORITIES = {"high", "medium", "low"}
MAX_CLAIMS = 20
MAX_MISSING_FACTS = 10
MAX_QUESTIONS = 2
MAX_TEXT_CHARS = 500

# 追问不得把精确敏感数值重新引入模型下游或让销售提交非必要机密。
FORBIDDEN_SENSITIVE_REQUEST = re.compile(
    r"(?:银行卡|身份证|手机号|密码|密钥|token|合同原件|精确金额|具体报价|详细预算|账户信息)", re.I
)


def _required_str(value: Any, field: str, errors: list[str], *, max_chars: int = MAX_TEXT_CHARS) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} 必须是非空字符串")
    elif len(value) > max_chars:
        errors.append(f"{field} 超过 {max_chars} 字符")


def _enum(value: Any, allowed: set[str], field: str, errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"{field} 非法值：{value!r}")


def _unique_ids(items: Any, field: str, errors: list[str]) -> set[str]:
    ids: set[str] = set()
    if not isinstance(items, list):
        errors.append(f"{field} 必须是数组")
        return ids
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append(f"{field}[{index}] 必须是对象")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{field}[{index}].id 必须是非空字符串")
        elif item_id in ids:
            errors.append(f"{field} 含重复 id：{item_id}")
        else:
            ids.add(item_id)
    return ids


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """在可引用内容中定位片段，返回 (start, end)；找不到返回 None。

    偏移由**服务端**计算，而不是让模型数——模型不擅长精确字符定位（尤其中文），
    通用模型的字符级偏移一致率很低，把这件事交给模型只会稳定地产生契约违例。
    模型只需保证 quote 是原文的**连续片段**，定位交给这里。
    同一片段出现多次时取首次出现（证据只需可核验，不要求唯一）。
    """
    if not isinstance(text, str) or not isinstance(quote, str) or not quote:
        return None
    index = text.find(quote)
    if index < 0:
        return None
    return index, index + len(quote)


def _validate_evidence(
    evidence: Any,
    *,
    field: str,
    contents: Mapping[str, str],
    errors: list[str],
) -> None:
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{field} 至少需要一条证据")
        return
    for index, item in enumerate(evidence):
        prefix = f"{field}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} 必须是对象")
            continue
        source = item.get("source", "initial_note")
        if source not in contents:
            errors.append(f"{prefix}.source 不在可引用内容范围内")
            continue
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"{prefix}.quote 必须是非空字符串")
            continue
        # 以服务端定位为准：模型给的 start/end 仅作参考，不参与判定
        located = locate_quote(contents[source], quote)
        if located is None:
            errors.append(f"{prefix} 无法精确定位到脱敏原文")
            continue
        start, end = item.get("start"), item.get("end")
        if start is not None or end is not None:
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)):
                errors.append(f"{prefix} 的 start/end 必须为整数（可省略，由服务端计算）")
            elif (start, end) != located:
                # 定位成功但与模型所给偏移不一致：以服务端为准并提示，不算违例
                errors.append(f"{prefix} 的 start/end 与原文不一致（服务端定位为 {located}）")


def validate_clarification_output(
    output: Mapping[str, Any],
    *,
    content_redacted: str,
    current_state: str | None = None,
    answer_contents: Mapping[str, str] | None = None,
) -> list[str]:
    """验证事实提取与追问结果，返回错误列表；空列表表示契约合法。"""
    errors: list[str] = []
    if not isinstance(output, Mapping):
        return ["澄清输出必须是 JSON 对象"]

    if output.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {SCHEMA_VERSION}")
    _enum(output.get("stop_reason"), STOP_REASONS, "stop_reason", errors)
    model_version = output.get("model_version")
    prompt_version = output.get("prompt_version")
    _required_str(model_version, "model_version", errors, max_chars=100)
    _required_str(prompt_version, "prompt_version", errors, max_chars=100)

    contents = {"initial_note": content_redacted}
    if answer_contents:
        contents.update({str(k): v for k, v in answer_contents.items() if isinstance(v, str)})

    claims = output.get("claims")
    claim_ids = _unique_ids(claims, "claims", errors)
    if isinstance(claims, list) and len(claims) > MAX_CLAIMS:
        errors.append(f"claims 最多 {MAX_CLAIMS} 条")
    if isinstance(claims, list):
        for index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                continue
            prefix = f"claims[{index}]"
            _enum(claim.get("type"), FACT_TYPES, f"{prefix}.type", errors)
            _enum(claim.get("attribution"), ATTRIBUTIONS, f"{prefix}.attribution", errors)
            _enum(claim.get("certainty"), CERTAINTIES, f"{prefix}.certainty", errors)
            _required_str(claim.get("value"), f"{prefix}.value", errors)
            _validate_evidence(claim.get("evidence"), field=f"{prefix}.evidence", contents=contents, errors=errors)
            if claim.get("attribution") == "customer_quote" and claim.get("certainty") == "unknown":
                errors.append(f"{prefix}: customer_quote 不能同时标为 unknown")

    missing_facts = output.get("missing_facts")
    missing_ids = _unique_ids(missing_facts, "missing_facts", errors)
    if isinstance(missing_facts, list) and len(missing_facts) > MAX_MISSING_FACTS:
        errors.append(f"missing_facts 最多 {MAX_MISSING_FACTS} 条")
    if isinstance(missing_facts, list):
        for index, missing in enumerate(missing_facts):
            if not isinstance(missing, Mapping):
                continue
            prefix = f"missing_facts[{index}]"
            _enum(missing.get("type"), FACT_TYPES, f"{prefix}.type", errors)
            _enum(missing.get("priority"), PRIORITIES, f"{prefix}.priority", errors)
            _required_str(missing.get("why_needed"), f"{prefix}.why_needed", errors)
            impacts = missing.get("impact_states")
            if not isinstance(impacts, list) or not impacts:
                errors.append(f"{prefix}.impact_states 至少需要一个状态")
            elif any(state not in customer_state.STATES or state == "expired" for state in impacts):
                errors.append(f"{prefix}.impact_states 含非法或 expired 状态")

    questions = output.get("questions")
    if not isinstance(questions, list):
        errors.append("questions 必须是数组")
        questions = []
    if len(questions) > MAX_QUESTIONS:
        errors.append(f"questions 每轮最多 {MAX_QUESTIONS} 个")
    question_ids = _unique_ids(questions, "questions", errors)
    for index, question in enumerate(questions):
        if not isinstance(question, Mapping):
            continue
        prefix = f"questions[{index}]"
        _required_str(question.get("question"), f"{prefix}.question", errors, max_chars=300)
        _enum(question.get("answer_type"), ANSWER_TYPES, f"{prefix}.answer_type", errors)
        missing_id = question.get("missing_fact_id")
        if missing_id not in missing_ids:
            errors.append(f"{prefix}.missing_fact_id 必须引用现有 missing_fact")
        if FORBIDDEN_SENSITIVE_REQUEST.search(str(question.get("question", ""))):
            errors.append(f"{prefix}.question 不得索取精确敏感信息")

    stop_reason = output.get("stop_reason")
    if stop_reason == "needs_clarification" and not questions:
        errors.append("stop_reason=needs_clarification 时至少需要一个追问")
    if stop_reason != "needs_clarification" and questions:
        errors.append("只有 stop_reason=needs_clarification 时才能返回 questions")
    can_propose = output.get("can_propose")
    if not isinstance(can_propose, bool):
        errors.append("can_propose 必须是布尔值")
    elif can_propose != (stop_reason == "ready_for_proposal"):
        errors.append("can_propose 必须与 stop_reason=ready_for_proposal 保持一致")
    if stop_reason == "ready_for_proposal" and not claims:
        errors.append("ready_for_proposal 至少需要一条事实声明")
    if current_state not in customer_state.STATES and current_state is not None:
        errors.append("current_state 不在允许的状态集合内")
    return errors


def parse_clarification_output(raw: str) -> dict:
    """只接受纯 JSON 对象，拒绝 Markdown 代码围栏和拼接文本。"""
    import json

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("澄清输出为空")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("澄清输出不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("澄清输出必须是 JSON 对象")
    return value
