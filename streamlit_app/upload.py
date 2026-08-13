import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from db import get_conn, insert_compile_task, update_compile_task
from ops import validate_upload, sha256_file, write_trigger

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
CATEGORIES = ["个人_notes", "会议", "经验", "项目"]


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
        ok, err = 0, 0
        for f in files:
            err_msg = validate_upload(f.name, f.size)
            if err_msg:
                st.error(f"{f.name}：{err_msg}")
                err += 1
                continue
            raw_dir = Path(KB_ROOT) / "RAW" / category
            raw_dir.mkdir(parents=True, exist_ok=True)
            dst = raw_dir / f.name
            dst.write_bytes(f.getbuffer())
            fingerprint = sha256_file(str(dst))
            insert_compile_task(f"RAW/{category}/{f.name}", fingerprint)
            ok += 1
        if ok:
            # 收集本批路径，写一个触发文件
            paths = [f"RAW/{category}/{f.name}" for f in files
                     if validate_upload(f.name, f.size) is None]
            write_trigger("compile", paths, "streamlit")
            st.success(f"{ok} 个文件已加入编译队列（{err} 个失败）。Claude Code 处理中——状态见下表。")

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
    else:
        st.info("暂无编译任务。上传文件后等待 Claude Code 处理。")
