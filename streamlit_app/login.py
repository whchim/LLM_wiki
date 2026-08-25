"""登录页：用户名/密码 → API /auth/login → 写 st.session_state["auth"]。"""
import streamlit as st

from api_client import ApiClient, ApiError


def render_login() -> None:
    """渲染登录表单；成功后写入 session_state 并 rerun。"""
    st.title("LLM Wiki 管理台")
    st.caption("Phase 2：登录后按角色（管理员/审核者/普通用户）开放对应操作")
    with st.form("login_form"):
        username = st.text_input("用户名")
        password = st.text_input("密码", type="password")
        submitted = st.form_submit_button("登录")

    if submitted:
        if not username or not password:
            st.error("请输入用户名和密码。")
            return
        try:
            resp = ApiClient.login(username, password)
            st.session_state["auth"] = {
                "token": resp["access_token"],
                "role": resp["role"],
                "display_name": resp.get("display_name") or username,
                "username": username,
            }
            st.rerun()
        except ApiError as e:
            st.error(f"登录失败：{e.message}")


def require_login() -> bool:
    """主界面守卫：未登录 → 渲染登录页并返回 False。"""
    if "auth" not in st.session_state:
        render_login()
        return False
    return True


def logout() -> None:
    st.session_state.pop("auth", None)
    st.rerun()