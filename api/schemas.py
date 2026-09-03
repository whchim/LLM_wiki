"""SP2 Pydantic 请求/响应模型（设计文档 5.1 端点表）。"""
from typing import Any

from pydantic import BaseModel


# ---- 认证 ----
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    display_name: str | None = None


# ---- 通用 ----
class ApiError(BaseModel):
    detail: str


class Page(BaseModel):
    total: int
    items: list[Any]


# ---- 上传 ----
class TaskOut(BaseModel):
    id: int
    raw_path: str
    status: str
    error_msg: str | None = None
    completed_at: str | None = None


class UploadResult(BaseModel):
    ok: int
    errors: list[str]
    task_ids: list[int]


# ---- 审核 ----
class ReviewOut(BaseModel):
    id: int
    nexus_path: str
    submitter: str | None = None
    department: str | None = None
    ai_verdict: str | None = None
    ai_scores: Any = None
    # LLM 输出契约校验（output_schema.validate_review_output）：None=无 ai_scores；False=存在契约违例
    ai_scores_valid: bool | None = None
    ai_scores_errors: list[str] = []
    human_decision: str | None = None
    reject_reason: str | None = None
    created_at: str | None = None
    title: str | None = None


class RejectRequest(BaseModel):
    reason: str


class Message(BaseModel):
    message: str