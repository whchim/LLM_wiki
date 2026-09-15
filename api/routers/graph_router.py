"""图谱路由：`GET /graph`（全图）+ `GET /graph/neighbors`（某条目 1 跳邻域）。

数据从**已编译的 Markdown 派生**（`core/graph.py`，不新增权威表）；按租户分区（L3.5）。
权限：登录即可查看（与「全部条目」一致——图谱不暴露比条目更多的信息）。
"""
import graph
from fastapi import APIRouter, Depends, Query

from api import auth

router = APIRouter(tags=["graph"])


@router.get("/graph")
def get_graph(include_pending: bool = Query(True),
              include_meta: bool = Query(True),
              include_raw: bool = Query(False),
              max_nodes: int = Query(1500, ge=50, le=5000),
              user: auth.User = Depends(auth.get_current_user)) -> dict:
    """知识图谱：节点（条目 + 知识库保留文件）+ 边（`related_to` / `[[wikilink]]` / markdown 链接）+ 待建页面。

    范围对齐 Obsidian 的图谱口径（**整个知识库都该被看见**）：
    - `include_meta=true`（默认）带上 `index.md` / `log.md` / `SCHEMA.md` 这些**保留文件**
      （PRD WIKI-00 §Reserved Files），它们是知识库自我描述的一部分；
    - `include_pending=true`（默认）带上 `pending_review/` 待审概念页；
    - `include_raw=false`（默认）不带 `RAW/` 原始语料——打开即把未编译的原料混进来会淹没
      真正的知识层，需要时一键打开（节点 `kind=raw`，前端另行着色）。
    """
    return graph.build_graph(include_pending=include_pending, include_meta=include_meta,
                             include_raw=include_raw, max_nodes=max_nodes)


@router.get("/graph/neighbors")
def get_neighbors(path: str = Query(..., min_length=1, max_length=500),
                  include_pending: bool = Query(True),
                  include_raw: bool = Query(False),
                  user: auth.User = Depends(auth.get_current_user)) -> dict:
    """某条目的 1 跳邻域（含"引用了但还没建"的待建页面）。"""
    return graph.neighbors(path, include_pending=include_pending, include_raw=include_raw)
