"""审核角色工作台：查看事实、证据、缺口和 Agent 运行成本。"""
import streamlit as st

from api_client import ApiClient, ApiError


STATUS_LABELS = {"open": "等待澄清", "needs_human_review": "转人工审核", "ready_for_proposal": "可生成建议",
                 "completed": "已完成", "cancelled": "已取消"}


def _detail(api: ApiClient, session_id: str) -> None:
    try:
        session = api.clarification_session(session_id)
    except ApiError as exc:
        st.error(f"获取会话失败：{exc.message}")
        return
    st.subheader(f"会话 {session_id}")
    st.metric("会话状态", STATUS_LABELS.get(session["status"], session["status"]))
    for turn in session.get("turns", []):
        output = turn.get("agent_output") or {}
        with st.container(border=True):
            st.markdown(f"**第 {turn['turn_no']} 轮 · {turn['status']}**")
            left, right = st.columns(2)
            left.metric("输入 token", turn.get("input_tokens") or 0)
            right.metric("输出 token", turn.get("output_tokens") or 0)
            st.caption(f"耗时 {turn.get('latency_ms') or 0} ms · 问题数 {turn.get('question_count') or 0}")
            tabs = st.tabs(["事实", "缺口与追问", "证据引用"])
            with tabs[0]:
                st.json(output.get("claims") or [])
            with tabs[1]:
                st.json({"missing_facts": output.get("missing_facts") or [], "questions": output.get("questions") or []})
            with tabs[2]:
                refs = []
                for claim in output.get("claims") or []:
                    refs.extend(claim.get("evidence_refs") or [])
                st.json(refs)
    if session.get("answers"):
        st.write("销售补充回答（追加记录）")
        st.dataframe(session["answers"], use_container_width=True, hide_index=True)


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("澄清审核")
    try:
        sessions = api.clarification_sessions()
    except ApiError as exc:
        st.error(f"获取会话失败：{exc.message}")
        return
    if not sessions:
        st.info("暂无澄清会话。")
        return
    options = {f"{item['conversation_id']} · {STATUS_LABELS.get(item['status'], item['status'])} · {item['session_id']}": item["session_id"] for item in sessions}
    selected = st.selectbox("选择会话", list(options))
    _detail(api, options[selected])
