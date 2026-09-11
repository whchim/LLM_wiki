"""销售角色工作台：低负担提交纪要，按问题逐项补充事实。"""
from datetime import datetime, timezone

import streamlit as st

from api_client import ApiClient, ApiError


SOURCE_LABELS = {"meeting_note": "会后纪要", "transcript": "会议转写", "chat_summary": "聊天摘要"}
STATUS_LABELS = {
    "open": "等待澄清",
    "ready_for_proposal": "可生成建议",
    "needs_human_review": "转人工审核",
    "completed": "已完成",
    "cancelled": "已取消",
}


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _render_intake(api: ApiClient) -> None:
    st.subheader("提交洽谈纪要")
    with st.form("sales_intake_form"):
        customer_id = st.text_input("客户脱敏标识", placeholder="customer-demo-001")
        occurred_at = st.datetime_input("洽谈时间", value=datetime.now())
        source_type = st.selectbox("记录来源", list(SOURCE_LABELS), format_func=SOURCE_LABELS.get)
        content = st.text_area("纪要正文", height=180, placeholder="只填写已脱敏的中文洽谈事实，不要粘贴手机号、邮箱、密钥或精确金额。")
        idem = st.text_input("提交幂等标识", placeholder="同一份纪要重试时保持不变")
        submitted = st.form_submit_button("提交并进入澄清")
    if not submitted:
        return
    if not customer_id.strip() or not content.strip() or not idem.strip():
        st.warning("客户标识、纪要正文和幂等标识均不能为空。")
        return
    try:
        result = api.sales_intake(
            idempotency_key=idem.strip(), customer_id=customer_id.strip(), content=content,
            occurred_at=_iso_datetime(occurred_at), source_type=source_type)
        st.success(f"已创建澄清会话：{result['session']['session_id']}。等待 Agent 返回必要问题。")
        if result.get("gate", {}).get("numeric_ref_count"):
            st.info("检测到数值字段：正文仅保存引用，精确值进入受限数值表。")
        st.rerun()
    except ApiError as exc:
        detail = exc.message
        if isinstance(detail, dict):
            detail = "；".join(detail.get("errors", [detail.get("message", "提交失败")]))
        st.error(f"提交失败：{detail}")


def _render_session(api: ApiClient, session_id: str) -> None:
    try:
        session = api.clarification_session(session_id)
    except ApiError as exc:
        st.error(f"获取会话失败：{exc.message}")
        return
    st.markdown(f"**会话状态：{STATUS_LABELS.get(session['status'], session['status'])}**")
    st.caption(f"客户：{session['conversation_id']} · 轮次 {session['round_count']}/{session['max_rounds']}")
    turns = session.get("turns", [])
    answers = {item["question_id"]: item for item in session.get("answers", [])}
    if not turns:
        st.info("Agent 尚未返回追问；可刷新查看异步处理结果。")
        return
    for turn in turns:
        output = turn.get("agent_output") or {}
        with st.container(border=True):
            st.markdown(f"**第 {turn['turn_no']} 轮 · {turn['status']}**")
            if output.get("claims"):
                st.write("已识别事实")
                st.json(output["claims"])
            if output.get("missing_facts"):
                st.write("仍缺少")
                st.json(output["missing_facts"])
            for question in output.get("questions", []):
                qid = question["id"]
                if qid in answers:
                    st.success(f"已回答：{answers[qid]['answer_text_redacted']}")
                    continue
                with st.form(f"answer_{turn['turn_id']}_{qid}"):
                    answer = st.text_area(question["question"], key=f"text_{turn['turn_id']}_{qid}")
                    if st.form_submit_button("提交回答"):
                        if not answer.strip():
                            st.warning("回答不能为空。")
                        else:
                            try:
                                api.answer_clarification(session_id, turn["turn_id"], qid, answer.strip())
                                st.success("回答已追加保存。")
                                st.rerun()
                            except ApiError as exc:
                                st.error(f"提交回答失败：{exc.message}")
            cost = {"输入 token": turn.get("input_tokens") or 0,
                    "输出 token": turn.get("output_tokens") or 0,
                    "耗时 ms": turn.get("latency_ms") or 0}
            st.caption(" · ".join(f"{key}: {value}" for key, value in cost.items()))


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("销售澄清工作台")
    _render_intake(api)
    st.divider()
    st.subheader("我的澄清会话")
    try:
        sessions = api.my_clarification_sessions()
    except ApiError as exc:
        st.error(f"获取会话失败：{exc.message}")
        return
    if not sessions:
        st.info("暂无澄清会话。")
        return
    options = {f"{item['customer_id']} · {STATUS_LABELS.get(item['status'], item['status'])} · {item['session_id']}": item["session_id"] for item in sessions}
    selected = st.selectbox("选择会话", list(options))
    _render_session(api, options[selected])
