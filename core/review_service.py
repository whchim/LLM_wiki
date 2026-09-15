"""应用内审核引擎：把 `workflows/review_workflow.md` 的六维度审核**代码化**。

分工（与项目"规则/模型分工"一致）：
- **确定性两维交代码**：维度一完整性（`rules.check_completeness`）、维度五敏感信息（`rules.check_sensitive`）；
- **模糊四维交模型**：维度二去重、维度三职务归属、维度四质量、维度六合规（一次调用，见 `prompts/review_prompt.md`）；
- **verdict 由代码按判定逻辑链计算**（`review_prompt.md` §判定逻辑），不信模型自报的结论；
  模型若给出不同 verdict，只记一条 concern 并留档（`verdict_model`），不改判定——**可观测、但不让模型越权**。

落库：`pending_reviews`（`db.insert_review`）。人工作业（通过/驳回）仍走工作台 `/reviews/{id}/approve|reject`
→ `ops.approve_entry` 把文件移入 `NEXUS/概念/` 并双写——**模型永不直接发布条目**。

与 CLI 驱动的关系：与编译引擎同款（`COMPILE_ENGINE`），CLI 侧 `review_workflow.md` 保留为本地开发路径。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

import db
import model_port
import output_schema
import paths
import rules
from clarification_service import load_system_prompt

PROMPT_NAME = "review_prompt.md"
DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_RETRIES = 1
PENDING_DIR = "pending_review"
NEXUS_DIR = "NEXUS"
DEDUP_CANDIDATES = 5
CANDIDATE_EXCERPT = 200
ENGINE = "api"
# L4：模型用途标签（租户可为审核单独配便宜模型）
PURPOSE = "review"

VERDICTS = ("approved", "rejected", "needs_human_review")


def kb_root() -> Path:
    """知识库根（按租户分区，见 `core/paths.py`；默认租户 = KB_ROOT 本身）。"""
    return paths.kb_root()


@dataclass
class ReviewResult:
    """单条目的审核结果。"""

    nexus_path: str
    status: str                       # reviewed | skipped | failed
    review_id: int | None = None
    verdict: str | None = None
    verdict_model: str | None = None
    department: str | None = None
    scores: dict | None = None
    concerns: list[str] = field(default_factory=list)
    error: str | None = None
    attempts: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def audit_dict(self) -> dict:
        return {"nexus_path": self.nexus_path, "status": self.status, "review_id": self.review_id,
                "verdict": self.verdict, "verdict_model": self.verdict_model,
                "department": self.department, "scores": self.scores,
                "concerns": self.concerns, "error": self.error, "attempts": list(self.attempts),
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "latency_ms": self.latency_ms, "engine": ENGINE}


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def _tokens(title: str) -> set[str]:
    """标题分词（中英混排）：用于确定性挑选去重候选。"""
    return {t for t in re.split(r"[\s、，,·\-—_/（）()【】\[\]]+", str(title)) if len(t) >= 2}


def dedup_candidates(nexus_path: str, title: str, limit: int = DEDUP_CANDIDATES) -> list[dict]:
    """按标题词重合度挑候选重复条目（确定性、不调模型）：路径 + 标题 + 前 200 字摘要。"""
    root = kb_root()
    wanted = _tokens(title)
    scored: list[tuple[int, dict]] = []
    for base in (PENDING_DIR, NEXUS_DIR):
        directory = root / base
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            if rel == nexus_path:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = _parse_frontmatter(text)
            other_title = str(fm.get("title") or path.stem)
            overlap = len(wanted & _tokens(other_title))
            if overlap == 0:
                continue
            body = re.sub(r"\s+", " ", text.split("---", 2)[-1]).strip()
            scored.append((overlap, {"path": rel, "title": other_title,
                                     "excerpt": body[:CANDIDATE_EXCERPT]}))
    scored.sort(key=lambda item: (-item[0], item[1]["path"]))
    return [item for _, item in scored[:limit]]


def _retry_feedback(errors: list[str]) -> str:
    detail = "；".join(errors)[:400]
    return ("上一次输出未通过契约校验，请**只修正下列问题**后重新输出同一个 JSON 对象"
            f"（不要解释、不要 Markdown 代码围栏）：{detail}")


def _parse_json(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出为空")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("审核输出必须是 JSON 对象")
    return value


def decide_verdict(scores: Mapping[str, Any], concerns: list[str]) -> tuple[str, str]:
    """按 `review_prompt.md` §判定逻辑链计算 verdict（确定性，可单测）。

    返回 (verdict, summary)。顺序即优先级，与 prompt 一致：
    sensitive=blocked → completeness=insufficient → 需人工（低质量/合规/敏感告警/concerns≥3）
    → dedup=duplicate → dedup=similar（通过但标注） → 通过。
    """
    if scores.get("sensitive") == "blocked":
        return "rejected", "条目包含敏感信息，一票否决"
    if scores.get("completeness") == "insufficient":
        return "rejected", "条目完整性严重不足，建议补充后重新提交"
    if (scores.get("quality") is not None and scores["quality"] <= 2) or \
       scores.get("compliance") == "flagged" or scores.get("sensitive") == "warning" or len(concerns) >= 3:
        return "needs_human_review", f"条目存在需人工判定的问题（{len(concerns)} 项），建议人工复核"
    if scores.get("dedup") == "duplicate":
        return "rejected", "条目与已有知识高度重复，不建议入库"
    if scores.get("dedup") == "similar":
        return "approved", "条目质量合格；与已有条目主题相似，已标注建议人工确认是否需要合并或区分视角"
    return "approved", "条目质量合格，建议通过审核"


def build_review_payload(nexus_path: str, *, submitter: str = "system") -> dict:
    """构造审核上下文（确定性两维 + 候选列表 + 正文），供模型与落库共用。"""
    path = kb_root() / nexus_path
    text = path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    parts = text.split("---", 2)
    fm_text = parts[1] if len(parts) >= 3 else ""
    body = parts[2] if len(parts) >= 3 else text
    completeness = rules.check_completeness(fm_text, body)
    sensitive = rules.check_sensitive(text)
    return {
        "nexus_path": nexus_path, "content": text, "frontmatter": fm, "submitter": submitter,
        "deterministic": {"completeness": completeness, "sensitive": sensitive},
        "candidates": dedup_candidates(nexus_path, str(fm.get("title") or path.stem)),
    }


def _build_user_prompt(payload: Mapping[str, Any]) -> str:
    candidates = payload["candidates"]
    candidate_text = "\n".join(
        f"- {c['path']}｜标题：{c['title']}｜摘要：{c['excerpt']}" for c in candidates) or "（空列表）"
    return (
        "## 待审核条目（含 YAML Frontmatter）\n\n"
        f"{payload['content']}\n\n"
        f"## 候选重复条目列表（{len(candidates)} 条，可能为空）\n\n{candidate_text}\n\n"
        f"## 审核上下文\n\n提交者：{payload['submitter']}\n"
        f"确定性检查结果（维度一完整性 / 维度五敏感信息，已由程序判定，请照抄进 scores）："
        f"completeness={payload['deterministic']['completeness']}、"
        f"sensitive={payload['deterministic']['sensitive']}\n\n"
        "请按 system prompt 的输出格式只输出一个 JSON 对象："
        "verdict/department/scores{completeness,dedup,quality,sensitive,compliance}/duplicates/concerns/summary。"
    )


def review_one(nexus_path: str, *, port=None, submitter: str = "system",
               max_tokens: int | None = None, max_retries: int = DEFAULT_MAX_RETRIES,
               system_prompt: str | None = None) -> ReviewResult:
    """审核单条目：确定性两维 + 模型四维 → 代码算 verdict → 写 pending_reviews。"""
    started = time.perf_counter()
    result = ReviewResult(nexus_path=nexus_path, status="failed")
    path = kb_root() / nexus_path
    if not path.exists():
        result.error = f"条目文件不存在：{nexus_path}"
        return result

    # 幂等：已有 AI 审核结果且未被人工处置 → 跳过（人工处置过的也不重审）
    existing = db.find_review(nexus_path)
    if existing and existing.get("ai_scores"):
        result.status = "skipped"
        result.review_id = existing["id"]
        result.verdict = existing.get("ai_verdict")
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    payload = build_review_payload(nexus_path, submitter=submitter)
    if payload["deterministic"]["sensitive"] == "blocked":
        # 确定性一票否决：没必要花模型的钱（与编译侧"门禁先于模型"同款纪律）
        scores = {"completeness": payload["deterministic"]["completeness"], "dedup": "pass",
                  "quality": 1, "sensitive": "blocked", "compliance": "flagged"}
        concerns = ["确定性检查命中敏感信息（一票否决），未送模型"]
        verdict, summary = decide_verdict(scores, concerns)
        return _store(result, nexus_path, payload, scores, concerns, verdict, summary,
                      verdict_model=None, submitter=submitter, started=started)

    if port is None:
        port = model_port.for_tenant(purpose=PURPOSE)
    if port is None:
        result.error = "未配置模型（MODEL_API_KEY / DASHSCOPE_API_KEY）"
        return result

    prompt = system_prompt if system_prompt is not None else load_system_prompt(PROMPT_NAME)
    user_prompt = _build_user_prompt(payload)
    parsed: dict | None = None
    feedback: str | None = None
    for attempt_no in range(1, max_retries + 2):
        attempt: dict[str, Any] = {"attempt": attempt_no}
        try:
            response = port.complete(system_prompt=prompt, user_prompt=user_prompt,
                                     max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
            result.input_tokens += response.input_tokens or 0
            result.output_tokens += response.output_tokens or 0
            candidate = _parse_json(response.raw_text)
            errors = output_schema.validate_review_output(candidate)
            attempt["status"] = "accepted" if not errors else "contract_error"
            attempt["error"] = None if not errors else "；".join(errors)[:300]
            result.attempts.append(attempt)
            if not errors:
                parsed = candidate
                break
            feedback = _retry_feedback(errors)
        except Exception as exc:
            attempt["status"] = "error"
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:300]
            result.attempts.append(attempt)
            feedback = _retry_feedback([attempt["error"]])
        if feedback:
            user_prompt = f"{user_prompt}\n{feedback}"

    if parsed is None:
        result.error = result.attempts[-1].get("error") or "审核失败"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # 合并：确定性两维以代码为准（模型的这两维只作参考），模糊四维取模型
    model_scores = parsed.get("scores") or {}
    scores = {
        "completeness": payload["deterministic"]["completeness"],
        "sensitive": payload["deterministic"]["sensitive"],
        "dedup": model_scores.get("dedup"),
        "quality": model_scores.get("quality"),
        "compliance": model_scores.get("compliance"),
    }
    concerns = [str(c) for c in (parsed.get("concerns") or [])]
    department = parsed.get("department") or payload["frontmatter"].get("department")
    # similar 的"建议合并"标注要**先**并入 concerns 再判定：否则 concerns 从 2 涨到 3 时，
    # 会出现"verdict=approved 但 concerns≥3"的自相矛盾（validate_review_output 会判违例）。
    if scores.get("dedup") == "similar":
        dup = (parsed.get("duplicates") or ["（未指明）"])[0]
        concerns.append(f"与已有条目 [{dup}] 主题相似，建议检查是否需要合并或区分视角")
    verdict, summary = decide_verdict(scores, concerns)
    return _store(result, nexus_path, payload, scores, concerns, verdict, summary,
                  verdict_model=parsed.get("verdict"), submitter=submitter, started=started,
                  department=department, duplicates=list(parsed.get("duplicates") or []))


def _store(result: ReviewResult, nexus_path: str, payload: Mapping[str, Any], scores: dict,
           concerns: list[str], verdict: str, summary: str, *, verdict_model: str | None,
           submitter: str, started: float, department: str | None = None,
           duplicates: list[str] | None = None) -> ReviewResult:
    """写 pending_reviews（人工作业的输入），并把模型自报 verdict 的差异记进 concerns。

    **落库 JSON 形状**与 `prompts/review_prompt.md` 的输出契约一致
    （`verdict / department / scores{...} / duplicates / concerns / summary`）——
    因为 `review_router._to_out` 会拿它跑 `validate_review_output` 判 `ai_scores_valid`，
    工作台也按 `ai_scores.scores.<维度>` 取值；额外字段（`verdict_model`/`engine`）不参与校验。
    """
    if verdict_model and verdict_model != verdict:
        concerns = [*concerns, f"模型自报 verdict={verdict_model} 与规则判定={verdict} 不一致，已按规则执行"]
    payload_out = {
        "verdict": verdict,
        "department": department or "共享层",
        "scores": {**scores},
        "duplicates": duplicates or [],
        "concerns": concerns,
        "summary": summary,
        "verdict_model": verdict_model,
        "engine": ENGINE,
    }
    review_id = db.insert_review(nexus_path, submitter, department or "共享层", verdict,
                                 json.dumps(payload_out, ensure_ascii=False))
    result.status = "reviewed"
    result.review_id = review_id
    result.verdict = verdict
    result.verdict_model = verdict_model
    result.department = department
    result.scores = scores
    result.concerns = concerns
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


def scan_pending(limit: int | None = None) -> list[str]:
    """`pending_review/` 下尚无 AI 审核结果的条目（相对 KB_ROOT 路径）。"""
    directory = kb_root() / PENDING_DIR
    if not directory.exists():
        return []
    out: list[str] = []
    for path in sorted(directory.rglob("*.md")):
        rel = path.relative_to(kb_root()).as_posix()
        existing = db.find_review(rel)
        if existing and existing.get("ai_scores"):
            continue
        out.append(rel)
    return out[:limit] if limit else out


def review_batch(nexus_paths: list[str], *, port=None, submitter: str = "system") -> dict:
    """批量审核并汇总（供 worker 调用）。"""
    started = time.perf_counter()
    if port is None:
        port = model_port.for_tenant(purpose=PURPOSE)
    results = [review_one(path, port=port, submitter=submitter) for path in nexus_paths]
    return {
        "reviewed": sum(1 for r in results if r.status == "reviewed"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "verdicts": {v: sum(1 for r in results if r.verdict == v) for v in VERDICTS},
        "results": [r.audit_dict() for r in results],
    }
