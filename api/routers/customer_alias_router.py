"""客户别名路由（应用层）。

销售按内部习惯用中文简称提交纪要，系统内部仍只存脱敏代号：
别名只是"选择入口"，不是业务事实，`customer_id` 仍是状态机唯一键。

边界：
- 别名必须通过敏感信息检查（用户自填自由文本，这条闸不能省）
- 别名重名冲突显式报错，绝不静默合并到别的客户
- 删除仅限创建者或管理员
"""
from fastapi import APIRouter, Depends, HTTPException

import db
from api import auth
from api.audit import audit_log
from api.schemas import CustomerAliasRequest

router = APIRouter(prefix="/customers/aliases", tags=["customer-alias"])


@router.get("", response_model=list[dict])
def list_aliases(user: auth.User = Depends(auth.get_current_user)) -> list[dict]:
    """所有别名绑定（销售提交纪要时用于下拉选择）。"""
    return db.list_customer_aliases()


@router.post("", response_model=dict)
def create_alias(body: CustomerAliasRequest,
                 user: auth.User = Depends(auth.get_current_user)) -> dict:
    try:
        result = db.create_customer_alias(body.alias, body.customer_id, user.username)
    except KeyError as exc:          # 别名已绑定到其他客户
        raise HTTPException(status_code=409, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:        # 格式/敏感信息校验失败
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log(user.username, "customer_alias_create", target_path=result["alias"],
              detail={"customer_id": result["customer_id"]})
    return result


@router.delete("", response_model=dict)
def delete_alias(alias: str, customer_id: str,
                 user: auth.User = Depends(auth.get_current_user)) -> dict:
    try:
        removed = db.delete_customer_alias(alias, customer_id,
                                           requester=user.username,
                                           is_admin=user.role == "admin")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="别名绑定不存在")
    audit_log(user.username, "customer_alias_delete", target_path=alias,
              detail={"customer_id": customer_id})
    return {"deleted": True, "alias": alias, "customer_id": customer_id}
