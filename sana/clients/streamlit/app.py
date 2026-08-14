"""Sana's API-only Streamlit entrypoint."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from sana.clients.streamlit.api_client import SanaAPIClient, SanaAPIError
from sana.clients.streamlit.session import (
    build_api_client,
    get_value,
    initialize_session,
    is_authenticated,
    login_session,
    logout_session,
    set_value,
)


st.set_page_config(
    page_title="Sana",
    page_icon=":material/neurology:",
    layout="wide",
)
initialize_session(st.session_state)


if not is_authenticated(st.session_state):
    st.title("连接 Sana", text_alignment="center")
    st.caption(
        "使用平台签发的访问令牌登录。令牌只保存在当前浏览器会话中。",
        text_alignment="center",
    )
    login_column = st.columns([1, 1.25, 1])[1]
    with login_column.container(border=True):
        with st.form("sana_login"):
            api_url = st.text_input(
                "API 地址",
                value=get_value(st.session_state, "api_url"),
                placeholder="http://localhost:8000",
            )
            access_token = st.text_input(
                "访问令牌",
                type="password",
                placeholder="OIDC token 或本地开发 token",
            )
            submitted = st.form_submit_button(
                "登录",
                icon=":material/login:",
                type="primary",
                width="stretch",
            )
        if submitted:
            try:
                candidate = SanaAPIClient(api_url, access_token)
                identity = candidate.authenticate()
            except (SanaAPIError, ValueError) as error:
                st.error(str(error), icon=":material/error:")
            else:
                login_session(
                    st.session_state,
                    api_url=api_url,
                    access_token=access_token,
                    identity=identity,
                )
                st.rerun()
    st.stop()


client = build_api_client(st.session_state)
conversations = get_value(st.session_state, "conversations")
if not conversations:
    try:
        conversations = client.list_conversations()
        set_value(st.session_state, "conversations", conversations)
    except SanaAPIError as error:
        st.sidebar.error(str(error), icon=":material/cloud_off:")


views_dir = Path(__file__).parent / "views"
page = st.navigation(
    [
        st.Page(
            views_dir / "chat.py",
            title="对话",
            icon=":material/chat:",
            default=True,
        ),
        st.Page(
            views_dir / "evidence.py",
            title="证据",
            icon=":material/fact_check:",
        ),
        st.Page(
            views_dir / "settings.py",
            title="设置",
            icon=":material/settings:",
        ),
    ],
    position="top",
)


with st.sidebar:
    st.header("Sana")
    identity = get_value(st.session_state, "identity") or {}
    st.badge("已连接", icon=":material/check:", color="green")
    st.caption(f"用户 {str(identity.get('user_id', ''))[:8]}…")

    with st.container(horizontal=True):
        if st.button("新会话", icon=":material/add:", type="primary"):
            try:
                created = client.create_conversation()
            except SanaAPIError as error:
                st.error(str(error), icon=":material/error:")
            else:
                conversations = [created, *conversations]
                set_value(st.session_state, "conversations", conversations)
                set_value(st.session_state, "selected_conversation_id", created["id"])
                st.rerun()
        if st.button("刷新", icon=":material/refresh:"):
            try:
                conversations = client.list_conversations()
            except SanaAPIError as error:
                st.error(str(error), icon=":material/error:")
            else:
                set_value(st.session_state, "conversations", conversations)
                st.rerun()

    if conversations:
        identifiers = [str(item["id"]) for item in conversations]
        selected = get_value(st.session_state, "selected_conversation_id")
        if selected not in identifiers:
            selected = identifiers[0]
            set_value(st.session_state, "selected_conversation_id", selected)
        titles = {
            str(item["id"]): item.get("title") or "未命名会话"
            for item in conversations
        }
        chosen = st.selectbox(
            "当前会话",
            identifiers,
            index=identifiers.index(selected),
            format_func=lambda item: titles[item],
        )
        if chosen != selected:
            set_value(st.session_state, "selected_conversation_id", chosen)
            set_value(st.session_state, "active_run_id", None)
            set_value(st.session_state, "active_run", None)
            st.rerun()
    else:
        st.caption("还没有会话。创建一个会话后即可开始。")

    if st.button("退出登录", icon=":material/logout:"):
        logout_session(st.session_state)
        st.rerun()


page.run()
