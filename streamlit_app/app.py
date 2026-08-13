import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
from db import rebuild_index, insert_search_log, get_conn
from ops import write_trigger

st.set_page_config(page_title="LLM Wiki 管理台", layout="wide")

# ---- 侧边栏 ----
with st.sidebar:
    st.title("LLM Wiki 管理台")
    role = st.selectbox("视角（Demo 单用户，仅提示）",
                        ["贡献者", "审核者", "消费者", "管理员"])
    query = st.text_input("搜索知识库（写入 search_logs）")
    if st.button("搜索") and query.strip():
        # grep 同款：NEXUS 下匹配行数
        import subprocess
        kb = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
        nexus = os.path.join(kb, "NEXUS")
        res = subprocess.run(
            ["grep", "-rl", "--include=*.md", query.strip(), nexus],
            capture_output=True, text=True)
        files = [l for l in res.stdout.splitlines() if l.strip()]
        insert_search_log(query.strip(), len(files), "streamlit")
        if files:
            st.success(f"命中 {len(files)} 个文件：")
            for f in files[:10]:
                st.markdown(f"- `{os.path.relpath(f, kb).replace(os.sep, '/')}`")
        else:
            st.info(f"知识库中暂无与「{query}」直接相关的信息。（已记录为知识缺口）")
    if st.button("重建索引（从 YAML 文件）"):
        n = rebuild_index()
        st.success(f"已重建索引：{n} 条")
    st.divider()
    page = st.radio("导航", ["上传文档", "审核管理", "自增长看板"])

# ---- 路由 ----
import upload, review, growth
if page == "上传文档":
    upload.render()
elif page == "审核管理":
    review.render()
else:
    growth.render()
