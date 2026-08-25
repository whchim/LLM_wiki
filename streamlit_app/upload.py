"""上传页：文件上传 → API（校验/落盘 RAW/任务入库/触发文件）+ 任务状态表。"""
import streamlit as st

from api_client import ApiClient, ApiError

CATEGORIES = ["个人_notes", "会议", "经验", "项目"]


def render() -> None:
    """上传页（SP2）：所有操作经 FastAPI，不再直连 PG/文件系统。"""
    api: ApiClient = st.session_state["api"]
    st.header("上传文档")

    with st.form("upload_form"):
        files = st.file_uploader("选择文档（.md/.txt/.pdf/.docx，≤10MB）",
                                 type=["md", "txt", "pdf", "docx"], accept_multiple_files=True)
        category = st.selectbox("来源分类", CATEGORIES)
        submitted = st.form_submit_button("上传并加入编译队列")

    if submitted:
        if not files:
            st.warning("请先选择文件。")
        else:
            # 组装 multipart：field="files"，返回 (field, (filename, bytes, ctype))
            parts = [("files", (f.name, f.getvalue(), f.type or "application/octet-stream"))
                     for f in files]
            try:
                resp = api.upload(parts, category)
                for msg in resp.get("errors", []):
                    st.error(msg)
                if resp.get("ok"):
                    st.success(f"{resp['ok']} 个文件已加入编译队列（{len(resp.get('errors', []))} 个失败）。"
                               f"Claude Code 处理中——状态见下表。")
            except ApiError as e:
                st.error(f"上传失败：{e.message}")

    st.divider()
    st.subheader("编译任务状态")
    if st.button("刷新"):
        st.rerun()
    try:
        rows = api.list_tasks(50)
    except ApiError as e:
        st.error(f"获取任务列表失败：{e.message}")
        return
    if rows:
        data = [{"任务": r["id"], "文件": r["raw_path"], "状态": r["status"],
                 "错误": r["error_msg"] or "", "完成时间": r["completed_at"] or ""} for r in rows]
        st.dataframe(data, use_container_width=True)
        failed = [r for r in rows if r["status"] == "failed"]
        if failed:
            st.warning(f"{len(failed)} 个任务失败。")
            for r in failed:
                if st.button(f"重试：{r['raw_path']}", key=f"retry_{r['id']}"):
                    try:
                        api.retry_task(r["id"])
                        st.success("已重新加入编译队列。")
                        st.rerun()
                    except ApiError as e:
                        st.error(f"重试失败：{e.message}")
    else:
        st.info("暂无编译任务。上传文件后等待 Claude Code 处理。")