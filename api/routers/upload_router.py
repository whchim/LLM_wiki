"""上传路由：上传/编译任务列表/失败重试。

复用 streamlit_app.ops.py 的上传校验与触发文件逻辑（共享模块），
FastAPI 侧只做 HTTP 层 + 审计。
"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

import db
import ops
from api import auth
from api.audit import audit_log
from api.schemas import TaskOut, UploadResult

router = APIRouter(prefix="/uploads", tags=["upload"])

# 动态读取 KB_ROOT（每次调用），保证测试/容器的 monkeypatch/env 生效
def _kb_root() -> str:
    return os.environ.get("KB_ROOT", os.path.join(os.path.dirname(ops.__file__), "..", "vault"))

CATEGORIES = ["个人_notes", "会议", "经验", "项目"]


@router.post("", response_model=UploadResult)
async def upload_files(
    files: list[UploadFile] = File(...),
    category: str = Form("个人_notes"),
    user: auth.User = Depends(auth.require_roles("user", "admin", "reviewer")),
) -> UploadResult:
    """上传文档：校验 → 落盘 RAW/<category>/ → 编译任务入库 → 写触发文件。"""
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"来源分类非法：{category}（可选 {CATEGORIES}）")

    saved: list[str] = []
    errors: list[str] = []
    for f in files:
        filename = f.filename or "未命名"
        name = Path(filename).name  # 净化：丢弃目录部分，防路径穿越
        try:
            content = await f.read()
        except Exception as e:
            errors.append(f"{filename}：读取失败（{e}）。可能原因：文件损坏。建议：重新上传。")
            continue
        err = ops.validate_upload(name, len(content))
        if err:
            errors.append(f"{filename}：{err}")
            continue
        raw_dir = Path(_kb_root()) / "RAW" / category
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            (raw_dir / name).write_bytes(content)
        except OSError as e:
            errors.append(f"{filename}：保存到 RAW 失败（{e}）。可能原因：磁盘空间不足。建议：检查磁盘后重试。")
            continue
        saved.append(f"RAW/{category}/{name}")

    if not saved:
        return UploadResult(ok=0, errors=errors, task_ids=[])

    task_ids: list[int] = []
    try:
        for path in saved:
            fingerprint = ops.sha256_file(str(Path(_kb_root()) / path))
            task_ids.append(db.insert_compile_task(path, fingerprint))
    except Exception as e:
        for tid in task_ids:
            try:
                db.update_compile_task(tid, "failed", error_msg=f"批处理中断：{e}")
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"任务入库失败（{e}）。建议：稍后重试或重建索引。")

    try:
        ops.write_trigger("compile", saved, "api")
    except OSError as e:
        for tid in task_ids:
            try:
                db.update_compile_task(tid, "failed", error_msg=f"触发文件写入失败：{e}")
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"触发文件写入失败（{e}）。本批任务已置为失败。")

    audit_log(user.username, "upload", target_path=(",".join(saved))[:500],
              detail={"files": saved, "category": category})
    return UploadResult(ok=len(saved), errors=errors, task_ids=task_ids)


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(limit: int = 50,
               user: auth.User = Depends(auth.require_roles("user", "admin", "reviewer"))):
    """编译任务状态列表（最近 limit 条）。"""
    return db.list_recent_compile_tasks(limit)


@router.post("/tasks/{task_id}/retry", response_model=dict)
def retry_task(task_id: int,
               user: auth.User = Depends(auth.require_roles("user", "admin"))):
    """failed 任务重试：重新写触发文件 + 置回 pending。"""
    rows = db.list_recent_compile_tasks(10000)
    target = next((r for r in rows if r["id"] == task_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if target["status"] != "failed":
        raise HTTPException(status_code=409, detail=f"仅 failed 任务可重试（当前 status={target['status']}）")
    ops.write_trigger("compile", [target["raw_path"]], "api")
    db.update_compile_task(task_id, "pending")
    audit_log(user.username, "retry_compile", target_path=target["raw_path"],
              detail={"task_id": task_id})
    return {"message": "任务已重新加入编译队列", "task_id": task_id}