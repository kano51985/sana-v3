"""Separated user preferences and administrator configuration boundary."""

from __future__ import annotations

import os

import streamlit as st

from sana.clients.streamlit.api_client import SanaAPIError
from sana.clients.streamlit.session import build_api_client, get_value, set_value


st.title("设置")
scope = st.segmented_control(
    "设置范围",
    ["用户设置", "管理员配置"],
    default="用户设置",
)

if scope == "用户设置":
    st.subheader("界面偏好")
    show_evidence = st.toggle(
        "运行完成后提示查看证据",
        value=bool(get_value(st.session_state, "show_evidence_after_run")),
    )
    set_value(st.session_state, "show_evidence_after_run", show_evidence)

    with st.container(border=True):
        st.subheader("当前身份")
        identity = get_value(st.session_state, "identity") or {}
        st.text(f"Tenant: {identity.get('tenant_id', '—')}")
        st.text(f"User: {identity.get('user_id', '—')}")
        st.caption(f"Issuer: {identity.get('issuer', '—')}")

    with st.container(border=True):
        st.subheader("API 连接")
        if os.environ.get("SANA_API_URL", "").strip():
            st.text("由部署环境管理")
        else:
            st.text(get_value(st.session_state, "api_url"))
        if st.button("检查连接", icon=":material/network_check:"):
            try:
                build_api_client(st.session_state).authenticate()
            except SanaAPIError as error:
                st.error(str(error), icon=":material/cloud_off:")
            else:
                st.success("API 身份验证正常。", icon=":material/check_circle:")
else:
    st.subheader("Provider 与模型")
    st.info(
        "Provider、模型路由、额度和密钥属于管理员服务端配置。"
        "此用户客户端不会读取、显示或写入任何模型密钥。",
        icon=":material/admin_panel_settings:",
    )
    with st.container(border=True):
        st.markdown("**当前边界**")
        st.write("用户可管理界面偏好；管理员配置通过受权限保护的运维入口管理。")
        st.caption("客户端没有本地 JSON、数据库或系统凭据访问能力。")
