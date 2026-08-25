"""认证路由：POST /auth/login、GET /auth/me。"""
from fastapi import APIRouter, Depends, HTTPException, status

from api import auth
from api.audit import audit_log
from api.schemas import LoginRequest, LoginResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    """登录：校验密码 → 签发 JWT。密码错/用户不存在统一 401（防枚举）。"""
    user = auth.get_user(body.username)
    if user is None or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = auth.create_access_token(user["username"], user["role"])
    audit_log(user["username"], "login")
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