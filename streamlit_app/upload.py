"""上传页：校验 + 落盘 RAW + 编译任务入库 + 触发文件 + 任务状态表（设计文档 9.3）。"""
import os
from pathlib import Path

import streamlit as st

from db import get_conn, insert_compile_task, update_compile_task
from ops import validate_upload, sha256_file, write_trigger

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
CATEGORIES = ["个人_notes", "会议", "经验", "项目"]


def _mark_failed(task_ids: list[int], msg: str) -> None:
    """补偿：把已插入的任务置为 failed，避免「pending 任务无触发文件」的永久挂起。"""
    for tid in task_ids:
        try:
            update_compile_task(tid, "failed", error_msg=msg)
        except Exception:
            pass  # 补偿尽力而为，不掩盖原始错误


def _process_upload(files, category: str) -> tuple[int, list[str]]:
    """单批上传：校验 → 落盘 RAW → 插 pending 任务 → 写触发文件。

    返回 (ok, errs)：ok 为成功数；errs 为逐条错误文案（PRD 9.3 三要素：
    问题描述/可能原因/建议操作，含受影响文件）。一致性保证：先插任务后写触发，
    触发失败时把已插任务成对补偿为 failed——任何情况下不残留无触发的 pending。
    """
    saved_paths: list[str] = []
    errs: list[str] = []
    for f in files:
        err_msg = validate_upload(f.name, f.size)
        if err_msg:
            errs.append(f"{f.name}：{err_msg}")
            continue
        raw_dir = Path(KB_ROOT) / "RAW" / category
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f.name).write_bytes(f.getbuffer())
        except OSError as e:
            errs.append(f"{f.name}：保存到 RAW 失败（{e}）。可能原因：磁盘空间不足或写入权限不足。"
                        f"建议：检查磁盘与权限后重试。")
            continue
        saved_paths.append(f"RAW/{category}/{f.name}")

    if not saved_paths:
        return 0, errs

    task_ids: list[int] = []
    try:
        for path in saved_paths:
            fingerprint = sha256_file(str(Path(KB_ROOT) / path))
            task_ids.append(insert_compile_task(path, fingerprint))
    except Exception as e:
        _mark_failed(task_ids, f"批处理中断（部分任务未入库）：{e}")
        errs.append(f"任务入库失败（{e}）。受影响文件：{'、'.join(saved_paths)}。"
                    f"可能原因：数据库锁定（busy_timeout 5s）或 meta.db 损坏。"
                    f"建议：稍后重试，或重建索引。")
        return 0, errs

    try:
        write_trigger("compile", saved_paths, "streamlit")
    except OSError as e:
        _mark_failed(task_ids, f"触发文件写入失败：{e}")
        errs.append(f"触发文件写入失败（{e}）。受影响文件：{'、'.join(saved_paths)}。"
                    f"可能原因：_triggers 目录不可写。本批任务已撤销（置为失败）。"
                    f"建议：检查目录权限后重试。")
        return 0, errs

    return len(saved_paths), errs


def render():
    st.header("上传文档")
    with st.form("upload_form"):
        files = st.file_uploader("选择文档（.md/.txt/.pdf/.docx，≤10MB）",
                                 type=["md", "txt", "pdf", "docx"], accept_multiple_files=True)
        category = st.selectbox("来源分类", CATEGORIES)
        submitted = st.form_submit_button("上传并加入编译队列")

    if submitted:
        if not files:
            st.warning("请先选择文件。")
            return
        ok, errs = _process_upload(files, category)
        for msg in errs:
            st.error(msg)
        if ok:
            st.success(f"{ok} 个文件已加入编译队列（{len(errs)} 个失败）。Claude Code 处理中——状态见下表。")

    st.divider()
    st.subheader("编译任务状态")
    if st.button("刷新"):
        st.rerun()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_path, status, error_msg, completed_at "
            "FROM compile_tasks ORDER BY id DESC LIMIT 50").fetchall()
    if rows:
        data = [{"任务": r[0], "文件": r[1], "状态": r[2], "错误": r[3] or "", "完成时间": r[4] or ""} for r in rows]
        st.dataframe(data, use_container_width=True)
        # 重试：仅 failed 行
        failed = [r for r in rows if r[2] == "failed"]
        if failed:
            st.warning(f"{len(failed)} 个任务失败。")
            for r in failed:
                if st.button(f"重试：{r[1]}", key=f"retry_{r[0]}"):
                    write_trigger("compile", [r[1]], "streamlit")
                    update_compile_task(r[0], "pending")
                    st.success("已重新加入编译队列。")
                    st.rerun()
    else:
        st.info("暂无编译任务。上传文件后等待 Claude Code 处理。")
