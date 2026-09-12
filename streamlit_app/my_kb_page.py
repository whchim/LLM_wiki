"""我的知识库：按流转阶段追溯"我提交了什么、现在到哪一步"。

现状边界（页面显式说明，不粉饰）：knowledge_entries 没有归属字段，
因此"我的"= 我上传过的（从 audit_logs 的 upload 事件追溯），
而不是"归我所有的"；条目一旦发布，系统无法反查作者。
"""
import streamlit as st

from api_client import ApiClient, ApiError

STAGE_ORDER = ["已发布", "待审核", "已编译", "编译中", "编译失败", "已驳回", "已上传"]
STAGE_HINT = {
    "已发布": "已通过审核进入企业知识库，全员可检索",
    "待审核": "编译产物正在等待 AI 六维度审核或人工放行",
    "已编译": "编译已完成，等待进入审核队列",
    "编译中": "Claude Code 正在处理（触发队列 watcher）",
    "编译失败": "编译中断，可在「上传文档」页重试",
    "已驳回": "审核未通过，可修改后重新提交",
    "已上传": "已落盘 RAW/，尚未开始编译",
}


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("我的知识库")
    st.caption("从个人沉淀到企业共享的流转视图：看得到自己提交的每一条内容现在到哪一步。")

    try:
        data = api.my_entries()
    except ApiError as e:
        st.error(f"加载失败：{e.message}")
        return

    items = data.get("items", [])
    counts = data.get("counts", {})

    # 流转阶段概览（结果优先：先给数字，再给列表）
    cols = st.columns(len(STAGE_ORDER))
    for col, stage in zip(cols, STAGE_ORDER):
        col.metric(stage, counts.get(stage, 0))

    if not items:
        st.info("你还没有提交过任何文档。到「上传文档」页上传 .md / .txt 后，这里会显示它从个人沉淀到企业共享的每一步。")
    else:
        st.divider()
        picked = st.selectbox("按阶段筛选", ["全部"] + [s for s in STAGE_ORDER if counts.get(s)])
        shown = items if picked == "全部" else [it for it in items if it["stage"] == picked]
        if picked != "全部":
            st.caption(STAGE_HINT.get(picked, ""))
        st.dataframe(
            [{
                "名称": it["raw_name"],
                "阶段": it["stage"],
                "编译": it.get("compile_status") or "—",
                "审核": it.get("review_decision") or "—",
                "类型": it.get("entry_type") or "—",
                "版本": it.get("entry_version") or "—",
                "来源分类": it["raw_path"].split("/")[1] if "/" in it["raw_path"] else "—",
                "提交时间": str(it.get("uploaded_at") or "—")[:19],
            } for it in shown],
            use_container_width=True, hide_index=True)

        # 逐条详情：只展开需要动作的（失败 / 驳回），其余折叠
        actionable = [it for it in shown if it["stage"] in ("编译失败", "已驳回")]
        if actionable:
            st.subheader("需要处理")
            for it in actionable:
                with st.container(border=True):
                    st.markdown(f"**{it['raw_name']}** — {it['stage']}")
                    if it.get("compile_error"):
                        st.error(f"编译错误：{it['compile_error']}")
                    if it.get("reject_reason"):
                        st.warning(f"驳回原因：{it['reject_reason']}")
                    st.caption(f"源文件：`{it['raw_path']}`")

    # 当前用户可见的全部知识条目（企业共享层，与上面"我的提交"区分开）
    st.divider()
    with st.expander("浏览企业知识库全部条目（不分作者）"):
        try:
            all_entries = api.entries()
        except ApiError as e:
            st.error(f"加载条目失败：{e.message}")
        else:
            total = all_entries.get("total", 0)
            st.caption(f"共 {total} 条。当前知识库为空时，上传文档并完成编译审核后此处会增长。")
            if total:
                st.dataframe(all_entries.get("items", []), use_container_width=True, hide_index=True)

    with st.expander("关于「我的」口径（已知边界）"):
        st.markdown(
            "- 系统目前**没有给知识条目记录归属人**（`knowledge_entries` 无 submitter/owner 字段，"
            "`contributors` 表已建但尚未写入）。\n"
            "- 因此本页的「我的」是**我上传过的**——依据上传审计事件追溯，"
            "而不是「归我所有的」。\n"
            "- 条目一旦发布进入 `NEXUS/`，系统**无法反查作者**；"
            "要支持按人过滤需给条目补归属字段（属后续迭代）。\n"
            "- 审核结论（通过/驳回）按文件名关联，同一文件名被多次提交时取最近结论。"
        )
