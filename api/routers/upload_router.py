"""上传路由：上传/编译任务列表/失败重试。

复用 streamlit_app.ops.py 的上传校验与触发文件逻辑（共享模块），
FastAPI 侧只做 HTTP 层 + 审计。
"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

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
MAX_FILES = 20
MAX_BATCH_SIZE = 50 * 1024 * 1024
MAX_FILENAME_LENGTH = 180


@router.post("", response_model=UploadResult)
async def upload_files(
    files: list[UploadFile] = File(...),
    category: str = Form("个人_notes"),
    user: auth.User = Depends(auth.require_roles("user", "admin", "reviewer")),
) -> UploadResult:
    """上传文档：校验 → 落盘 RAW/<category>/ → 编译任务入库 → 写触发文件。"""
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"来源分类非法：{category}（可选 {CATEGORIES}）")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"单批最多上传 {MAX_FILES} 个文件")

    saved: list[str] = []
    errors: list[str] = []
    batch_size = 0
    for f in files:
        filename = f.filename or "未命名"
        name = Path(filename).name  # 净化：丢弃目录部分，防路径穿越
        if (not name or name in {".", ".."} or len(name) > MAX_FILENAME_LENGTH
                or any(ord(ch) < 32 for ch in name)):
            errors.append(f"{filename}：文件名非法或过长（最多 {MAX_FILENAME_LENGTH} 个字符）")
            continue
        if any(saved_path.endswith(f"/{name}") for saved_path in saved):
            errors.append(f"{filename}：本批次存在同名文件，已拒绝")
            continue
        try:
            # 分块读取并在超过上限时立即停止，避免先把超大请求完整载入内存。
            chunks = []
            total = 0
            too_large = False
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > ops.MAX_SIZE:
                    too_large = True
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
        except Exception as e:
            errors.append(f"{filename}：读取失败（{e}）。可能原因：文件损坏。建议：重新上传。")
            continue
        if too_large:
            errors.append(f"{filename}：文件大小超过限制（10MB）")
            continue
        if batch_size + len(content) > MAX_BATCH_SIZE:
            errors.append(f"{filename}：单批上传总大小不能超过 50MB")
            continue
        err = ops.validate_upload(name, len(content))
        if err:
            errors.append(f"{filename}：{err}")
            continue
        raw_dir = Path(_kb_root()) / "RAW" / category
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / name
        try:
            # xb 保证检查与创建是一个原子操作，避免并发请求互相覆盖。
            with target.open("xb") as out:
                out.write(content)
            batch_size += len(content)
        except FileExistsError:
            errors.append(f"{filename}：目标文件已存在，已拒绝覆盖")
            continue
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
def list_tasks(limit: int = Query(50, ge=1, le=500),
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
