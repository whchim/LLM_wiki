"""LLM 调用可观测：把模型调用的**元数据**上报到 Langfuse（可选、默认关闭）。

## 为什么是"应用层接 Langfuse + 知识层自建埋点"

- **知识层（编译管道）接不进去**：编译由 watcher 用 headless 方式唤起 **Claude Code CLI**，
  LLM 调用发生在**外部进程**里，我们的 Python 进程没有 hook 点，拿不到它内部的
  span/generation；能拿到的只有编译**结束后** CLI 汇总的元数据（页数、缓存命中、token、耗时），
  因此主链路是自建埋点 `tools/record_compile_trace.py` → `trace_events`。
  要真接需改用 OTel 从 CLI 侧导出或把驱动改成 API 直调——**那是重构，不是接线**（`docs/WIKI-35` §4）。
- **应用层（销售 Agent）接得进去**：模型调用走 `core/model_port.py`，在本进程内，
  且 `ModelResponse` 已带 token 用量——所以在 `ModelPort.complete()` 边界上报 generation。

## 纪律

- **默认只上报元数据**：模型名、input/output token、延迟、trace_id、成败、错误摘要。
  **不上报 prompt/completion 正文**——正文即便已脱敏（PII 已脱敏、金额为占位符），
  也没有理由外发给第三方 SaaS；确需联调时用 `LANGFUSE_SEND_TEXT=1` 显式开启。
- **失败静默**：上报异常绝不阻断模型调用（与 `api/trace.py` 写库同款纪律）。
- **零侵入**：未配置 `LANGFUSE_*` 时不 import SDK、不建连接、不发网络请求。

环境变量：
    LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST   三者齐全才启用
    LANGFUSE_SEND_TEXT=1        额外上报 prompt/completion 正文（默认否）
    LANGFUSE_FLUSH_EACH=1       每次上报后立即 flush（默认交由 SDK 后台批量上报）
    LANGFUSE_RELEASE / LANGFUSE_ENVIRONMENT   可选标记

> 目标 SDK：langfuse v2.x（与 `tools/langfuse_probe.py` 同一套 `trace()/generation()` 用法）。
> 若将来升级到 v3（OTel 风格 `start_generation`），只需改本文件，业务代码不受影响。
"""
from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar

logger = logging.getLogger("llmwiki.llm_observability")

ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
ENV_HOST = "LANGFUSE_HOST"
ENV_SEND_TEXT = "LANGFUSE_SEND_TEXT"
ENV_FLUSH_EACH = "LANGFUSE_FLUSH_EACH"

# 同一次请求内的关联 id：由 api/trace.py 绑定，Langfuse trace 与 trace_events 共用同一个 id，
# 便于"这条 Langfuse trace 对应哪次接口调用"直接对上。
_TRACE_ID: ContextVar[str | None] = ContextVar("llmwiki_trace_id", default=None)
_CLIENTS: dict[tuple[str, str], object] = {}
_WARNED = False


def is_enabled() -> bool:
    """是否启用上报（密钥齐全即可，SDK 是否装得上另算）。"""
    return all(os.environ.get(k) for k in ENV_KEYS)


def bind_trace_id(trace_id: str):
    """绑定当前上下文的关联 id；返回 ContextVar token，供 finally 复位。"""
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token) -> None:
    try:
        _TRACE_ID.reset(token)
    except (ValueError, LookupError):      # 跨上下文复位失败时忽略，不影响主流程
        pass


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def reset() -> None:
    """清空客户端缓存（测试与密钥轮换用）。"""
    _CLIENTS.clear()
    global _WARNED
    _WARNED = False


def _warn_once(message: str) -> None:
    global _WARNED
    if not _WARNED:
        logger.warning("Langfuse 上报不可用（不影响主链路）：%s", message)
        _WARNED = True


def _client():
    """惰性构造 Langfuse 客户端；不可用时返回 None（绝不抛异常）。"""
    if not is_enabled():
        return None
    public_key = os.environ[ENV_KEYS[0]]
    host = os.environ.get(ENV_HOST, "")
    cache_key = (public_key, host)
    if cache_key in _CLIENTS:
        return _CLIENTS[cache_key] or None
    try:
        from langfuse import Langfuse
        kwargs = {"public_key": public_key, "secret_key": os.environ[ENV_KEYS[1]]}
        if host:
            kwargs["host"] = host
        for env, arg in (("LANGFUSE_RELEASE", "release"), ("LANGFUSE_ENVIRONMENT", "environment")):
            if os.environ.get(env):
                kwargs[arg] = os.environ[env]
        client = Langfuse(**kwargs)
    except Exception as exc:                      # 未安装 SDK / 参数不合法 → 静默降级
        _warn_once(f"{type(exc).__name__}: {exc}")
        _CLIENTS[cache_key] = None
        return None
    _CLIENTS[cache_key] = client
    return client


def record_generation(*, model: str, latency_ms: int, operation: str = "chat.completions",
                      input_tokens: int | None = None, output_tokens: int | None = None,
                      request_id: str | None = None, error: str | None = None,
                      system_prompt: str | None = None, user_prompt: str | None = None,
                      completion: str | None = None) -> str | None:
    """上报一次模型调用的元数据；返回 trace_id（未启用时返回 None）。**永不抛异常。**"""
    if not is_enabled():
        return None
    client = _client()
    if client is None:
        return None
    trace_id = current_trace_id() or uuid.uuid4().hex
    send_text = os.environ.get(ENV_SEND_TEXT) == "1"
    metadata = {"operation": operation, "latency_ms": latency_ms, "request_id": request_id}
    if error:
        metadata["error"] = error[:200]
    try:
        trace = client.trace(id=trace_id, name=f"llm:{operation}",
                             metadata={"operation": operation})
        trace.generation(
            name="model_call", model=model,
            usage={"input": input_tokens or 0, "output": output_tokens or 0},
            metadata=metadata,
            input={"system": system_prompt, "user": user_prompt} if send_text else None,
            output=completion if send_text else None,
            level="ERROR" if error else "DEFAULT",
        )
        if os.environ.get(ENV_FLUSH_EACH) == "1":
            client.flush()
    except Exception as exc:                      # 上报失败绝不阻断业务
        _warn_once(f"上报失败：{type(exc).__name__}: {exc}")
    return trace_id
