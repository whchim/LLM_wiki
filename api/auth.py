"""SP2 认证模块：JWT 签发/校验 + 密码哈希 + FastAPI 权限依赖。

设计文档第 6 节。PyJWT HS256（JWT_SECRET，12h 过期）+ pwdlib argon2。
"""
import os
import time
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pydantic import BaseModel

import db

JWT_ALG = "HS256"
TOKEN_TTL_HOURS = 12

_bearer = HTTPBearer(auto_error=False)


class User(BaseModel):
    """当前登录用户（从 JWT + users 表还原）。"""
    id: int
    username: str
    role: str
    display_name: str | None = None


# ---- 密码哈希 ----
def _hasher():
    import pwdlib
    return pwdlib.PasswordHash.recommended()


def hash_password(plain: str) -> str:
    return _hasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher().verify(plain, hashed)
    except Exception:
        return False


# ---- JWT ----
def _jwt_secret() -> str:
    """运行时读取 JWT_SECRET（函数内动态读 env，沿"被 import 的模块零副作用"纪律）。"""
    return os.environ.get("JWT_SECRET", "")


def ensure_ready() -> None:
    """JWT_SECRET 缺失时 fail-fast（安全边界，设计文档第 8 节）。

    运行时检查而非 import 时检查：工具脚本/测试可在未配置 key 的情况下
    导入 api 包（import 零副作用）；服务侧的 fail-fast 由 main.lifespan 显式调用。"""
    if not _jwt_secret():
        raise RuntimeError("JWT_SECRET 未设置：请在环境变量中配置（.env.example 有说明）")


def create_access_token(username: str, role: str, expires_h: int = TOKEN_TTL_HOURS) -> str:
    """签发 JWT：payload 含 sub(用户名)、role、exp(过期时间戳)。"""
    ensure_ready()
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + expires_h * 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALG)


def decode_token(token: str) -> dict[str, Any] | None:
    """校验 JWT：签名/过期/缺失返回 None（调用方转 401）。"""
    ensure_ready()
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


# ---- 用户查询 ----
def get_user(username: str) -> dict | None:
    """按用户名查 users 表。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, display_name FROM users WHERE username=%s",
            (username,)).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "username": row[1], "password_hash": row[2],
        "role": row[3], "display_name": row[4],
    }


# ---- FastAPI 依赖 ----
def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    """从 Authorization: Bearer <token> 还原用户；未登录/伪造/过期 → 401。"""
    err = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或登录已过期", headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise err
    payload = decode_token(credentials.credentials)
    if payload is None:
        raise err
    username = payload.get("sub")
    if not username:
        raise err
    u = get_user(username)
    if u is None:  # 用户已被删除/禁用
        raise err
    return User(id=u["id"], username=u["username"], role=u["role"], display_name=u["display_name"])


def require_roles(*roles: str):
    """角色守卫工厂：`dependencies=[Depends(require_roles("admin"))]`。

    角色不符 → 403（不泄露端点存在性以外的信息）。
    """
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限执行此操作")
        return user
    return checker