"""问答路由（对话窗口后端）：`POST /ask` —— 检索 → 有依据才作答 → 引用溯源 → 缺口回流。

分层：本路由只做 HTTP（鉴权、取参、trace、响应整形），问答逻辑在 `core/answer_service.py`
（可被工具/worker 复用，与编译/审核引擎同款：**逻辑在服务层，路由只做壳**）。

安全与边界：
- 需要登录（任何角色）；模型调用走 `model_port.for_tenant(purpose="answer")`，受租户配额拦截；
- 检索与条目读取都限定在**本租户知识库根**内（`retrieval` 内部用 `paths.kb_root()`）；
- 未配置模型时**明确 409**，不返回"看起来像答案"的占位文本（不静默降级）；
- 对话历史不落库（前端内存保留），缺口会进 `search_logs` 供自增长看板使用。
"""
import answer_service
from fastapi import APIRouter, Depends, HTTPException, Request

from api import auth, trace as trace_mod
from api.schemas import AskRequest

router = APIRouter(tags=["ask"])


def _shape(result: answer_service.AnswerResult) -> dict:
    return {
        "question": result.question,
        "status": result.status,
        "answer": result.answer,
        "citations": result.citations,
        "retrieved": result.retrieved,
        "gap": result.gap,
        "insufficient": result.insufficient,
        "contract_ok": result.contract_ok,
        "followups": result.followups,
        "channels": result.channels,
        "warnings": result.warnings,
        "model": result.model,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "latency_ms": result.latency_ms,
        "trace_id": result.trace_id,
        "engine": result.engine,
        "error": result.error,
    }


@router.post("/ask")
def ask(body: AskRequest, request: Request,
        user: auth.User = Depends(trace_mod.trace("ask"))) -> dict:
    """就知识库提问，返回带引用溯源的答案（无依据时如实说明并记为知识缺口）。"""
    result = answer_service.ask(body.question, top_k=body.top_k)

    if result.status == "failed" and result.error and "未配置" in result.error:
        request.state.trace_detail = {"operation": "ask", **result.audit_dict()}
        raise HTTPException(status_code=409, detail=result.error)

    request.state.trace_detail = {"operation": "ask", **result.audit_dict()}
    return _shape(result)
