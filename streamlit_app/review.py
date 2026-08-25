"""审核页：待审/AI 判定/通过/驳回/重提 —— 写操作经 FastAPI（SP2），预览直读共享卷。"""
import json
import os
from pathlib import Path

import streamlit as st

from api_client import ApiClient, ApiError

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def _load_scores(rec: dict) -> dict | None:
    """ai_scores 可能是 dict（API 已解析 JSONB）或 str。"""
    s = rec.get("ai_scores")
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return None


def _preview(nexus_path: str) -> str | None:
    """只读预览：API 与 Streamlit 共享 vault 卷，路径一致可直接读。"""
    full = Path(KB_ROOT) / nexus_path
    if full.exists():
        try:
            return full.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _render_pending(api: ApiClient, rec: dict) -> None:
    title = rec.get("title") or rec["nexus_path"].split("/")[-1]
    with st.expander(f"{title} — AI判定：{rec.get('ai_verdict') or '未完成'}"):
        scores = _load_scores(rec)
        if isinstance(scores, dict):
            cols = st.columns(5)
            s = scores.get("scores", {})
            cols[0].metric("完整性", s.get("completeness", "-"))
            cols[1].metric("去重", s.get("dedup", "-"))
            cols[2].metric("质量", f"{s.get('quality', '-')}/5")
            cols[3].metric("敏感信息", s.get("sensitive", "-"))
            cols[4].metric("合规", s.get("compliance", "-"))
            st.caption(f"职务归属：{scores.get('department', '-')} | 摘要：{scores.get('summary', '')}")
            if scores.get("concerns"):
                st.warning("关注项：" + "；".join(scores["concerns"]))
        else:
            st.warning("AI 审核未完成或失败。可人工审核，或 [重试 AI 审核]。")

        preview = _preview(rec["nexus_path"])
        if preview:
            with st.container(border=True):
                st.markdown(preview)
        else:
            st.info("（预览不可用：请用 Obsidian 打开 vault/ 查看）")

        c1, c2, c3 = st.columns(3)
        if c1.button("✓ 通过", key=f"ok_{rec['id']}"):
            try:
                res = api.approve(rec["id"])
                st.success(f"已通过：{res.get('target_path')}")
                st.rerun()
            except ApiError as e:
                st.error(f"操作失败：{e.message}")
        if c2.button("✗ 驳回", key=f"no_{rec['id']}"):
            st.session_state[f"rejecting_{rec['id']}"] = True
        if st.session_state.get(f"rejecting_{rec['id']}"):
            reason = st.text_input("驳回原因（必填）", key=f"reason_{rec['id']}")
            if st.button("确认驳回", key=f"confirm_{rec['id']}"):
                if not reason.strip():
                    st.error("驳回原因不能为空。")
                else:
                    try:
                        api.reject(rec["id"], reason)
                        st.session_state.pop(f"rejecting_{rec['id']}", None)
                        st.success("已驳回。")
                        st.rerun()
                    except ApiError as e:
                        st.session_state.pop(f"rejecting_{rec['id']}", None)
                        st.error(f"操作失败：{e.message}")
        if c3.button("重试 AI 审核", key=f"ai_{rec['id']}"):
            try:
                api.retry_ai(rec["id"])
                st.success("已加入 AI 审核队列。")
            except ApiError as e:
                st.error(f"操作失败：{e.message}")


def render() -> None:
    api: ApiClient = st.session_state["api"]
    st.header("审核管理")

    try:
        pending = api.list_pending()
        rejected = api.list_rejected()
    except ApiError as e:
        st.error(f"加载审核列表失败：{e.message}")
        return

    st.subheader(f"待审核（{len(pending)} 条）")
    if not pending:
        st.info("暂无待审核条目。")
    for rec in pending:
        _render_pending(api, rec)

    if rejected:
        st.divider()
        st.subheader("已驳回（可重新提交）")
        for rec in rejected:
            title = rec.get("title") or rec["nexus_path"].split("/")[-1]
            with st.expander(f"{title} — 驳回原因：{rec.get('reject_reason')}"):
                if st.button("重新提交审核", key=f"rs_{rec['id']}"):
                    try:
                        api.resubmit(rec["id"])
                        st.success("已重新提交 AI 审核。")
                        st.rerun()
                    except ApiError as e:
                        st.error(f"操作失败：{e.message}")