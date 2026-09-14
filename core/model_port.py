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
import logging
import os
import time
import urllib.error
import urllib.request

import llm_observability
from sales_clarification_runtime import ModelResponse

logger = logging.getLogger("llmwiki.model_port")


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

    def __init__(self, *, model: str | None = None, temperature: float = 0.0,
                 base_url: str | None = None, api_key: str | None = None,
                 purpose: str = "default", tenant_id: str | None = None):
        self._model_override = model
        # 事实抽取与契约输出要求稳定，默认温度 0；采样自由度越低越容易过 schema
        self._temperature = temperature
        # L4：租户级配置（来自 tenant_model_configs）优先于环境变量；None 表示回落 env
        self._base_url_override = base_url
        self._api_key_override = api_key
        self._purpose = purpose
        self._tenant_id = tenant_id

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int,
                 operation: str = "chat.completions") -> ModelResponse:
        """调用一次模型；成功/失败都上报元数据 + 用量记账（未启用时是纯 no-op）。

        调用前做**租户配额拦截**（`tenant_model_configs.daily_token_quota`）：超额直接报错，
        不静默降级——编译侧会落成可重试的失败任务，销售侧会转人工。
        """
        started = time.perf_counter()
        tenant_id = self._tenant_id or _current_tenant()
        if _over_quota(tenant_id, self._purpose):
            raise ModelPortError(
                f"租户 {tenant_id} 今日 token 配额已用尽（purpose={self._purpose}）："
                f"请调整 tenant_model_configs.daily_token_quota，或等待次日重置")
        try:
            response = self._complete_once(system_prompt=system_prompt, user_prompt=user_prompt,
                                           max_tokens=max_tokens)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            llm_observability.record_generation(
                model=self._model_override or _config()[2], operation=operation,
                latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")
            _record_usage(tenant_id=tenant_id, purpose=self._purpose,
                          model=self._model_override or _config()[2], latency_ms=latency_ms, ok=False)
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        llm_observability.record_generation(
            model=response.model_version, operation=operation, latency_ms=latency_ms,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            request_id=response.request_id, system_prompt=system_prompt,
            user_prompt=user_prompt, completion=response.raw_text)
        _record_usage(tenant_id=tenant_id, purpose=self._purpose, model=response.model_version,
                      latency_ms=latency_ms, ok=True, input_tokens=response.input_tokens,
                      output_tokens=response.output_tokens)
        return response

    def _complete_once(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ModelResponse:
        base, key, model, timeout = _config()
        if self._base_url_override:                 # L4：租户配置优先
            base = self._base_url_override.rstrip("/")
        if self._api_key_override:
            key = self._api_key_override
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


def _current_tenant() -> str:
    """当前租户（L3/L4）：延迟 import db，保持本模块可被工具/测试轻量导入。"""
    try:
        import db
        return db.current_tenant()
    except Exception:
        return "default"


def tenant_config(tenant_id: str | None = None, purpose: str = "default") -> dict | None:
    """读该租户的模型配置：先精确匹配 (tenant, purpose)，再回落 (tenant, 'default')。

    密钥解密失败（缺密钥版本/密文被篡改）时**返回 None 并告警**，让调用方回退环境变量，
    不静默用错 key（AES-GCM 的 AAD 绑定了 tenant/purpose，搬移密文会校验失败）。
    """
    tenant = tenant_id or _current_tenant()
    try:
        import db
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT tenant_id, purpose, provider, base_url, model, api_key_ciphertext, "
                "api_key_key_version, max_tokens, temperature, daily_token_quota, enabled "
                "FROM tenant_model_configs WHERE tenant_id=%s AND purpose IN (%s,'default') "
                "AND enabled ORDER BY (purpose=%s) DESC, (purpose='default') DESC LIMIT 1",
                (tenant, purpose, purpose)).fetchone()
    except Exception:                     # 表未建/DB 不可达：回落 env，不影响单租户部署
        return None
    if row is None:
        return None
    config = dict(zip(("tenant_id", "purpose", "provider", "base_url", "model",
                       "api_key_ciphertext", "api_key_key_version", "max_tokens",
                       "temperature", "daily_token_quota", "enabled"), row))
    config["api_key"] = None
    if config.get("api_key_ciphertext"):
        try:
            import sensitive_cipher
            config["api_key"] = sensitive_cipher.SensitiveNumericCipher().decrypt_secret(
                f"{config['tenant_id']}:{config['purpose']}", "model_api_key",
                config["api_key_ciphertext"])
        except Exception as exc:
            logger.warning("租户模型密钥解密失败（回落环境变量）：tenant=%s purpose=%s %s",
                           config["tenant_id"], config["purpose"], exc)
            return None
    return config


def for_tenant(tenant_id: str | None = None, purpose: str = "default") -> "OpenAICompatPort | None":
    """租户级端口工厂（L4）：**先查库、查不到回落环境变量**。

    - 库里有配置：用该租户的 base_url / model / 解密后的 key（密钥按租户隔离，租户可自带）
    - 库里没有：回落进程环境变量（单租户部署与本地开发行为不变）
    - 都没有：返回 None（调用方决定是否报错，与 default_port 一致）
    """
    config = tenant_config(tenant_id, purpose)
    if config is not None:
        return OpenAICompatPort(model=config["model"], temperature=float(config.get("temperature") or 0),
                                base_url=config.get("base_url"), api_key=config.get("api_key"),
                                purpose=config["purpose"], tenant_id=config["tenant_id"])
    return default_port(purpose=purpose, tenant_id=tenant_id)


def _over_quota(tenant_id: str, purpose: str) -> bool:
    """租户当日 token 配额是否已用尽（未配置配额 = 不限）。配额检查失败不拦截调用。"""
    config = tenant_config(tenant_id, purpose)
    if config is None or not config.get("daily_token_quota"):
        return False
    try:
        import db
        return db.llm_usage_today(tenant_id=tenant_id) >= int(config["daily_token_quota"])
    except Exception:
        return False


def _record_usage(*, tenant_id: str, purpose: str, model: str | None, latency_ms: int,
                  ok: bool, input_tokens: int | None = None,
                  output_tokens: int | None = None) -> None:
    """用量记账（L4）。**失败静默**：记账不该阻断模型调用。"""
    try:
        import db
        import llm_observability
        db.record_llm_usage(tenant_id=tenant_id, purpose=purpose, model=model,
                            input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
                            latency_ms=latency_ms, ok=ok,
                            trace_id=llm_observability.current_trace_id())
    except Exception as exc:
        logger.warning("llm_usage 记账失败（不影响调用）：%s", exc)


def default_port(purpose: str = "default", tenant_id: str | None = None):
    """按环境变量返回可用的端口；未配置 key 时返回 None（调用方决定是否报错）。"""
    if not is_available():
        return None
    return OpenAICompatPort(purpose=purpose, tenant_id=tenant_id)
