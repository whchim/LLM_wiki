"""自增长看板：统计/缺口经 API；周报直读共享卷（只读）。"""
import glob
import os
from pathlib import Path

import streamlit as st

from api_client import ApiClient, ApiError

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("自增长看板")

    try:
        stats = api.stats()
        top = api.missed(20).get("items", [])
    except ApiError as e:
        st.error(f"加载看板数据失败：{e.message}")
        return

    c1, c2 = st.columns(2)
    c1.metric("总搜索次数", stats.get("total", 0))
    c2.metric("未命中率", f"{stats.get('miss_rate', 0) * 100:.0f}%")
    st.divider()

    st.subheader("搜索未命中 Top 20（知识缺口）")
    if top:
        st.dataframe([{"缺口查询": t["query"], "搜索次数": t["cnt"], "最近出现": t["last_seen"]} for t in top],
                     use_container_width=True)
    else:
        st.info("暂无知识缺口记录——用户搜索都有结果。")

    st.divider()
    st.subheader("最近自增长周报")
    reports = sorted(glob.glob(str(Path(KB_ROOT) / "NEXUS" / "研究" / "自增长周报_*.md")), reverse=True)
    if reports:
        st.markdown(Path(reports[0]).read_text(encoding="utf-8"))
    else:
        st.info("尚无周报。Claude Code 执行 /process-growth 命令后生成（见 workflows/growth_workflow.md）。")

    st.divider()
    st.subheader("最近健康周报（SP5）")
    health = sorted(glob.glob(str(Path(KB_ROOT) / "NEXUS" / "研究" / "健康周报_*.md")), reverse=True)
    if health:
        st.markdown(Path(health[0]).read_text(encoding="utf-8"))
    else:
        st.info("尚无健康周报。Claude Code 执行 /health-check 命令后生成（见 workflows/health_workflow.md）。")