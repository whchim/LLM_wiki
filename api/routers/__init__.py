"""SP2 业务路由包。"""
from api.routers import (admin_router, auth_router, review_router,
                         search_router, upload_router)

__all__ = ["admin_router", "auth_router", "review_router", "search_router", "upload_router"]