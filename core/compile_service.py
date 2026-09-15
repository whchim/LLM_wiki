"""应用内编译引擎：把 `workflows/compile_workflow.md` 的步骤**代码化**。

## 为什么需要它（设计文档：docs/WIKI-70_Phase2_应用内编译引擎_设计文档.md）

原先知识层的 LLM 引擎是**应用外**的 Claude Code CLI（watcher → `claude -p` → 读 workflow 文档）。
放到云端多租户一用就露底：① 不可容器化（要 Node + 登录态 + 可写 HOME）；
② watcher 串行"一次一个 claude 进程"，无并发、无队列；③ 没有租户上下文；
④ `acceptEdits` 让 Agent 能写宿主机文件系统，租户隔离无从保证；⑤ 拿不到 per-tenant 计量与配额；
⑥ **workflow 是自然语言文档，写不了单测**。

而编译本身是"**单文档 → 结构化产物**"的确定性流水线，不需要 Agent 的自主性。
因此把流程落成代码：读 RAW → 调 `ModelPort`（`prompts/compile_prompt.md`）→
`output_schema` 契约校验（违例**回灌清单**重试）→ 落盘（资源摘要 + 概念页）→
双写 PG（`knowledge_entries` / `compile_tasks`）→ 记 `compile_session` trace。

CLI 驱动保留为**可选**（`COMPILE_ENGINE=claude_cli`，本地开发/开放探索任务用），
两条驱动共用同一份契约，见 `tools/compile_worker.py`。

## 纪律

- **门禁在模型之前**：`rules.check_sensitive` 为 `blocked` 的文档直接 failed，不送模型。
- **契约校验不通过不落盘**（违例重试 1 次，再败标 failed 并留 error_msg）。
- **永不越过人工**：概念页一律进 `pending_review/`（status=pending），资源摘要按事实策略
  `status=active` 直接发布；两者的 `knowledge_entries` 状态与文件一一对应。
- **指纹幂等**：同路径同指纹的 `done` 任务直接跳过，不重复烧 token。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

import db
import model_port
import ops
import output_schema
import paths
import rules
from clarification_service import load_system_prompt

PROMPT_NAME = "compile_prompt.md"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_RETRIES = 1
# 单篇正文上限：超长文档截断送模型（记录在 trace detail 里，避免"悄悄少读"）
MAX_INPUT_CHARS = 40000
RESOURCE_DIR = "NEXUS/资源"
CONCEPT_PENDING_DIR = "pending_review"
ENGINE = "api"
# L4：模型用途标签（用于租户模型配置的 purpose 分级与用量记账）
PURPOSE = "compile"


def kb_root() -> Path:
    """知识库根（按租户分区，见 `core/paths.py`；每次调用动态解析）。

    默认租户 = `KB_ROOT` 本身；其他租户 = `<KB_ROOT>/tenants/<id>` —— 单租户部署路径不变。
    """
    return paths.kb_root()


@dataclass
class CompileResult:
    """单篇文档的编译结果。"""

    raw_path: str
    status: str                      # done | cached | skipped | failed
    task_id: int | None = None
    resource_path: str | None = None
    concept_paths: list[str] = field(default_factory=list)
    error: str | None = None
    attempts: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    truncated: bool = False

    @property
    def produced(self) -> list[str]:
        paths = list(self.concept_paths)
        if self.resource_path:
            paths.insert(0, self.resource_path)
        return paths

    def audit_dict(self) -> dict:
        """不含正文的摘要（供 trace/审计）。"""
        return {"raw_path": self.raw_path, "status": self.status, "task_id": self.task_id,
                "resource_path": self.resource_path, "concepts": len(self.concept_paths),
                "error": self.error, "attempts": list(self.attempts),
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "latency_ms": self.latency_ms, "truncated": self.truncated, "engine": ENGINE}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_new_raw(limit: int | None = None) -> list[str]:
    """RAW 下"从未进过 compile_tasks"的文件（相对 KB_ROOT 的 POSIX 路径）。

    与 watcher 同口径：任何状态（含 failed/cached）都算"见过"，避免失败任务形成风暴。
    """
    raw = kb_root() / "RAW"
    if not raw.exists():
        return []
    candidates = sorted(p for p in raw.rglob("*")
                         if p.is_file() and p.suffix.lower() in {".md", ".txt"})
    with db.get_conn() as conn:
        seen = {r[0] for r in conn.execute("SELECT DISTINCT raw_path FROM compile_tasks").fetchall()}
    out = [p.relative_to(kb_root()).as_posix() for p in candidates
           if p.relative_to(kb_root()).as_posix() not in seen]
    return out[:limit] if limit else out


def _safe_name(title: str, fallback: str) -> str:
    """标题 → 文件名：去掉路径分隔符与前后空白，控制长度（同名冲突由调用方处理）。"""
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", str(title)).strip()
    name = re.sub(r"\s+", " ", name)
    return (name or fallback)[:60]


def _write_markdown(path: Path, frontmatter: Mapping[str, Any], body: str) -> None:
    """写 Markdown（YAML frontmatter + 正文），**写前校验 frontmatter 契约**。

    契约来源：`vault/SCHEMA.md` + `output_schema.validate_entry_frontmatter`
    （type/status/title/source 必填、version 版本号格式、tags 必须落在三类命名空间）。
    违例直接抛错 → 上层标记 failed 且不落盘——不让不合规条目进库（门禁进 loop）。
    """
    violations = output_schema.validate_entry_frontmatter(dict(frontmatter))
    if violations:
        raise ValueError("frontmatter 不合契约：" + "；".join(violations))
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(dict(frontmatter), allow_unicode=True, sort_keys=False,
                        default_flow_style=False).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body.strip()}\n", encoding="utf-8")


def _namespaced_tags(tags: Any, department: str | None = None, limit: int = 5) -> list[str]:
    """只保留 SCHEMA.md 命名空间内的标签（模型自由生成的领域词会被丢弃）。

    为什么必须过滤：`output_schema.validate_entry_frontmatter` 要求 tags ⊂ 三类命名空间，
    而编译 prompt 对 tags 只是"优先使用预定义标签"的软约束——实测模型会输出
    `政策规划`/`新质生产力` 这类好听但不合规的标签，直接落盘就违反落库契约。
    这里做确定性清洗（不额外烧 token）；模型的领域词已经体现在 description 与正文里。
    """
    keep: list[str] = []
    for tag in tags or []:
        if isinstance(tag, str) and tag in output_schema.TAG_NAMESPACE and tag not in keep:
            keep.append(tag)
    if department in output_schema.TAG_NAMESPACE and department not in keep:
        keep.insert(0, department)
    return keep[:limit]


def _unique_path(path: Path) -> Path:
    """同名冲突时追加 -2/-3…（不覆盖既有条目）。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 100):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"同名文件过多，无法落盘：{path}")


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return text


def _retry_feedback(errors: list[str]) -> str:
    """把契约违例回灌给模型（盲重试几乎必然再错一次）。"""
    detail = "；".join(errors)[:600]
    return ("上一次输出未通过契约校验，请**只修正下列问题**后重新输出同一个 JSON 对象"
            f"（不要解释、不要 Markdown 代码围栏）：{detail}")


def _parse_compile_json(raw: str) -> dict:
    """只接受纯 JSON 对象（与 sales_state_agent 同款严格口径）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出为空")
    text = raw.strip()
    if text.startswith("```"):          # 容错：模型偶尔仍包代码围栏
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("编译输出必须是 JSON 对象")
    return value


def _record_session_trace(*, trace_id: str, results: list[CompileResult], latency_ms: int) -> None:
    """写 compile_session trace（与 tools/record_compile_trace.py 同口径 + engine 标记）。"""
    compiled = sum(1 for r in results if r.status == "done")
    cached = sum(1 for r in results if r.status in ("cached", "skipped"))
    failed = sum(1 for r in results if r.status == "failed")
    files = [p for r in results for p in r.produced][:100]
    detail = {"compiled": compiled, "cached": cached, "skipped": 0, "failed": failed,
              "files": files, "engine": ENGINE}
    status = "error" if failed else "ok"
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO trace_events (span_type, trace_id, operation, status, "
                "latency_ms, detail, operator) "
                "VALUES ('compile_session', %s, 'batch', %s, %s, %s, 'system')",
                (trace_id, status, latency_ms, json.dumps(detail, ensure_ascii=False)))
    except Exception:                    # trace 失败绝不阻断编译（与 ops/trace 同款纪律）
        pass


def compile_one(raw_relpath: str, *, port=None, max_tokens: int | None = None,
                max_retries: int = DEFAULT_MAX_RETRIES,
                system_prompt: str | None = None,
                task_id: int | None = None) -> CompileResult:
    """编译单篇 RAW 文档；返回结果（**不抛业务异常**，失败落在 result.error）。

    `task_id` 给定时表示"该任务已由队列 worker 认领"（L2）：本函数**不再创建/更新任务状态**，
    生命周期（重试退避、终态、租约释放）归 worker——这样多个 worker 可以并发认领不同任务。
    """
    started = time.perf_counter()
    result = CompileResult(raw_path=raw_relpath, status="failed")
    own_task = task_id is None
    raw_abs = kb_root() / raw_relpath
    if not raw_abs.exists():
        result.error = f"RAW 文件不存在：{raw_relpath}"
        return result

    text = raw_abs.read_text(encoding="utf-8", errors="replace")
    fingerprint = sha256_text(text)
    result.truncated = len(text) > MAX_INPUT_CHARS

    # ---- 指纹幂等：同路径同指纹已编译过就跳过（两种模式都生效）----
    # 队列模式（task_id 已给定）也必须查：同一个文件可能被重复入队（上传/扫描/人工重试），
    # 只看"最新任务"会漏掉历史 done 记录，从而重复编译并产出 `xxx-2.md`（实测问题）。
    done = db.find_done_compile_task(raw_relpath, fingerprint, exclude_id=task_id)
    if done is not None:
        result.status = "skipped"
        result.task_id = task_id if task_id is not None else done["id"]
        result.resource_path = done.get("nexus_path")
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ---- 门禁在模型之前：命中 blocked 的文档不送模型 ----
    verdict = rules.check_sensitive(text)
    if verdict == "blocked":
        if own_task:
            task_id = db.insert_compile_task(raw_relpath, fingerprint)
            db.update_compile_task(task_id, "failed", error_msg="门禁拦截（敏感信息/内部标记）")
        result.task_id = task_id
        result.error = "提交门禁拦截：文档含不可外发的敏感信息或内部标记"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    if own_task:
        task_id = db.insert_compile_task(raw_relpath, fingerprint)
        db.update_compile_task(task_id, "processing")
    result.task_id = task_id

    if port is None:
        port = model_port.for_tenant(purpose=PURPOSE)
    if port is None:
        if own_task:
            db.update_compile_task(task_id, "failed",
                                   error_msg="未配置模型（MODEL_API_KEY / DASHSCOPE_API_KEY）")
        result.error = "未配置模型"
        return result

    prompt = system_prompt if system_prompt is not None else load_system_prompt(PROMPT_NAME)
    user_text = text if not result.truncated else text[:MAX_INPUT_CHARS] + "\n…（已截断）"

    parsed: dict | None = None
    feedback: str | None = None
    for attempt_no in range(1, max_retries + 2):
        attempt: dict[str, Any] = {"attempt": attempt_no}
        try:
            response = port.complete(system_prompt=prompt, user_prompt=user_text,
                                     max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
            result.input_tokens += response.input_tokens or 0
            result.output_tokens += response.output_tokens or 0
            candidate = _parse_compile_json(response.raw_text)
            errors = output_schema.validate_compile_output(candidate)
            attempt["status"] = "accepted" if not errors else "contract_error"
            attempt["error"] = None if not errors else "；".join(errors)[:300]
            result.attempts.append(attempt)
            if not errors:
                parsed = candidate
                break
            feedback = _retry_feedback(errors)
        except Exception as exc:                  # 网络/解析失败同样按"可重试"处理
            attempt["status"] = "error"
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:300]
            result.attempts.append(attempt)
            feedback = _retry_feedback([attempt["error"]])
        if feedback:
            user_text = f"{user_text}\n{feedback}"

    if parsed is None:
        if own_task:
            db.update_compile_task(task_id, "failed",
                                   error_msg=(result.attempts[-1].get("error") or "编译失败")[:500])
        result.error = result.attempts[-1].get("error") or "编译失败"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        ops.append_log("编译", f"{raw_relpath} 失败：{result.error[:120]}")
        return result

    try:
        resource_path, concept_paths = _write_products(parsed, raw_relpath, fingerprint)
    except Exception as exc:
        if own_task:
            db.update_compile_task(task_id, "failed", error_msg=f"落盘失败：{exc}"[:500])
        result.error = f"落盘失败：{exc}"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    if own_task:
        db.update_compile_task(task_id, "done", nexus_path=resource_path)
    result.status = "done"
    result.resource_path = resource_path
    result.concept_paths = concept_paths
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    # Reserved File（PRD WIKI-00）：编译成功写一行 log.md（审计日志，append-only）
    ops.append_log("编译", f"{raw_relpath} → {resource_path}"
                           f"（资源 1 + 概念 {len(concept_paths)}）")
    return result


def _write_products(parsed: Mapping[str, Any], raw_relpath: str,
                    fingerprint: str) -> tuple[str, list[str]]:
    """落盘资源摘要（active）+ 概念页（pending 待审），并双写 knowledge_entries。"""
    resource = parsed["resource"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = str(resource["title"]).strip()
    res_path = _unique_path(kb_root() / RESOURCE_DIR / f"{_safe_name(title, '未命名资源')}.md")
    res_rel = res_path.relative_to(kb_root()).as_posix()
    _write_markdown(res_path, {
        "type": "resource", "title": title, "status": "active", "version": "V1.0",
        "department": resource.get("department"), "source_type": resource.get("source_type"),
        "tags": _namespaced_tags(resource.get("tags"), resource.get("department")),
        "description": str(resource.get("description") or ""),
        "source": raw_relpath, "fingerprint": fingerprint, "updated_at": today,
    }, str(resource.get("summary") or ""))
    db.upsert_entry(res_rel, "resource", title, resource.get("department"), "active",
                    "V1.0", fingerprint, today)
    ops.append_index("资源", f"[[{res_path.stem}]] → {res_rel}")

    concept_paths: list[str] = []
    for concept in parsed.get("concepts") or []:
        c_title = str(concept["title"]).strip()
        c_path = _unique_path(kb_root() / CONCEPT_PENDING_DIR / f"{_safe_name(c_title, '未命名概念')}.md")
        c_rel = c_path.relative_to(kb_root()).as_posix()
        _write_markdown(c_path, {
            "type": "concept", "title": c_title, "status": "pending", "version": "V1.0",
            "department": concept.get("department"),
            "tags": _namespaced_tags(concept.get("tags"), concept.get("department")),
            "description": str(concept.get("description") or ""),
            "related_to": list(concept.get("related_to") or []),
            "source": raw_relpath, "fingerprint": fingerprint, "updated_at": today,
        }, str(concept.get("content") or ""))
        db.upsert_entry(c_rel, "concept", c_title, concept.get("department"), "pending",
                        "V1.0", fingerprint, today)
        concept_paths.append(c_rel)
    return res_rel, concept_paths


def compile_batch(raw_relpaths: list[str], *, port=None, max_tokens: int | None = None,
                  write_review_trigger: bool = True) -> dict:
    """批量编译：逐篇编译 → 汇总 → 给概念页写 review 纸条 → 记 compile_session trace。

    返回 {"trace_id", "compiled", "cached", "failed", "results": [...], "files": [...]}。
    """
    started = time.perf_counter()
    trace_id = uuid.uuid4().hex
    if port is None:
        port = model_port.for_tenant(purpose=PURPOSE)
    results = [compile_one(path, port=port, max_tokens=max_tokens) for path in raw_relpaths]

    concept_paths = [p for r in results for p in r.concept_paths]
    if write_review_trigger and concept_paths:
        ops.write_trigger("review", concept_paths, "compile_service")

    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_session_trace(trace_id=trace_id, results=results, latency_ms=latency_ms)
    return {
        "trace_id": trace_id,
        "compiled": sum(1 for r in results if r.status == "done"),
        "cached": sum(1 for r in results if r.status in ("cached", "skipped")),
        "failed": sum(1 for r in results if r.status == "failed"),
        "files": [p for r in results for p in r.produced],
        "latency_ms": latency_ms,
        "results": [r.audit_dict() for r in results],
    }
