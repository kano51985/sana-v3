"""Evidence and uncovered-fact inspection page."""

from __future__ import annotations

from collections import defaultdict

import streamlit as st

from sana.clients.streamlit.api_client import SanaAPIError
from sana.clients.streamlit.session import build_api_client, get_value, set_value


client = build_api_client(st.session_state)
st.title("证据与事实覆盖")
st.caption("这里展示服务端已验证的原文片段、来源链接，以及未能覆盖的必需事实。")

messages_by_conversation = get_value(st.session_state, "messages_by_conversation")
conversation_id = get_value(st.session_state, "selected_conversation_id")
messages = messages_by_conversation.get(conversation_id, [])
run_ids = list(
    dict.fromkeys(
        str(message["run_id"])
        for message in reversed(messages)
        if message.get("run_id")
    )
)
selected_run_id = get_value(st.session_state, "selected_evidence_run_id")
if selected_run_id and selected_run_id not in run_ids:
    run_ids.insert(0, selected_run_id)

if not run_ids:
    st.info("完成一次搜索后，可在这里检查证据。", icon=":material/fact_check:")
    st.stop()

if selected_run_id not in run_ids:
    selected_run_id = run_ids[0]
selected_run_id = st.selectbox(
    "运行",
    run_ids,
    index=run_ids.index(selected_run_id),
    format_func=lambda value: f"{value[:8]}…",
)
set_value(st.session_state, "selected_evidence_run_id", selected_run_id)

try:
    run = client.get_run(selected_run_id)
    report = client.get_evidence(selected_run_id)
except SanaAPIError as error:
    st.error(str(error), icon=":material/error:")
    st.stop()

with st.container(horizontal=True):
    st.metric("模式", run.get("mode", "—"))
    st.metric("状态", run.get("status", "—"))
    st.metric("回答质量", run.get("answer_quality", "—"))

if run.get("answer_quality") == "PARTIAL":
    st.warning(
        "这是部分回答。下方列出了仍未覆盖的必需事实。",
        icon=":material/warning:",
    )

missing_facts = list(report.get("missing_facts") or ())
st.subheader(f"缺失事实 · {len(missing_facts)}")
if missing_facts:
    for fact in missing_facts:
        with st.container(border=True):
            st.markdown(f"**{fact.get('fact_key', 'fact')}**")
            st.write(fact.get("description") or "未提供描述")
            st.caption(f"状态：{fact.get('status', 'OPEN')}")
else:
    st.success("必需事实均已覆盖。", icon=":material/check_circle:")

evidence_by_fact: dict[str, list[dict]] = defaultdict(list)
for item in report.get("evidence") or ():
    evidence_by_fact[str(item.get("fact_key") or "未分类")].append(item)

st.subheader(f"已验证来源 · {sum(map(len, evidence_by_fact.values()))}")
if not evidence_by_fact:
    st.caption("当前运行没有可展示的已验证来源。")
for fact_key, items in evidence_by_fact.items():
    with st.expander(f"{fact_key} · {len(items)} 条", icon=":material/source:"):
        for index, item in enumerate(items, start=1):
            with st.container(border=True):
                st.markdown(f"**证据 {index}** · {item.get('verdict', 'UNKNOWN')}")
                st.write(item.get("quote") or "")
                st.caption(f"置信度 {float(item.get('confidence') or 0):.2f}")
                source_url = item.get("source_url")
                if source_url:
                    st.link_button(
                        "打开来源",
                        source_url,
                        icon=":material/open_in_new:",
                    )
