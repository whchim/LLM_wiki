"""模型调用适配器（ModelPort 的生产实现）。

设计要点：
- **只做一次调用**：不重试（重试与契约校验在 sales_clarification_runtime 里，避免两处重试叠加）
- **模型调用本身零第三方依赖**：stdlib urllib，与 api/embedding.py 同风格（项目一贯做法）；
  可选的可观测上报走独立模块 `llm_observability`（未配置 LANGFUSE_* 时不 import、不发请求）
- **可替换**：任何 OpenAI 兼容端点（DashScope / DeepSeek / vLLM / Ollama）都能用，
  换供应商只改环境变量，业务代码与契约校验不动
- **不隐瞒 token 消耗**：返回 input/output tokens 供 ClarificationRun 累计审计，
  并在同一处上报给 Langfuse（只上报元数据，不上报正文——正文外发需显式开启）

环境变量：
    MODEL_BASE_URL   默认 https://dashscope.aliyuncs.com/compatible-mode/v1
    MODEL_API_KEY    优先读它；缺省回落到 DASHSCOPE_API_KEY（与 embedding 共用同一 key）
    MODEL_NAME       默认 qwen-plus
    MODEL_TIMEOUT    默认 60 秒
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import llm_observability
from sales_clarification_runtime import ModelResponse


class ModelPortError(Exception):
    """模型调用失败（网络/鉴权/限流/返回体异常）。调用方应转人工，不得静默降级。"""


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
DEFAULT_TIMEOUT = 60


def _config() -> tuple[str, str, str, int]:
    base = os.environ.get("MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or ""
    model = os.environ.get("MODEL_NAME", DEFAULT_MODEL)
    try:
        timeout = int(os.environ.get("MODEL_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return base, key, model, max(5, timeout)


def is_available() -> bool:
    """配置了 key 即视为可用（真实可达性由调用方异常兜底）。"""
    return bool(_config()[1])


class OpenAICompatPort:
    """OpenAI 兼容 chat/completions 适配器。

    用法：
        port = OpenAICompatPort()
        run = run_clarification_agent(port, ..., system_prompt=...)
    """

    def __init__(self, *, model: str | None = None, temperature: float = 0.0):
        self._model_override = model
        # 事实抽取与契约输出要求稳定，默认温度 0；采样自由度越低越容易过 schema
        self._temperature = temperature

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int,
                 operation: str = "chat.completions") -> ModelResponse:
        """调用一次模型；成功/失败都上报一次元数据（未启用 Langfuse 时是纯 no-op）。"""
        started = time.perf_counter()
        try:
            response = self._complete_once(system_prompt=system_prompt, user_prompt=user_prompt,
                                           max_tokens=max_tokens)
        except Exception as exc:
            llm_observability.record_generation(
                model=self._model_override or _config()[2], operation=operation,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}")
            raise
        llm_observability.record_generation(
            model=response.model_version, operation=operation,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            request_id=response.request_id, system_prompt=system_prompt,
            user_prompt=user_prompt, completion=response.raw_text)
        return response

    def _complete_once(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ModelResponse:
        base, key, model, timeout = _config()
        if not key:
            raise ModelPortError("未配置 MODEL_API_KEY / DASHSCOPE_API_KEY，无法调用模型")
        model = self._model_override or model
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": self._temperature,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise ModelPortError(f"模型服务返回 HTTP {e.code}：{detail or e.reason}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise ModelPortError(f"模型调用失败：{type(e).__name__} {e}") from e

        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelPortError(f"模型返回结构异常：{str(payload)[:200]}") from e
        if not isinstance(text, str) or not text.strip():
            raise ModelPortError("模型返回空内容")

        usage = payload.get("usage") or {}
        return ModelResponse(
            raw_text=text,
            model_version=payload.get("model") or model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            request_id=payload.get("id"),
        )


def default_port():
    """按环境变量返回可用的端口；未配置 key 时返回 None（调用方决定是否报错）。"""
    if not is_available():
        return None
    return OpenAICompatPort()
