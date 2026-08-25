"""SP2 审计：写操作事件落 audit_logs 表（设计文档 4.2）。

业务函数显式调用（非 ASGI 中间件）——携带 operator/action/target/detail 语义。
审计写入失败不阻断主操作（try/except + 打印），审计是增强不是强一致。
"""
import logging

import psycopg.types.json
from psycopg.types.json import Jsonb

import db

logger = logging.getLogger("llmwiki.audit")

# 受控 action 枚举（设计文档 4.2）
ACTIONS = {
    "login", "upload", "review_approve", "review_reject", "review_resubmit",
    "rebuild_index", "retry_compile", "trigger_write",
}


def audit_log(operator: str, action: str, target_path: str | None = None,
              detail: dict | None = None) -> None:
    """写一条审计记录。operator 为登录用户名或 'system'。"""
    if action not in ACTIONS:
        logger.warning("未知审计 action: %s（已忽略）", action)
        return
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_logs (operator, action, target_path, detail) "
                "VALUES (%s, %s, %s, %s)",
                (operator, action, target_path, Jsonb(detail) if detail is not None else None))
    except Exception:
        logger.exception("审计写入失败（不阻断主操作）：action=%s", action)