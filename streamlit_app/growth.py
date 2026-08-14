import glob
import os
from pathlib import Path

import streamlit as st

from db import get_conn, top_missed_queries, search_stats

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def render():
    st.header("自增长看板")
    stats = search_stats()
    c1, c2 = st.columns(2)
    c1.metric("总搜索次数", stats["total"])
    c2.metric("未命中率", f"{stats['miss_rate'] * 100:.0f}%")
    st.divider()

    st.subheader("搜索未命中 Top 20（知识缺口）")
    top = top_missed_queries(20)
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
