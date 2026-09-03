"""SP2 FastAPI 主应用：实例 + CORS + 路由注册 + 健康检查。

启动：uvicorn api.main:app --host 0.0.0.0 --port 8000
自愈：lifespan 启动时 ensure_schema（建目录 + 建表 + 初始管理员）；
import 时零副作用（不连库、不起连接池），保证测试收集/工具导入安全。
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 共享模块路径：api/ 内 `import db/ops` 指向 streamlit_app/（容器已设 PYTHONPATH，本机兜底）
_API_ROOT = Path(__file__).resolve().parent.parent
_SHLIB = _API_ROOT / "streamlit_app"
if str(_SHLIB) not in sys.path:
    sys.path.insert(0, str(_SHLIB))

import db
from api import auth
from api.routers import admin_router, auth_router, review_router, search_router, upload_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动自愈：目录树 + 建表 + 初始管理员（幂等）
    db.ensure_schema()
    auth.ensure_ready()  # JWT_SECRET 缺失时启动即失败（import 时检查已移除，见 auth.py）
    yield
    db.close_pool()  # 优雅退出，避免连接池线程悬挂


app = FastAPI(
    title="LLM Wiki 知识库平台 API",
    version="0.1.0",
    description="Phase 2 SP2：上传/审核/搜索/管理 REST API（JWT 认证 + 审计）。",
    lifespan=lifespan,
)

# CORS：仅允许 Streamlit 管理台来源（设计文档第 8 节）
_origins = [
    os.environ.get("CORS_ORIGIN", "http://localhost:8501"),
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(upload_router.router)
app.include_router(review_router.router)
app.include_router(search_router.router)
app.include_router(admin_router.router)


@app.get("/healthz", tags=["system"])
def healthz() -> dict:
    """容器健康检查：无鉴权。"""
    return {"status": "ok"}