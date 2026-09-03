import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

from db import ensure_schema
from login import require_login, logout
from api_client import ApiClient, ApiError

ensure_schema()  # 自愈：clone 后无需 init.sh 也能建表建目录（幂等）

st.set_page_config(page_title="LLM Wiki 管理台", layout="wide")

# ---- 登录守卫 ----
if not require_login():
    st.stop()

auth = st.session_state["auth"]
api = ApiClient(auth["token"])
st.session_state["api"] = api  # 供各页面 render() 使用
ROLE_LABEL = {"admin": "管理员", "reviewer": "审核者", "user": "普通用户"}

# ---- 侧边栏 ----
with st.sidebar:
    st.title("LLM Wiki 管理台")
    st.caption(f"当前用户: {auth.get('display_name') or auth['username']}（{ROLE_LABEL.get(auth['role'], auth['role'])}）")
    if st.button("退出登录"):
        logout()

    # 搜索（走 API，写入 search_logs；gap=知识缺口：grep 零命中且相似度低于阈值）
    query = st.text_input("搜索知识库（写入 search_logs）")
    if st.button("搜索") and query.strip():
        try:
            res = api.search(query.strip())
            if res.get("gap"):
                st.info(f"知识库中暂无与「{query}」直接相关的信息（语义相似度低于判定阈值，已记录为知识缺口）。")
            elif res["matches"]:
                st.success(f"命中 {res['matches']} 个文件：")
                for f in res["files"][:10]:
                    st.markdown(f"- `{f}`")
            else:
                st.info(f"知识库中暂无与「{query}」直接相关的信息。（已记录为知识缺口）")
        except ApiError as e:
            st.error(f"搜索失败：{e.message}")

    # 重建索引（admin）
    if auth["role"] == "admin":
        if st.button("重建索引（从 YAML 文件）"):
            try:
                n = api.rebuild_index()["entries"]
                st.success(f"已重建索引：{n} 条")
            except ApiError as e:
                st.error(f"操作失败：{e.message}")

    st.divider()

    # 导航（按角色收敛可见页面）
    pages = ["上传文档", "自增长看板", "可观测性"]
    if auth["role"] in ("admin", "reviewer"):
        pages.insert(1, "审核管理")
    page = st.radio("导航", pages)

# ---- 路由 ----
import upload, review, growth, obs
if page == "上传文档":
    if auth["role"] in ("admin", "reviewer", "user"):
        upload.render()
    else:
        st.warning("该页面需要登录权限。")
elif page == "审核管理":
    if auth["role"] in ("admin", "reviewer"):
        review.render()
    else:
        st.warning("审核操作仅管理员/审核者可用。")
elif page == "可观测性":
    obs.render()
else:
    growth.render()