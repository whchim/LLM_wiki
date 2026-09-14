"""销售事实澄清 Agent 运行时（阶段 3）。

运行时只调用注入的模型端口并返回经过契约校验的理解结果；它不会写数据库、
创建 StateProposal 或调用负责人确认接口。模型失败和契约失败都保留在内存结果中，
由上层决定转人工或持久化审计记录。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from clarification_schema import locate_quote, parse_clarification_output, validate_clarification_output

PROMPT_VERSION = "sales-clarification-v1"
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_INPUT_CHARS = 12_000
DEFAULT_MAX_OUTPUT_CHARS = 20_000
DEFAULT_MAX_TOKENS = 2_000


class ModelPort(Protocol):
    """最小模型端口：供应商适配器只需实现一次调用。"""

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> "ModelResponse":
        ...


@dataclass(frozen=True)
class ModelResponse:
    raw_text: str
    model_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class RuntimeAttempt:
    attempt: int
    status: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


@dataclass
class ClarificationRun:
    """一次调用的可审计内存结果。不会自动落库。"""

    status: str
    output: dict[str, Any] | None
    errors: list[str]
    attempts: list[RuntimeAttempt] = field(default_factory=list)
    model_version: str | None = None
    prompt_version: str = PROMPT_VERSION
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: int = 0

    @property
    def needs_human_review(self) -> bool:
        return self.status != "accepted"

    def audit_dict(self) -> dict[str, Any]:
        """返回不含原文的成本/失败摘要，供上层写 audit 或 trace。"""
        return {
            "status": self.status,
            "errors": list(self.errors),
            "attempts": [attempt.__dict__.copy() for attempt in self.attempts],
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "latency_ms": self.total_latency_ms,
        }


def build_user_prompt(
    *,
    customer_id: str,
    content_redacted: str,
    current_state: str | None,
    prior_claims: list[Mapping[str, Any]] | None = None,
    answer_contents: Mapping[str, str] | None = None,
    allowed_sources: list[str] | None = None,
    conclusion_only: bool = False,
) -> str:
    """构造最小脱敏上下文；不接受精确敏感数值或原始客户身份。

    conclusion_only=True 表示本次是**收尾判定轮**（追问预算已用尽）：要求模型只给结论，
    不得再提出追问——服务端另有确定性兜底（clarification_service 会把追问强制改写为
    insufficient_evidence），不依赖模型自律。
    """
    if not isinstance(content_redacted, str) or not content_redacted.strip():
        raise ValueError("content_redacted 不能为空")
    if len(content_redacted) > DEFAULT_MAX_INPUT_CHARS:
        raise ValueError(f"content_redacted 超过 {DEFAULT_MAX_INPUT_CHARS} 字符")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id 不能为空")
    claims_json = json.dumps(prior_claims or [], ensure_ascii=False, separators=(",", ":"))
    answers_json = json.dumps(answer_contents or {}, ensure_ascii=False, separators=(",", ":"))
    sources = allowed_sources or ["initial_note", *(answer_contents or {})]
    final_note = (
        "\n【收尾判定轮】追问次数已用尽：questions 必须为空数组；"
        "证据足以支撑状态建议时 stop_reason=ready_for_proposal，否则 stop_reason=insufficient_evidence。"
        if conclusion_only else ""
    )
    return (
        f"customer_id={customer_id.strip()}\n"
        f"current_state={current_state or 'none'}\n"
        f"initial_note={content_redacted}\n"
        f"prior_claims={claims_json}\n"
        f"clarification_answers={answers_json}\n"
        f"allowed_evidence_sources={json.dumps(sources, ensure_ascii=False)}\n"
        "请严格按 system prompt 输出 JSON，不要输出 Markdown。"
        "证据的 source 只能取 allowed_evidence_sources 中的值。"
        f"{final_note}"
    )


def normalize_evidence_offsets(parsed: dict, contents: Mapping[str, str]) -> dict:
    """规范化证据：来源别名纠正 + 服务端定位 start/end。

    两件事都因为"不该指望模型做这些"：
    1. **来源别名**：契约要求 source 取 `initial_note` 或问题 ID（如 question-1），
       但模型常按顺序写成 `answer-1`/`answer_1`/`a1`。若该别名能唯一对应到某条已回答
       问题的文本，就改写为真实 question_id；否则交由契约报错（不静默放行）。
    2. **偏移**：模型只需给 source + quote，start/end 由服务端定位计算。
    就地修改并返回 parsed。
    """
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return parsed
    # 别名 → 真实 key：先按内容反查，再按出现顺序编号
    answer_keys = [k for k in contents if k != "initial_note"]
    text_to_key = {contents[k]: k for k in answer_keys}
    alias_map: dict[str, str] = {}
    for idx, key in enumerate(answer_keys, start=1):
        for alias in (f"answer-{idx}", f"answer_{idx}", f"a{idx}", f"answer{idx}"):
            alias_map[alias] = key

    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, str) and source not in contents:
                # 先尝试按文本内容反查（最稳），再按别名编号
                quote = item.get("quote")
                matched = text_to_key.get(quote) if isinstance(quote, str) else None
                item["source"] = matched or alias_map.get(source.strip().lower(), source)
            text = contents.get(str(item.get("source", "initial_note")))
            quote = item.get("quote")
            if not isinstance(text, str) or not isinstance(quote, str):
                continue
            located = locate_quote(text, quote)
            if located is None:
                continue
            item["start"], item["end"] = located
    return parsed


def run_clarification_agent(
    port: ModelPort,
    *,
    customer_id: str,
    content_redacted: str,
    current_state: str | None = None,
    prior_claims: list[Mapping[str, Any]] | None = None,
    answer_contents: Mapping[str, str] | None = None,
    allowed_sources: list[str] | None = None,
    system_prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_attempt: Callable[[RuntimeAttempt], None] | None = None,
    conclusion_only: bool = False,
) -> ClarificationRun:
    """调用模型并校验输出，失败后最多重试一次，永不写状态。"""
    if max_tokens < 1 or max_tokens > DEFAULT_MAX_TOKENS:
        raise ValueError(f"max_tokens 必须在 1 到 {DEFAULT_MAX_TOKENS} 之间")
    if max_retries < 0 or max_retries > 2:
        raise ValueError("max_retries 必须在 0 到 2 之间")
    user_prompt = build_user_prompt(
        customer_id=customer_id,
        content_redacted=content_redacted,
        current_state=current_state,
        prior_claims=prior_claims,
        answer_contents=answer_contents,
        allowed_sources=allowed_sources,
        conclusion_only=conclusion_only,
    )
    result = ClarificationRun(status="failed", output=None, errors=[])
    for attempt_number in range(1, max_retries + 2):
        started = time.perf_counter()
        try:
            response = port.complete(system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens)
            if not isinstance(response, ModelResponse):
                raise TypeError("ModelPort 必须返回 ModelResponse")
            if not isinstance(response.raw_text, str) or len(response.raw_text) > DEFAULT_MAX_OUTPUT_CHARS:
                raise ValueError(f"模型输出为空或超过 {DEFAULT_MAX_OUTPUT_CHARS} 字符")
            parsed = parse_clarification_output(response.raw_text)
            parsed.setdefault("model_version", response.model_version)
            parsed.setdefault("prompt_version", PROMPT_VERSION)
            # 偏移以服务端定位为准（模型只给 quote），保证落库结果与脱敏原文一致
            normalize_evidence_offsets(parsed, {
                "initial_note": content_redacted,
                **{str(k): v for k, v in (answer_contents or {}).items() if isinstance(v, str)},
            })
            errors = validate_clarification_output(
                parsed, content_redacted=content_redacted, current_state=current_state,
                answer_contents=answer_contents)
            latency_ms = int((time.perf_counter() - started) * 1000)
            attempt = RuntimeAttempt(
                attempt=attempt_number,
                status="accepted" if not errors else "contract_error",
                latency_ms=latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                error="；".join(errors) if errors else None,
            )
            result.attempts.append(attempt)
            if response.input_tokens:
                result.total_input_tokens += response.input_tokens
            if response.output_tokens:
                result.total_output_tokens += response.output_tokens
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
            attempt = RuntimeAttempt(attempt=attempt_number, status="error", latency_ms=latency_ms, error=str(exc))
            result.attempts.append(attempt)
            result.total_latency_ms += latency_ms
            result.errors = [str(exc)]
        if on_attempt:
            on_attempt(result.attempts[-1])
    result.status = "needs_human_review"
    return result
