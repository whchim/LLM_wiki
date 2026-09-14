"""LLM 调用可观测（Langfuse 可选上报）测试：零侵入、只报元数据、失败不阻断。

实测背景：项目里 Langfuse 原先只是一个手工命令行探针（`tools/langfuse_probe.py`），
主链路是自建 `trace_events`。这里覆盖"应用层接上 Langfuse"后的四条纪律：
1. 没配 `LANGFUSE_*` → **连 SDK 都不 import**、不发请求（零侵入）；
2. 配了 → 上报元数据（模型、token、延迟、trace_id），**默认不含正文**；
3. `LANGFUSE_SEND_TEXT=1` 才上报 prompt/completion（正文外发必须显式开启）；
4. SDK 构造/上报任何异常都**不影响模型调用**（失败静默，与 trace 写库同款纪律）。
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))

import llm_observability  # noqa: E402
import model_port  # noqa: E402

pytestmark = pytest.mark.no_db

KEYS = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test",
        "LANGFUSE_HOST": "https://langfuse.example"}


class FakeGeneration:
    def __init__(self, sink: list[dict], **kwargs):
        self._sink = sink
        self._kwargs = kwargs

    def generation(self, **kwargs):
        self._sink.append(kwargs)


class FakeLangfuse:
    """最小 langfuse v2 桩：记录 trace/generation 调用参数。"""

    calls: list[dict] = []
    raise_on_init = False

    def __init__(self, **kwargs):
        if FakeLangfuse.raise_on_init:
            raise RuntimeError("SDK 初始化失败")
        self.kwargs = kwargs

    def trace(self, **kwargs):
        FakeLangfuse.calls.append({"trace": kwargs})
        return FakeGeneration(FakeLangfuse.calls, **kwargs)

    def flush(self):
        FakeLangfuse.calls.append({"flush": True})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (*llm_observability.ENV_KEYS, "LANGFUSE_HOST", "LANGFUSE_SEND_TEXT",
                "LANGFUSE_FLUSH_EACH", "MODEL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    llm_observability.reset()
    FakeLangfuse.calls = []
    FakeLangfuse.raise_on_init = False
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)
    yield
    llm_observability.reset()


def _install_fake_sdk(monkeypatch, monkeypatch_keys: bool = True) -> None:
    module = types.ModuleType("langfuse")
    module.Langfuse = FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    if monkeypatch_keys:
        for key, value in KEYS.items():
            monkeypatch.setenv(key, value)


def _openai_payload() -> bytes:
    return json.dumps({
        "id": "req-1", "model": "qwen-plus",
        "choices": [{"message": {"content": "{\"ok\": true}"}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }).encode("utf-8")


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---------- 1. 零侵入 ----------

class _SpyModule(types.ModuleType):
    """探针模块：只有代码真的 `from langfuse import Langfuse` 时才会被触碰。"""

    touched = False

    def __getattr__(self, name):
        _SpyModule.touched = True
        raise AttributeError(name)


def test_disabled_without_keys_does_not_import_sdk(monkeypatch):
    """未配置密钥：不上报、**不 import SDK**、不发请求。"""
    _SpyModule.touched = False
    monkeypatch.setitem(sys.modules, "langfuse", _SpyModule("langfuse"))
    assert llm_observability.is_enabled() is False
    assert llm_observability.record_generation(model="m", latency_ms=1) is None
    assert _SpyModule.touched is False, "未配置密钥时不允许碰 SDK"
    assert FakeLangfuse.calls == []


# ---------- 2/3. 上报内容 ----------

def test_reports_metadata_without_text_by_default(monkeypatch):
    _install_fake_sdk(monkeypatch)
    token = llm_observability.bind_trace_id("t-abc")
    try:
        trace_id = llm_observability.record_generation(
            model="qwen-plus", input_tokens=120, output_tokens=30, latency_ms=456,
            request_id="req-1", operation="clarification_advance",
            system_prompt="系统提示词机密内容", user_prompt="客户纪要正文", completion="模型输出")
    finally:
        llm_observability.reset_trace_id(token)

    assert trace_id == "t-abc"                       # 复用请求级关联 id
    trace_call, generation = FakeLangfuse.calls[0]["trace"], FakeLangfuse.calls[1]
    assert trace_call["id"] == "t-abc"
    assert generation["model"] == "qwen-plus"
    assert generation["usage"] == {"input": 120, "output": 30}
    assert generation["metadata"]["latency_ms"] == 456
    assert generation["metadata"]["operation"] == "clarification_advance"
    assert generation["input"] is None and generation["output"] is None    # 默认不外发正文
    assert "客户纪要正文" not in json.dumps(generation, ensure_ascii=False)


def test_send_text_switch_includes_prompts(monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("LANGFUSE_SEND_TEXT", "1")
    llm_observability.record_generation(model="qwen-plus", latency_ms=10,
                                        system_prompt="系统提示词", user_prompt="客户纪要正文",
                                        completion="模型输出")
    generation = FakeLangfuse.calls[1]
    assert generation["input"] == {"system": "系统提示词", "user": "客户纪要正文"}
    assert generation["output"] == "模型输出"


def test_error_is_reported_and_flush_switch_respected(monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("LANGFUSE_FLUSH_EACH", "1")
    llm_observability.record_generation(model="qwen-plus", latency_ms=8, error="ModelPortError: 超时")
    generation = FakeLangfuse.calls[1]
    assert generation["level"] == "ERROR"
    assert "超时" in generation["metadata"]["error"]
    assert {"flush": True} in FakeLangfuse.calls


# ---------- 4. 失败静默 ----------

def test_sdk_init_failure_is_silent(monkeypatch):
    _install_fake_sdk(monkeypatch)
    FakeLangfuse.raise_on_init = True
    assert llm_observability.record_generation(model="m", latency_ms=1) is None    # 不抛异常


def test_report_failure_does_not_break_model_call(monkeypatch):
    """SDK 构造失败时，模型调用必须照常返回（可观测性不绑架主链路）。"""
    _install_fake_sdk(monkeypatch)
    FakeLangfuse.raise_on_init = True
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(model_port.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(_openai_payload()))
    response = model_port.OpenAICompatPort().complete(
        system_prompt="s", user_prompt="u", max_tokens=100)
    assert response.raw_text == "{\"ok\": true}"


def test_model_call_reports_tokens_and_survives_failure(monkeypatch):
    """成功上报 token；失败上报 error 且不丢原始异常。"""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(model_port.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(_openai_payload()))
    model_port.OpenAICompatPort().complete(system_prompt="s", user_prompt="u", max_tokens=100)
    generation = FakeLangfuse.calls[1]
    assert generation["usage"] == {"input": 120, "output": 30}

    FakeLangfuse.calls = []

    def _boom(req, timeout=None):
        raise model_port.urllib.error.URLError("connection refused")

    monkeypatch.setattr(model_port.urllib.request, "urlopen", _boom)
    with pytest.raises(model_port.ModelPortError):
        model_port.OpenAICompatPort().complete(system_prompt="s", user_prompt="u", max_tokens=100)
    generation = FakeLangfuse.calls[1]
    assert generation["level"] == "ERROR" and "URLError" in generation["metadata"]["error"]
