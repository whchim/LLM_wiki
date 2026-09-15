"""应用内问答服务（对话窗口的后端）：检索 → 有依据才调模型 → 契约校验 → 引用溯源 → 缺口回流。

设计立场（与编译/审核引擎同款纪律）：

1. **门禁先于模型**：检索零命中（缺口判据成立）时**不调模型**，直接给出"知识库暂无"的
   结构化回应，并把这次提问记成缺口（`search_logs.match_count=0`）——不做"无依据硬答"。
   这既省钱，也避免模型用预训练知识冒充企业知识（对话窗口最大的可信度风险）。
2. **引用必须可溯源**：模型输出的每条引用都要过两关——路径必须来自**本次检索结果**，
   `quote` 必须能在该条目正文里**逐字找到**。违例回灌重试 1 次，再违例就**失败**，
   不把编造的答案端给用户（`output_schema.validate_answer_output`）。
3. **契约失败不静默降级**：`status=failed` + 明确错误，前端照实展示。
4. **租户与用量**：模型走 `model_port.for_tenant(purpose="answer")`（租户可单独配便宜模型、
   自带密钥、受配额拦截），检索走 `retrieval.run_search`（路径按租户分区），记账自动进 `llm_usage`。

与 CLI 路径的关系：`.claude/commands/ask.md`（Claude Code 内问答）仍是本地开发路径，
两者共用同一份 `prompts/answer_prompt.md` 契约。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import db
import model_port
import output_schema
import retrieval
from clarification_service import load_system_prompt

PROMPT_NAME = "answer_prompt.md"
PURPOSE = "answer"
ENGINE = "api"
DEFAULT_TOP_K = 6
DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_RETRIES = 1
# 没命中时的兜底话术（契约里"无匹配结果"策略的代码版：话术由代码给，模型不参与）
NO_HITS_TEMPLATE = ("知识库中暂无与「{question}」直接相关的已发布条目。\n\n"
                    "💡 建议：换用更具体的关键词重试；若这属于你的专业领域，"
                    "欢迎把相关文档上传到知识库（编译+审核通过后即可被检索到）。")


@dataclass
class AnswerResult:
    """一次问答的结果（含溯源、缺口与用量信息；**不抛业务异常**）。"""

    question: str
    status: str                        # answered | insufficient | no_hits | failed
    answer: str | None = None
    citations: list[dict] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)   # [{path,title,score,channels,vector_sim}]
    gap: bool = False
    insufficient: bool = False
    contract_ok: bool = True
    dropped_citations: list[str] = field(default_factory=list)  # 溯源失败的引用（留档用）
    followups: list[str] = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    trace_id: str | None = None
    engine: str = ENGINE
    error: str | None = None

    def audit_dict(self) -> dict:
        """trace/日志用（带答案摘要但不落全文，避免 trace 表被正文撑爆）。"""
        return {"question": self.question, "status": self.status,
                "answer_chars": len(self.answer or ""), "citations": len(self.citations),
                "dropped_citations": self.dropped_citations,
                "retrieved": len(self.retrieved), "gap": self.gap,
                "insufficient": self.insufficient, "contract_ok": self.contract_ok,
                "model": self.model, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "latency_ms": self.latency_ms,
                "engine": self.engine, "error": self.error}


def _parse_answer_json(raw: str) -> dict:
    """只接受纯 JSON 对象（与编译/状态 Agent 同款严格口径）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出为空")
    text = raw.strip()
    if text.startswith("```"):          # 容错：模型偶尔仍包代码围栏
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("问答输出必须是 JSON 对象")
    return value


def _retry_feedback(errors: list[str]) -> str:
    """把契约违例回灌给模型（盲重试几乎必然再错一次）。"""
    detail = "；".join(errors)[:600]
    return ("上一次输出未通过契约校验，请**只修正下列问题**后重新输出同一个 JSON 对象"
            f"（不要解释、不要 Markdown 代码围栏）：{detail}")


def _retrieved_brief(entries: list[dict]) -> list[dict]:
    """给前端/日志用的检索摘要（不含正文）。"""
    return [{"path": e["path"], "title": e.get("title"), "score": round(float(e.get("score", 0)), 4),
             "channels": e.get("channels"), "vector_sim": e.get("similarity")}
            for e in entries]


def build_user_prompt(question: str, entries: list[dict]) -> str:
    """把检索结果拼成模型输入（编号 + 路径 + 正文），契约要求逐字引用原文。"""
    parts = [f"## 用户问题\n\n{question}\n\n## 检索结果（共 {len(entries)} 条，只能依据这些内容作答）\n"]
    for i, e in enumerate(entries, 1):
        note = "（正文已截断）" if e.get("truncated") else ""
        parts.append(f"### 来源 {i}：{e.get('title') or e['path']}{note}\n"
                     f"- path: {e['path']}\n- 检索得分: {round(float(e.get('score', 0)), 3)}\n"
                     f"- 正文：\n\n{e.get('content') or '（正文不可读）'}\n")
    return "\n".join(parts)


def ask(question: str, *, port=None, retriever: Callable[..., dict] | None = None,
        top_k: int = DEFAULT_TOP_K, max_tokens: int | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES, system_prompt: str | None = None,
        log_search: bool = True, record_trace: bool = True) -> AnswerResult:
    """回答一个知识库问题。返回 `AnswerResult`；**不抛业务异常**（失败落在 status/error）。

    `retriever` 默认 `retrieval.run_search`（可注入假检索做单测）；
    `port` 默认 `model_port.for_tenant(purpose="answer")`（租户可配、受配额拦截）。
    """
    started = time.perf_counter()
    result = AnswerResult(question=(question or "").strip(), status="failed")
    if not result.question:
        result.error = "问题为空"
        return result

    # ---- 1. 检索（恒走双通道；向量不可用时 retrieval 内部降级为 grep-only 并给 warning）----
    search = (retriever or retrieval.run_search)(
        result.question, mode="auto", top_k=top_k, with_content=True, content_k=top_k)
    entries = search.get("entries") or []
    # 只把**有正文**的条目当依据：向量命中的条目若文件读不到（被删/未落盘），不能当依据
    grounded = [e for e in entries if e.get("content")]
    result.retrieved = _retrieved_brief(entries)
    result.channels = search.get("channels") or {}
    result.gap = bool(search.get("gap"))
    result.warnings.extend(search.get("warnings") or [])

    # ---- 2. 门禁先于模型：无依据不硬答，同时把提问记成知识缺口（缺口回流）----
    if not grounded:
        result.status = "no_hits"
        result.insufficient = True
        result.answer = NO_HITS_TEMPLATE.format(question=result.question)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        if log_search:
            _log_search(result.question, 0)
        if record_trace:
            result.trace_id = _record_trace(result)
        return result

    if port is None:
        port = model_port.for_tenant(purpose=PURPOSE)
    if port is None:
        result.error = "未配置问答模型（tenant_model_configs 的 answer 用途，或环境变量 MODEL_API_KEY）"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    prompt = system_prompt if system_prompt is not None else load_system_prompt(PROMPT_NAME)
    paths = {e["path"] for e in grounded}
    bodies = {e["path"]: e.get("content") or "" for e in grounded}
    user_text = build_user_prompt(result.question, grounded)

    parsed: dict | None = None
    feedback: str | None = None
    for attempt_no in range(1, max_retries + 2):
        attempt: dict[str, Any] = {"attempt": attempt_no}
        try:
            response = port.complete(system_prompt=prompt, user_prompt=user_text,
                                     max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
            result.model = (getattr(response, "model", None)
                            or getattr(response, "model_version", None) or result.model)
            result.input_tokens += response.input_tokens or 0
            result.output_tokens += response.output_tokens or 0
            candidate = _parse_answer_json(response.raw_text)
            errors = output_schema.validate_answer_output(candidate, paths, bodies)
            attempt["status"] = "accepted" if not errors else "contract_error"
            attempt["error"] = None if not errors else "；".join(errors)[:300]
            result.attempts.append(attempt)
            if not errors:
                parsed = candidate
                break
            result.dropped_citations.extend(errors[:5])
            feedback = _retry_feedback(errors)
        except Exception as exc:                  # 网络/解析失败同样按"可重试"处理
            attempt["status"] = "error"
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:300]
            result.attempts.append(attempt)
            feedback = _retry_feedback([attempt["error"]])
        if feedback:
            user_text = f"{user_text}\n\n{feedback}"

    if parsed is None:
        result.status = "failed"
        result.contract_ok = False
        result.error = "问答输出两次未过契约校验（引用不可溯源/形状非法），本次不作答"
    else:
        result.answer = str(parsed.get("answer") or "").strip()
        result.citations = _normalize_citations(parsed.get("citations") or [], bodies)
        result.insufficient = bool(parsed.get("insufficient"))
        result.followups = [str(f).strip() for f in (parsed.get("followups") or [])
                            if str(f).strip()][:3]
        result.status = "insufficient" if result.insufficient else "answered"
        result.contract_ok = True

    result.latency_ms = int((time.perf_counter() - started) * 1000)
    if log_search:
        # 缺口回流口径与 /search 一致：判不出/没依据才算未命中（match_count=0）
        hit = 0 if (result.status in ("no_hits", "failed") or result.insufficient) else len(entries)
        _log_search(result.question, hit)
    if record_trace:
        result.trace_id = _record_trace(result)
    return result


def _normalize_citations(citations: list, bodies: dict[str, str]) -> list[dict]:
    """规范化引用（只保留可溯源的；正文摘录截断，避免前端被长文撑爆）。"""
    out: list[dict] = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        path = str(c.get("path") or "").strip()
        if path not in bodies:
            continue
        body = bodies[path]
        quote = str(c.get("quote") or "").strip()
        idx = body.find(quote[:40]) if quote else -1
        excerpt = body[max(0, idx): idx + 160].strip() if idx >= 0 else quote[:160]
        out.append({"path": path, "quote": quote[:300], "excerpt": excerpt,
                    "note": str(c.get("note") or "").strip()[:200]})
    return out


def _log_search(question: str, match_count: int) -> None:
    """记检索日志（看板缺口 = match_count=0）。失败不阻断问答（与 trace 同款纪律）。"""
    try:
        db.insert_search_log(question, match_count, "ask")
    except Exception:
        pass


def _record_trace(result: AnswerResult) -> str | None:
    """写 `ask` trace span（自建可观测；失败绝不阻断问答）。"""
    trace_id = uuid.uuid4().hex
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO trace_events (span_type, trace_id, operation, status, "
                "latency_ms, detail, operator) "
                "VALUES ('ask', %s, 'ask', %s, %s, %s, 'system')",
                (trace_id, "error" if result.status == "failed" else "ok",
                 result.latency_ms, json.dumps(result.audit_dict(), ensure_ascii=False)))
    except Exception:
        return None
    return trace_id
