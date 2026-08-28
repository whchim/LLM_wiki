"""搜索路由：全文搜索（grep 语义）+ 缺口统计 + 条目列表。

搜索命中/未命中写入 search_logs（自增长闭环输入）。
Demo 期用系统 grep（Windows 无此命令，仅 Git Bash 可用）；SP2 改为纯 Python
实现等价的"文件内容包含匹配"——跨平台且行为一致。
"""
import os

from fastapi import APIRouter, Depends, Request

import db
from api import auth, trace as trace_mod

router = APIRouter(tags=["search"])


def _kb_root() -> str:
    """动态读取 KB_ROOT（每次调用），保证测试/容器的 env 生效。"""
    return os.environ.get("KB_ROOT", os.path.join(os.path.dirname(db.__file__), "..", "vault"))


def _grep(query: str) -> list[str]:
    """grep -rl 同款语义：NEXUS 下正文包含 query 的 .md 文件（相对 KB_ROOT）。

    纯 Python 实现（os.walk + 全文包含匹配），不依赖系统 grep 命令。"""
    nexus = os.path.join(_kb_root(), "NEXUS")
    hits: list[str] = []
    for dirpath, _, files in os.walk(nexus):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as f:
                    if query in f.read():
                        hits.append(os.path.relpath(full, _kb_root()).replace(os.sep, "/"))
            except (OSError, UnicodeDecodeError):
                continue
    return hits


@router.get("/search")
def search(query: str, request: Request,
           user: auth.User = Depends(trace_mod.trace("search"))) -> dict:
    """搜索知识库（grep 语义），记录 search_logs + trace（hit_count）。"""
    if not query.strip():
        request.state.trace_detail = {"operation": "search", "query": query, "hit_count": 0}
        return {"query": query, "matches": 0, "files": []}
    files = _grep(query.strip())
    db.insert_search_log(query.strip(), len(files), "api")
    request.state.trace_detail = {
        "operation": "search", "query": query.strip(), "hit_count": len(files)}
    return {"query": query, "matches": len(files), "files": files[:50]}


@router.get("/search/missed")
def missed(limit: int = 20,
           user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索未命中 Top N（知识缺口）。"""
    return {"items": db.top_missed_queries(limit)}


@router.get("/search/stats")
def stats(user: auth.User = Depends(auth.get_current_user)) -> dict:
    """搜索统计：总次数/未命中数/未命中率。"""
    return db.search_stats()


@router.get("/entries")
def entries(limit: int = 100, offset: int = 0,
            type_: str | None = None, status: str | None = None,
            user: auth.User = Depends(auth.get_current_user)) -> dict:
    """条目列表（分页/过滤）。"""
    sql = "SELECT path, type, title, department, status, version, updated_at FROM knowledge_entries"
    conds, params = [], []
    if type_:
        conds.append("type=%s")
        params.append(type_)
    if status:
        conds.append("status=%s")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY path LIMIT %s OFFSET %s"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    keys = ["path", "type", "title", "department", "status", "version", "updated_at"]
    return {"total": total, "items": [dict(zip(keys, r)) for r in rows]}