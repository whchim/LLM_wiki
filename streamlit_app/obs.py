"""可观测性页（SP2.5）：Trace 指标看板——只读直查 trace_events（与 growth.py 同模式）。

4 项指标：
- 当日编译次数（compile_session 会话数 + 编译文件数）
- 检索成功/失败率（span_type=search）
- 平均响应延迟（按 span_type 分组）
- Top 失败模式（span_type × detail.error 聚合）
"""
import os

import streamlit as st

import db

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.get_conn() as conn:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _today() -> str:
    row = _rows("SELECT to_char(now(), 'YYYY-MM-DD') AS d")
    return row[0]["d"]


def _span_display() -> dict[str, str]:
    return {
        "compile_session": "编译会话",
        "search": "检索",
        "review_approve": "审核-通过",
        "review_reject": "审核-驳回",
        "review_resubmit": "审核-重提",
        "review_retry_ai": "审核-重试AI",
        "rebuild_index": "重建索引",
        "login": "登录",
    }


def render() -> None:
    st.header("可观测性")

    try:
        today = _today()
        # 1. 当日编译次数
        compile_stat = _rows(
            "SELECT COUNT(*) AS sessions, "
            "       COALESCE(SUM((detail->>'compiled')::int), 0) AS files, "
            "       COALESCE(SUM((detail->>'cached')::int), 0) AS cached, "
            "       COALESCE(AVG(latency_ms)::int, 0) AS avg_ms "
            "FROM trace_events WHERE span_type='compile_session' AND created_at >= %s",
            (today + " 00:00:00",))[0]

        # 2. 检索成功/失败率
        search_stat = _rows(
            "SELECT COUNT(*) AS total, "
            "       COALESCE(SUM((status='ok')::int),0) AS ok_cnt, "
            "       COALESCE(SUM((status='error')::int),0) AS err_cnt "
            "FROM trace_events WHERE span_type='search'")[0]

        # 3. 平均延迟（按 span_type 分组，整体在最前）
        latency = _rows(
            "SELECT span_type, AVG(latency_ms)::int AS avg_ms, COUNT(*) AS n "
            "FROM trace_events GROUP BY span_type ORDER BY n DESC LIMIT 8")

        # 4. Top 失败模式
        top_fail = _rows(
            "SELECT span_type, COALESCE(detail->>'error','unknown') AS err, COUNT(*) AS n "
            "FROM trace_events WHERE status='error' "
            "GROUP BY span_type, detail->>'error' ORDER BY n DESC LIMIT 10")
    except Exception as e:
        st.error(f"加载可观测性数据失败：{e}（请确认 trace_events 表已建立，或已有 trace 数据）")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当日编译会话", compile_stat["sessions"])
    c1.caption(f"编译文件 {compile_stat['files']} / 缓存命中 {compile_stat['cached']}")
    c2.metric("当日检索成功数", f"{search_stat['ok_cnt']}")
    c2.caption(f"失败 {search_stat['err_cnt']} / 总数 {search_stat['total']}")
    c3.metric("编译平均延迟", f"{compile_stat['avg_ms']} ms")
    c4.metric("检索成功率",
              f"{search_stat['ok_cnt'] / search_stat['total'] * 100:.0f}%" if search_stat["total"] else "-")

    st.divider()
    st.subheader("平均响应延迟（按类型）")
    if latency:
        sp = _span_display()
        st.dataframe(
            [{"类型": sp.get(r["span_type"], r["span_type"]),
              "平均延迟(ms)": r["avg_ms"], "次数": r["n"]} for r in latency],
            use_container_width=True)
    else:
        st.info("暂无 trace 数据——上传/检索/审核操作后这里才会有指标。")

    st.divider()
    st.subheader("Top 失败模式")
    if top_fail:
        sp = _span_display()
        st.dataframe(
            [{"类型": sp.get(r["span_type"], r["span_type"]),
              "错误": r["err"], "次数": r["n"]} for r in top_fail],
            use_container_width=True)
    else:
        st.info("暂无失败记录 🎉")