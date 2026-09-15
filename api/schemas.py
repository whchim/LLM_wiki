"""SP2 Pydantic 请求/响应模型（设计文档 5.1 端点表）。"""
from typing import Any

from pydantic import BaseModel, Field


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


# ---- 销售客户状态 ----
class StateDecisionRequest(BaseModel):
    decision: str
    final_state: str | None = None
    reason: str | None = None
    evidence_refs: list[dict] | None = None


class StateWithdrawRequest(BaseModel):
    reason: str


class StateCorrectionRequest(BaseModel):
    final_state: str
    reason: str
    evidence_refs: list[dict]
    valid_until: str | None = None


class ClarificationSessionRequest(BaseModel):
    conversation_id: str
    max_rounds: int = 2


class SalesIntakeRequest(BaseModel):
    """销售工作台提交的脱敏中文纪要；submitted_by 由 JWT 注入。

    customer_alias：可选，内部中文简称。给了它就以别名绑定的 customer_id 为准，
    前端因此不必自己知道代号；别名未登记/对应多个客户时明确报错，不做猜测。
    force_new：可选，同一客户同一份正文默认复用已有洽谈（防重复提交堆会话）；
    确实要再建一次洽谈时显式传 true。
    """
    idempotency_key: str
    customer_id: str = ""
    content: str
    occurred_at: str
    source_type: str = "meeting_note"
    source_ref: str | None = None
    customer_alias: str | None = None
    force_new: bool = False


class ClarificationAnswerRequest(BaseModel):
    turn_id: str
    question_id: str
    answer_text_redacted: str


class ClarificationResolveRequest(BaseModel):
    """人工处置澄清会话（reviewer/admin）。

    decision=closed → 关闭会话（reason 必填）；
    decision=reopened → 补充事实后重开继续（仅当轮次预算未用尽；answer_text 可选）。
    """
    decision: str
    reason: str | None = None
    answer_text_redacted: str | None = None
    question_id: str | None = None


class CustomerAliasRequest(BaseModel):
    """建立"内部中文简称 → 脱敏代号"的绑定；别名由服务端做敏感信息检查。"""
    alias: str
    customer_id: str


class ModelConfigRequest(BaseModel):
    """L4：租户模型配置。

    `api_key` 语义：**省略 = 保留原密钥**（改模型名不必重传）；**空串 = 清空**（回落环境变量）；
    非空 = 以 AES-GCM 加密落库（AAD 绑定 tenant:purpose，密文搬移会校验失败）。
    `daily_token_quota` 为 None 表示不限量。
    """
    purpose: str = "default"
    provider: str = "openai_compatible"
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_tokens: int = 4000
    temperature: float = 0.0
    daily_token_quota: int | None = None
    enabled: bool = True


class AskRequest(BaseModel):
    """对话窗口提问。

    `top_k` 为送进模型的依据条数（检索条数同值）；`mode` 保留给调试（默认 auto 双通道融合）。
    """
    question: str = Field(min_length=1, max_length=300)
    top_k: int = Field(default=6, ge=1, le=20)
    mode: str = Field(default="auto", pattern="^(auto|grep|vector)$")
