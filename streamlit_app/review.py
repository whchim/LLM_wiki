import json
import os
from pathlib import Path

import streamlit as st

from db import get_conn, list_pending_reviews, list_rejected_reviews
from ops import approve_entry, reject_entry, resubmit, write_trigger

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def render():
    st.header("审核管理")
    pending = list_pending_reviews()
    st.subheader(f"待审核（{len(pending)} 条）")
    if not pending:
        st.info("暂无待审核条目。")
    for rec in pending:
        with st.expander(f"{rec.get('title') or Path(rec['nexus_path']).stem} — AI判定：{rec['ai_verdict'] or '未完成'}"):
            try:
                scores = json.loads(rec["ai_scores"])
            except (json.JSONDecodeError, TypeError):
                scores = None
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
            full = Path(KB_ROOT) / rec["nexus_path"]
            if full.exists():
                with st.container(border=True):
                    st.markdown(full.read_text(encoding="utf-8"))
            c1, c2, c3 = st.columns(3)
            if c1.button("✓ 通过", key=f"ok_{rec['id']}"):
                new_path = "NEXUS/概念/" + Path(rec["nexus_path"]).name
                approve_entry(rec["id"], rec["nexus_path"], new_path)
                st.success(f"已通过：{new_path}")
                st.rerun()
            if c2.button("✗ 驳回", key=f"no_{rec['id']}"):
                st.session_state[f"rejecting_{rec['id']}"] = True
            if st.session_state.get(f"rejecting_{rec['id']}"):
                reason = st.text_input("驳回原因（必填）", key=f"reason_{rec['id']}")
                if st.button("确认驳回", key=f"confirm_{rec['id']}"):
                    if not reason.strip():
                        st.error("驳回原因不能为空。")
                    else:
                        reject_entry(rec["id"], rec["nexus_path"], reason)
                        st.session_state.pop(f"rejecting_{rec['id']}", None)
                        st.success("已驳回。")
                        st.rerun()
            if c3.button("重试 AI 审核", key=f"ai_{rec['id']}"):
                write_trigger("review", [rec["nexus_path"]], "streamlit")
                st.success("已加入 AI 审核队列。")

    rejected = list_rejected_reviews()
    if rejected:
        st.divider()
        st.subheader("已驳回（可重新提交）")
        for rec in rejected:
            with st.expander(f"{rec.get('title') or Path(rec['nexus_path']).stem} — 驳回原因：{rec['reject_reason']}"):
                if st.button("重新提交审核", key=f"rs_{rec['id']}"):
                    resubmit(rec["id"], rec["nexus_path"])
                    write_trigger("review", [rec["nexus_path"]], "streamlit")
                    st.success("已重新提交 AI 审核。")
                    st.rerun()
