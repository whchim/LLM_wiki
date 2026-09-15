"""图谱路由：`GET /graph`（全图）+ `GET /graph/neighbors`（某条目 1 跳邻域）。

数据从**已编译的 Markdown 派生**（`core/graph.py`，不新增权威表）；按租户分区（L3.5）。
权限：登录即可查看（与「全部条目」一致——图谱不暴露比条目更多的信息）。
"""
import graph
from fastapi import APIRouter, Depends, Query

from api import auth

router = APIRouter(tags=["graph"])


@router.get("/graph")
def get_graph(include_pending: bool = Query(False),
              max_nodes: int = Query(1500, ge=50, le=5000),
              user: auth.User = Depends(auth.get_current_user)) -> dict:
    """知识图谱：节点（条目）+ 边（`related_to` / `[[wikilink]]` / markdown 链接）+ 待建页面。

    `include_pending=true` 带上 `pending_review/`（审核视角看全貌）；
    默认只看 `NEXUS/`（已发布），与检索口径一致。
    """
    return graph.build_graph(include_pending=include_pending, max_nodes=max_nodes)


@router.get("/graph/neighbors")
def get_neighbors(path: str = Query(..., min_length=1, max_length=500),
                  user: auth.User = Depends(auth.get_current_user)) -> dict:
    """某条目的 1 跳邻域（含"引用了但还没建"的待建页面）。"""
    return graph.neighbors(path)
