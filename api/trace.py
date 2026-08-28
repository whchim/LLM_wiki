"""SP2.5 可观测性：FastAPI 端点 Trace 依赖（非侵入埋点）。

每个需要观测的端点注入 Depends(trace("span_type"))，自动记录：
- 时耗（perf_counter 单调钟）
- status（ok / error，业务异常与未捕获异常都记 error）
- detail（按 span_type 个性化，由端点通过 request.state.trace_detail 提供）
- operator（当前登录用户；未登录记 system）

写库失败静默（打印日志），绝不阻断主操作。
"""
import json
import logging
import time

from fastapi import Depends, Request

import db
from api import auth

logger = logging.getLogger("llmwiki.trace")

# span_type 受控枚举
SPAN_TYPES = {
    "login", "search", "review_approve", "review_reject", "review_resubmit",
    "review_retry_ai", "rebuild_index",
}


def _operator(request: Request) -> str:
    try:
        user = request.state.current_user
        return user.username
    except Exception:
        return "system"


def _record(span_type: str, operation: str | None, status: str,
            latency_ms: int, detail: dict | None, operator: str) -> None:
    detail = detail or {}
    detail.setdefault("error", None)
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO trace_events (span_type, trace_id, operation, status, "
                "latency_ms, detail, operator) "
                "VALUES (%s, NULL, %s, %s, %s, %s, %s)",
                (span_type, operation, status, latency_ms,
                 json.dumps(detail, ensure_ascii=False), operator))
    except Exception:
        logger.exception("trace 写入失败（不阻断主操作）：span_type=%s", span_type)


def trace(span_type: str):
    """FastAPI 依赖工厂：包裹端点执行并记录 trace。用法见模块 docstring。"""
    if span_type not in SPAN_TYPES:
        raise RuntimeError(f"未知 span_type: {span_type}")

    def dependency(request: Request, user=auth.get_current_user):
        # 先取用户，供端点与 trace 使用（注入到 request.state 供端点读取）
        request.state.current_user = user
        start = time.perf_counter()
        status = "ok"
        error_msg = None
        try:
            yield user
        except Exception as e:
            # 业务 HTTPException 或未捕获异常：都记为 error
            status = "error"
            error_msg = getattr(e, "detail", None) or str(e)
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            detail = getattr(request.state, "trace_detail", None) or {}
            detail.setdefault("error", error_msg)
            _record(span_type, detail.pop("operation", None), status,
                    latency_ms, detail, _operator(request))

    return dependency