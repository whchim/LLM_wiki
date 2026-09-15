"""知识库路径解析（L3.5 文件分区）：单实例服务多租户时，文件按租户分区。

- **默认租户 `default`** → 直接用 `KB_ROOT` 本身：单租户部署与本地开发的路径**完全不变**；
- **其他租户** → `<KB_ROOT>/tenants/<tenant_id>/`；编译产物、RAW、pending_review、
  _triggers 都落在该子树里，租户之间在文件系统层面也互不可见。

安全：租户 id 会被拼进文件路径，**必须**过白名单校验（只允许字母/数字/下划线/连字符，
长度 ≤64）——否则 `../` 就是一个目录穿越漏洞。非法 id 直接抛错，不做"清洗后继续"。

用法：
    from paths import kb_root, ensure_tenant_tree
    root = kb_root()                      # 跟随当前租户上下文（db.current_tenant()）
    root = kb_root("tenant-a")            # 显式指定
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TENANT_DIR = "tenants"
DEFAULT_TENANT = "default"
_SAFE_TENANT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 多租户子树需要的目录（ensure_tenant_tree 用；与 init.sh / ensure_schema 的目录树一致）
REQUIRED_DIRS = ("RAW", "pending_review", "NEXUS/资源", "NEXUS/概念", "NEXUS/研究", "_triggers/done")


def safe_tenant_id(tenant_id: str) -> str:
    """校验租户 id 可安全拼进文件路径；非法直接抛 ValueError（不静默清洗）。"""
    if not isinstance(tenant_id, str) or not _SAFE_TENANT.match(tenant_id):
        raise ValueError(
            f"非法租户标识：{tenant_id!r}（只允许字母/数字/下划线/连字符，长度 1-64）")
    return tenant_id


def base_root() -> Path:
    """进程级知识库根（`KB_ROOT`，默认仓库内 vault/）。"""
    default = Path(__file__).resolve().parent.parent / "vault"
    return Path(os.environ.get("KB_ROOT") or default)


def kb_root(tenant_id: str | None = None) -> Path:
    """该租户的知识库根：默认租户 = `KB_ROOT`；其他租户 = `<KB_ROOT>/tenants/<id>`。

    `tenant_id=None` 时跟随当前租户上下文（HTTP 请求由中间件绑定、worker 用 `db.bind_tenant`）。
    **空串/None 视为默认租户**（与 `db.bind_tenant` 的口径一致：空值回落 default，而不是报错），
    只有非空且不合白名单的 id 才抛错——避免"空值炸在路径拼接上"这种半途失败。
    """
    tenant = tenant_id if tenant_id is not None else _current_tenant()
    if not tenant or tenant == DEFAULT_TENANT:
        return base_root()
    return base_root() / TENANT_DIR / safe_tenant_id(tenant)


def ensure_tenant_tree(tenant_id: str | None = None) -> Path:
    """自愈建目录：租户子树缺失时补齐（受限角色/新租户首次运行）。返回该租户根。"""
    root = kb_root(tenant_id)
    for rel in REQUIRED_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def _current_tenant() -> str:
    """当前租户（延迟 import db，保持本模块可被工具/测试轻量导入）。"""
    try:
        import db
        return db.current_tenant()
    except Exception:
        return DEFAULT_TENANT
