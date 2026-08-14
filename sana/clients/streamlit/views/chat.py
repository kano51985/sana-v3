"""Conversation page with resumable run progress streaming."""

from __future__ import annotations

from typing import Any

import streamlit as st

from sana.clients.streamlit.api_client import SanaAPIError, ServerSentEvent
from sana.clients.streamlit.session import (
    build_api_client,
    get_value,
    set_value,
)


_MODE_LABELS = {"FAST": "快速", "RESEARCH": "研究"}
_ROUTING_REASON_LABELS = {
    "single_or_low_complexity_fact": "单一或低复杂度事实",
    "three_or_more_required_facts": "包含三个以上必需事实",
    "comparison_request": "需要比较多个对象",
    "fresh_multi_fact": "需要核验多项最新事实",
    "high_consequence": "结论具有较高影响",
    "valuable_required_gap": "快速检索仍有高价值缺口",
}
_EVENT_LABELS = {
    "RUN_QUEUED": "请求已入队",
    "STEP_STARTED": "正在执行",
    "STEP_COMPLETED": "阶段完成",
    "RUN_UPGRADED": "已自动升级为研究模式",
    "RUN_COMPLETED": "回答已完成",
    "RUN_SUCCEEDED": "回答已完成",
    "RUN_FAILED": "运行失败",
    "RUN_CANCELLED": "运行已取消",
}


def _message_role(value: Any) -> str:
    normalized = str(value or "").casefold()
    return "assistant" if normalized == "assistant" else "user"


def _event_text(event: ServerSentEvent) -> str | None:
    for key in ("delta", "text_delta", "answer_delta"):
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            return value
    if event.is_terminal:
        for key in ("answer", "text", "content"):
            value = event.payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


client = build_api_client(st.session_state)
conversation_id = get_value(st.session_state, "selected_conversation_id")
st.title("与 Sana 对话")
st.caption("搜索模式由系统根据问题复杂度自动选择，并在运行中显示理由。")

if conversation_id is None:
    st.info("请先从侧边栏创建一个会话。", icon=":material/add_comment:")
    st.stop()

messages_by_conversation = get_value(st.session_state, "messages_by_conversation")
if conversation_id not in messages_by_conversation:
    try:
        messages_by_conversation[conversation_id] = client.list_messages(conversation_id)
    except SanaAPIError as error:
        st.error(str(error), icon=":material/error:")
        st.stop()
messages = messages_by_conversation[conversation_id]

for message in messages:
    role = _message_role(message.get("role"))
    with st.chat_message(role):
        st.markdown(str(message.get("content") or ""))
        if message.get("answer_quality") == "PARTIAL":
            st.caption("部分回答 · 仍有事实缺口")

active_run_id = get_value(st.session_state, "active_run_id")
active_run = get_value(st.session_state, "active_run")

if active_run_id:
    try:
        active_run = client.get_run(active_run_id)
        set_value(st.session_state, "active_run", active_run)
    except SanaAPIError as error:
        st.warning(str(error), icon=":material/cloud_off:")

if active_run:
    with st.container(horizontal=True, vertical_alignment="center"):
        mode = str(active_run.get("mode") or "")
        st.badge(
            f"{_MODE_LABELS.get(mode, mode)}模式",
            icon=":material/route:",
            color="blue" if mode == "FAST" else "violet",
        )
        st.caption(
            " · ".join(
                _ROUTING_REASON_LABELS.get(str(item), str(item))
                for item in active_run.get("routing_reason_codes") or ()
            )
            or "系统自动路由"
        )
        if active_run_id and st.button(
            "取消运行",
            icon=":material/stop_circle:",
        ):
            try:
                client.cancel_run(active_run_id)
            except SanaAPIError as error:
                st.error(str(error), icon=":material/error:")
            else:
                set_value(st.session_state, "active_run_id", None)
                set_value(st.session_state, "active_run", None)
                st.toast("运行已取消", icon=":material/check_circle:")
                st.rerun()

terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELLED"}
if active_run_id and active_run and active_run.get("status") not in terminal_statuses:
    with st.chat_message("assistant"):
        progress = st.status("正在连接运行进度…", expanded=True)

        def response_stream():
            for event in client.iter_run_events(
                active_run_id,
                after_sequence=get_value(st.session_state, "last_event_id"),
            ):
                set_value(st.session_state, "last_event_id", event.sequence)
                get_value(st.session_state, "run_events").append(
                    {
                        "sequence": event.sequence,
                        "event_type": event.event_type,
                        "payload": event.payload,
                    }
                )
                progress.update(
                    label=_EVENT_LABELS.get(event.event_type, event.event_type),
                    state="complete" if event.is_terminal else "running",
                )
                chunk = _event_text(event)
                if chunk:
                    yield chunk

        try:
            streamed = st.write_stream(response_stream())
            finished_run = client.get_run(active_run_id)
        except SanaAPIError as error:
            progress.update(label="进度连接中断，可安全重连", state="error")
            st.warning(str(error), icon=":material/sync_problem:")
        else:
            response_text = streamed if isinstance(streamed, str) else ""
            if response_text:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response_text,
                        "run_id": str(active_run_id),
                        "answer_quality": finished_run.get("answer_quality"),
                    }
                )
            set_value(st.session_state, "selected_evidence_run_id", str(active_run_id))
            set_value(st.session_state, "active_run_id", None)
            set_value(st.session_state, "active_run", None)
            if get_value(st.session_state, "show_evidence_after_run"):
                st.toast("证据与事实覆盖已可查看", icon=":material/fact_check:")
            st.rerun()
elif active_run_id and active_run:
    set_value(st.session_state, "selected_evidence_run_id", str(active_run_id))
    set_value(st.session_state, "active_run_id", None)
    set_value(st.session_state, "active_run", None)

if not messages:
    suggestions = {
        "比较两个方案": "比较 PostgreSQL 与 MongoDB 在多用户检索系统中的取舍。",
        "核验最新事实": "核验今天一个值得关注的 AI 工程新闻，并给出来源。",
        "做深入研究": "系统分析现代搜索代理的证据链架构与主要失败模式。",
    }
    selected_suggestion = st.pills(
        "可以这样开始",
        list(suggestions),
        label_visibility="collapsed",
    )
    if selected_suggestion:
        st.session_state["sana_chat_input"] = suggestions[selected_suggestion]

prompt = st.chat_input(
    "向 Sana 提问",
    key="sana_chat_input",
    max_chars=100_000,
    disabled=bool(get_value(st.session_state, "active_run_id")),
    submit_mode="disable",
)
if prompt:
    try:
        receipt = client.submit_message(conversation_id, prompt)
        run = client.get_run(receipt["search_run_id"])
    except SanaAPIError as error:
        st.error(str(error), icon=":material/error:")
    else:
        messages.append(
            {
                "id": receipt["message_id"],
                "role": "user",
                "content": prompt,
                "run_id": receipt["search_run_id"],
                "run_status": receipt["status"],
            }
        )
        set_value(st.session_state, "active_run_id", receipt["search_run_id"])
        set_value(st.session_state, "active_run", run)
        set_value(st.session_state, "run_events", [])
        set_value(st.session_state, "last_event_id", 0)
        st.rerun()
