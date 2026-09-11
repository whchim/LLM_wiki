"""老板/负责人结果优先的客户状态工作台。"""
import streamlit as st

from api_client import ApiClient, ApiError


STATE_LABELS = {
    "new_lead": "新线索", "contacted": "已接触", "need_confirmed": "需求已确认",
    "solution_eval": "方案评估", "commercial_negotiation": "商务谈判",
    "won": "已赢单", "lost_or_paused": "流失/暂停", "expired": "已过期",
}


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("销售客户状态")
    st.caption("首屏只展示当前结果、风险和下一步；确认动作会写入不可变状态事件。")
    try:
        proposals = api.pending_state_proposals()
    except ApiError as exc:
        st.error(f"获取待确认建议失败：{exc.message}")
        return
    if not proposals:
        st.info("暂无待确认的客户状态建议。")
        return
    for proposal in proposals:
        proposal_id = proposal["proposal_id"]
        with st.container(border=True):
            st.subheader(f"客户 {proposal['customer_id']}")
            left, right = st.columns(2)
            left.metric("建议状态", STATE_LABELS.get(proposal["proposed_state"], proposal["proposed_state"]))
            if proposal.get("current_state"):
                st.caption(f"当前状态：{STATE_LABELS.get(proposal['current_state'], proposal['current_state'])}")
            right.metric("置信度", f"{float(proposal['confidence']):.0%}")
            if proposal.get("risk_flags"):
                st.warning("风险：" + "、".join(proposal["risk_flags"]))
            st.write("判断摘要：", proposal.get("reasoning_summary") or "—")
            st.write("下一步：", proposal.get("next_action") or "—")
            with st.expander("查看证据引用"):
                st.json(proposal.get("evidence_refs") or [])
            decision = st.radio("负责人决定", ["approved", "modified", "rejected"],
                                format_func={"approved": "确认", "modified": "修改后确认", "rejected": "驳回"}.get,
                                key=f"decision_{proposal_id}", horizontal=True)
            final_state = None
            if decision == "modified":
                final_state = st.selectbox("修改为", [
                    "new_lead", "contacted", "need_confirmed", "solution_eval",
                    "commercial_negotiation", "won", "lost_or_paused",
                ], key=f"state_{proposal_id}")
            reason = st.text_input("原因（驳回/修改时必填）", key=f"reason_{proposal_id}")
            if st.button("提交负责人决定", key=f"submit_{proposal_id}"):
                try:
                    api.decide_customer_state(proposal_id, decision, final_state, reason or None)
                    st.success("决定已记录，当前状态已更新。")
                    st.rerun()
                except ApiError as exc:
                    st.error(f"提交失败：{exc.message}")
            with st.expander("查看客户状态时间线"):
                try:
                    events = api.customer_state_events(proposal["customer_id"])
                    st.dataframe([
                        {"时间": event.get("effective_at") or event.get("created_at"),
                         "事件": event.get("event_type"),
                         "状态": STATE_LABELS.get(event.get("state"), event.get("state")),
                         "操作人": event.get("created_by"),
                         "原因": event.get("reason") or ""}
                        for event in events
                    ], use_container_width=True, hide_index=True)
                except ApiError as exc:
                    st.warning(f"时间线暂时不可用：{exc.message}")
