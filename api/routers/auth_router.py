"""认证路由：POST /auth/login、GET /auth/me。"""
import time

from fastapi import APIRouter, Depends, HTTPException, status

from api import auth, trace as trace_mod
from api.audit import audit_log
from api.schemas import LoginRequest, LoginResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    """登录：校验密码 → 签发 JWT。密码错/用户不存在统一 401（防枚举）。

    登录是无 token 可访问的端点，不能注入依赖——端点内显式记录 trace
    （operator 记尝试用户名，detail 统一失败文案，不做枚举泄漏）。"""
    start = time.perf_counter()
    user = auth.get_user(body.username)
    if user is None or not auth.verify_password(body.password, user["password_hash"]):
        trace_mod._record("login", "login", "error",
                          int((time.perf_counter() - start) * 1000),
                          {"error": "认证失败"}, body.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = auth.create_access_token(user["username"], user["role"])
    audit_log(user["username"], "login")
    trace_mod._record("login", "login", "ok",
                      int((time.perf_counter() - start) * 1000),
                      {"role": user["role"]}, user["username"])
    return LoginResponse(
        access_token=token,
        expires_in=auth.TOKEN_TTL_HOURS * 3600,
        role=user["role"],
        display_name=user["display_name"],
    )


@router.get("/me")
def me(current: auth.User = Depends(auth.get_current_user)) -> dict:
    """当前登录用户信息（供 Streamlit 会话校验角色）。"""
    return {"username": current.username, "role": current.role,
            "display_name": current.display_name}