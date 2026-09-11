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

from clarification_schema import parse_clarification_output, validate_clarification_output

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
) -> str:
    """构造最小脱敏上下文；不接受精确敏感数值或原始客户身份。"""
    if not isinstance(content_redacted, str) or not content_redacted.strip():
        raise ValueError("content_redacted 不能为空")
    if len(content_redacted) > DEFAULT_MAX_INPUT_CHARS:
        raise ValueError(f"content_redacted 超过 {DEFAULT_MAX_INPUT_CHARS} 字符")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id 不能为空")
    claims_json = json.dumps(prior_claims or [], ensure_ascii=False, separators=(",", ":"))
    answers_json = json.dumps(answer_contents or {}, ensure_ascii=False, separators=(",", ":"))
    return (
        f"customer_id={customer_id.strip()}\n"
        f"current_state={current_state or 'none'}\n"
        f"initial_note={content_redacted}\n"
        f"prior_claims={claims_json}\n"
        f"clarification_answers={answers_json}\n"
        "请严格按 system prompt 输出 JSON，不要输出 Markdown。"
    )


def run_clarification_agent(
    port: ModelPort,
    *,
    customer_id: str,
    content_redacted: str,
    current_state: str | None = None,
    prior_claims: list[Mapping[str, Any]] | None = None,
    answer_contents: Mapping[str, str] | None = None,
    system_prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_attempt: Callable[[RuntimeAttempt], None] | None = None,
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
