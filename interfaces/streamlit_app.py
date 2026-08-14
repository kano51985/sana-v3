import streamlit as st
import html, time, logging, io, contextlib
import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sana.agent import SanaAgent
from sana.config import registry, dump_model_config, reset_model_config
from sana.models.credentials import get_user_env, set_user_env
from sana.models.deepseek_backend import DeepSeekBackend
from sana.models.openai_backend import OpenAIModelBackend
from sana.models.registry import ModelConfig
from sana.services.web_tool_config import WebToolConfig
from sana.nodes.pause_parser import strip_pause_tags

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
_USER_AVATAR = os.path.join(_ASSETS_DIR, "user_avatar.jpg")
_SANA_AVATAR = os.path.join(_ASSETS_DIR, "sana_avatar.png")

st.set_page_config(page_title="Sana Control Panel", layout="wide")
st.title("Sana 情感控制台（旧版回滚入口）")
st.warning(
    "此界面仅用于回滚窗口，不具备新平台的多租户隔离。"
    "默认入口已切换到 API 客户端，请勿在生产环境启用。",
    icon="⚠️",
)
st.markdown(
    """
    <style>
    .sana-segment {
        opacity: 0;
        animation: sanaFadeIn 0.35s ease forwards;
        white-space: pre-wrap;
    }
    @keyframes sanaFadeIn {
        from { opacity: 0; transform: translateY(3px); }
        to { opacity: 1; transform: translateY(0); }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if "agent" not in st.session_state:
    st.session_state.agent = SanaAgent()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "console_logs" not in st.session_state:
    st.session_state.console_logs = []

agent = st.session_state.agent

class _LogHandler(logging.Handler):
    def emit(self, record):
        try:
            st.session_state.console_logs.append(self.format(record))
        except Exception:
            pass

if not st.session_state.get("_log_added"):
    h = _LogHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    st.session_state._log_added = True

def norm(v):
    return max(0.0, min(1.0, (v + 1.0) / 2.0))


def _clean_visible_text(text):
    return strip_pause_tags(text or "")


def _segments_for(msg):
    segments = msg.get("segments")
    if segments:
        cleaned = []
        for seg in segments:
            text = _clean_visible_text(seg.get("text"))
            if text:
                cleaned.append({**seg, "text": text})
        return cleaned
    content = _clean_visible_text(msg.get("content", ""))
    return [{"text": content, "delay": 0.0}] if content else []


def _tool_badge(trace, web_enabled=False):
    if not trace:
        return "联网查询未触发" if web_enabled else ""
    if not trace.get("triggered"):
        return "联网查询未触发" if web_enabled else ""
    tool = trace.get("tool", "tool")
    status = trace.get("status", "")
    if tool == "web":
        if status == "blocked":
            return '<span style="background:#b7791f;color:white;padding:2px 8px;border-radius:4px;font-size:12px">联网查询被心情拦截</span>'
        if status == "failed":
            return '<span style="background:#c53030;color:white;padding:2px 8px;border-radius:4px;font-size:12px">联网查询失败</span>'
        return '<span style="background:#2b6cb0;color:white;padding:2px 8px;border-radius:4px;font-size:12px">联网查询工具已触发</span>'
    if tool == "memory":
        return '<span style="background:#6b46c1;color:white;padding:2px 8px;border-radius:4px;font-size:12px">记忆工具已触发</span>'
    return ""


def _render_tool_badge(trace, web_enabled=False):
    badge = _tool_badge(trace, web_enabled)
    if badge:
        st.markdown(f'<div style="margin-top:4px">{badge}</div>', unsafe_allow_html=True)


def _render_history():
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user", avatar=_USER_AVATAR):
                st.markdown(_clean_visible_text(msg["content"]).replace("~~", ""))
            continue
        for seg in _segments_for(msg):
            with st.chat_message("assistant", avatar=_SANA_AVATAR):
                st.markdown(_clean_visible_text(seg["text"]).replace("~~", ""))
        if msg.get("thinking"):
            with st.expander("Sana 内心 OS"):
                st.text(msg["thinking"])
        _render_tool_badge(msg.get("tool_trace"), agent.web_config_store.load().enabled)


def _render_live_assistant(segments, thinking="", tool_trace=None):
    for seg in segments:
        text = _clean_visible_text(seg.get("text", ""))
        if not text:
            continue
        try:
            delay = max(0.0, float(seg.get("delay") or 0.0))
        except (TypeError, ValueError):
            delay = 0.0
        if delay:
            time.sleep(delay)
        placeholder = st.empty()
        with placeholder.container():
            with st.chat_message("assistant", avatar=_SANA_AVATAR):
                st.markdown(
                    f'<div class="sana-segment">{html.escape(text.replace("~~", ""))}</div>',
                    unsafe_allow_html=True,
                )
    if thinking:
        with st.expander("Sana 内心 OS"):
            st.text(thinking)
    _render_tool_badge(tool_trace, agent.web_config_store.load().enabled)


with st.sidebar:
    st.header("白日的控制台")
    st.markdown("---")

    try:
        p = agent.alma.current_mood["P"]
        a = agent.alma.current_mood["A"]
        d = agent.alma.current_mood["D"]
        mood = agent.alma._describe_mood(p, a, d)
        emo = agent.alma.current_transient_emotion
    except Exception:
        p, a, d, mood, emo = 0, 0, 0, "未知", "平静"

    st.subheader("当前心情")
    st.info(f"**{mood}** | 心情: {emo}")

    st.subheader("PAD 观察仓")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("P (Pleasure愉悦度)", f"{p:.2f}")
        st.progress(norm(p))
    with col2:
        st.metric("A (Arousal唤醒度)", f"{a:.2f}")
        st.progress(norm(a))
    with col3:
        st.metric("D (Dominance掌控度)", f"{d:.2f}")
        st.progress(norm(d))

    st.markdown("---")
    st.subheader("注入 PAD")
    new_p = st.slider("P", -1.0, 1.0, float(p), 0.1)
    new_a = st.slider("A", -1.0, 1.0, float(a), 0.1)
    new_d = st.slider("D", -1.0, 1.0, float(d), 0.1)
    if st.button("应用"):
        agent.alma.current_mood["P"] = new_p
        agent.alma.current_mood["A"] = new_a
        agent.alma.current_mood["D"] = new_d
        st.success("已注入"); st.rerun()

    st.markdown("---")
    # ── Web Tool config ──
    with st.expander("联网查询"):
        web_cfg = agent.web_config_store.load()
        enabled = st.toggle("启用联网查询", value=web_cfg.enabled)
        level = st.select_slider(
            "自主等级",
            [0, 1, 2, 3, 4],
            value=web_cfg.autonomy_level,
            format_func=lambda x: f"{x} - {['关闭', '显式', '克制', '主动', '探索'][x]}",
        )
        max_heads = st.slider("每轮最大查询头数", 1, 5, web_cfg.max_query_heads)
        results_per_head = st.slider("每头结果数", 1, 5, web_cfg.results_per_head)
        max_injected = st.slider("最终注入结果数", 1, 10, web_cfg.max_injected_results)
        timeout_seconds = st.slider(
            "单请求超时（秒）",
            1.0,
            10.0,
            float(web_cfg.timeout_seconds),
            0.5,
        )
        provider_timeout_seconds = st.slider(
            "Provider 阶段超时（秒）",
            1.0,
            30.0,
            float(web_cfg.provider_timeout_seconds),
            1.0,
        )
        rerank_timeout_seconds = st.slider(
            "精排阶段超时（秒）",
            1.0,
            30.0,
            float(web_cfg.rerank_timeout_seconds),
            1.0,
        )
        web_total_timeout_seconds = st.slider(
            "Web 搜索总超时（秒）",
            10.0,
            120.0,
            float(web_cfg.web_total_timeout_seconds),
            5.0,
        )
        allow_bing = st.checkbox("允许必应", value=web_cfg.allow_bing)
        allow_baidu = st.checkbox("允许百度", value=web_cfg.allow_baidu)
        allow_direct = st.checkbox("允许官网/百科兜底", value=web_cfg.allow_direct)
        allow_bing_rss = st.checkbox("允许 Bing RSS", value=web_cfg.allow_bing_rss)
        allow_duckduckgo = st.checkbox("允许 DuckDuckGo", value=web_cfg.allow_duckduckgo)
        allow_searxng = st.toggle("允许 SearXNG", value=web_cfg.allow_searxng)
        searxng_url = st.text_input("SearXNG URL", value=web_cfg.searxng_url)
        searxng_timeout_seconds = st.slider(
            "SearXNG 超时（秒）",
            1.0,
            30.0,
            float(web_cfg.searxng_timeout_seconds),
            0.5,
        )
        allow_katana = st.checkbox("允许 Katana 爬虫", value=web_cfg.allow_katana)
        katana_bin = st.text_input("Katana 路径", value=web_cfg.katana_bin)
        katana_total_timeout_seconds = st.slider(
            "Katana 总爬取超时（秒）",
            5.0,
            60.0,
            float(web_cfg.katana_total_timeout_seconds),
            1.0,
        )
        if st.button("保存联网配置", use_container_width=True):
            agent.web_config_store.save(WebToolConfig(
                enabled=enabled,
                autonomy_level=level,
                max_query_heads=max_heads,
                results_per_head=results_per_head,
                max_injected_results=max_injected,
                timeout_seconds=timeout_seconds,
                provider_timeout_seconds=provider_timeout_seconds,
                rerank_timeout_seconds=rerank_timeout_seconds,
                web_total_timeout_seconds=web_total_timeout_seconds,
                allow_bing=allow_bing,
                allow_baidu=allow_baidu,
                allow_direct=allow_direct,
                allow_bing_rss=allow_bing_rss,
                allow_duckduckgo=allow_duckduckgo,
                allow_searxng=allow_searxng,
                searxng_url=searxng_url,
                searxng_timeout_seconds=searxng_timeout_seconds,
                allow_katana=allow_katana,
                katana_bin=katana_bin,
                katana_total_timeout_seconds=katana_total_timeout_seconds,
            ))
            st.success("已保存")
        st.markdown("**最近工具轨迹**")
        st.json(agent.last_web_trace)

    st.markdown("---")
    # ── Model config ──
    with st.expander("模型配置"):
        _layers = ["perception", "chat", "summarize"]
        _labels = {
            "perception": "感知层 Perception",
            "chat": "对话层 Chat",
            "summarize": "总结层 Summarize",
        }
        _backends = ["local", "deepseek", "openai"]

        st.session_state.setdefault("model_api_key", "")
        st.text_input("DeepSeek/OpenAI API Key", type="password", key="model_api_key")
        _deepseek_key_ok = bool(get_user_env("DEEPSEEK_API_KEY"))
        _openai_key_ok = bool(get_user_env("OPENAI_API_KEY"))
        st.caption(
            f"DeepSeek Key: {'已检测到' if _deepseek_key_ok else '未检测到'} | "
            f"OpenAI Key: {'已检测到' if _openai_key_ok else '未检测到'}"
        )

        for role in _layers:
            st.markdown(f"**{_labels[role]}**")
            cur = registry.models[role]
            try:
                be_idx = _backends.index(cur.backend_name)
            except ValueError:
                be_idx = 0
            col1, col2 = st.columns(2)
            with col1:
                st.selectbox("后端", _backends, index=be_idx, key=f"be_{role}")
            with col2:
                be = st.session_state[f"be_{role}"]
                _prev_be_key = f"_prev_be_{role}"
                _prev_be = st.session_state.get(_prev_be_key)
                _be_changed = _prev_be is not None and _prev_be != be

                if be == "deepseek":
                    if _be_changed:
                        st.session_state.pop(f"mid_{role}", None)
                    _ds_models = ["deepseek-v4-flash", "deepseek-v4-pro"]
                    _ds_labels = ["FLASH", "PRO"]
                    _ds_map = dict(zip(_ds_labels, _ds_models))
                    _cur_mid = st.session_state.get(f"mid_{role}", cur.model_id)
                    # Map full model name to display label
                    _cur_label = next((l for l, m in _ds_map.items() if m == _cur_mid), _ds_labels[0])
                    try:
                        _idx = _ds_labels.index(_cur_label)
                    except ValueError:
                        _idx = 0
                    st.selectbox("模型 ID", _ds_labels, index=_idx, key=f"mid_{role}")
                else:
                    if _be_changed:
                        st.session_state.pop(f"mid_{role}", None)
                    st.text_input("模型 ID", value=cur.model_id, key=f"mid_{role}")

                st.session_state[_prev_be_key] = be
            st.slider(
                "温度", 0.0, 2.0, cur.params.get("temperature", 0.7),
                step=0.05, key=f"temp_{role}"
            )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("应用配置", use_container_width=True):
                _api_key = st.session_state.get("model_api_key", "").strip()
                _be_set = {st.session_state[f"be_{role}"] for role in _layers}
                if _api_key and "deepseek" in _be_set:
                    set_user_env("DEEPSEEK_API_KEY", _api_key)
                    registry.backends["deepseek"] = DeepSeekBackend(api_key=_api_key)
                if _api_key and "openai" in _be_set:
                    set_user_env("OPENAI_API_KEY", _api_key)
                    registry.backends["openai"] = OpenAIModelBackend(api_key=_api_key)
                _profile = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "user_profile.json",
                )
                for role in _layers:
                    _mid = st.session_state[f"mid_{role}"]
                    _be = st.session_state[f"be_{role}"]
                    if _be == "deepseek":
                        _mid = {"PRO": "deepseek-v4-pro", "FLASH": "deepseek-v4-flash"}.get(_mid, _mid)
                    registry.models[role] = ModelConfig(
                        model_id=_mid,
                        backend_name=_be,
                        params={"temperature": st.session_state[f"temp_{role}"]},
                    )
                try:
                    with open(_profile, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["model_config"] = dump_model_config()
                    with open(_profile, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    st.success("已应用并保存")
                except Exception as e:
                    st.error(f"保存失败: {e}")
        with col2:
            if st.button("重置为默认", use_container_width=True):
                reset_model_config()
                _profile = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "user_profile.json",
                )
                for role in _layers:
                    for k in (f"be_{role}", f"mid_{role}", f"temp_{role}"):
                        st.session_state.pop(k, None)
                try:
                    with open(_profile, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["model_config"] = dump_model_config()
                    with open(_profile, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
                st.rerun()

    st.markdown("---")
    with st.expander("日志"):
        logs = st.session_state.console_logs[-100:]
        st.text_area("log", value="\n".join(_clean_visible_text(line) for line in logs), height=300)
    if st.button("清空日志"):
        st.session_state.console_logs = []

_render_history()

with st.container(horizontal=True):
    if st.button("手动聚合记忆", icon=":material/database:"):
        with st.spinner("正在聚合当前对话..."):
            result = agent.consolidate_memory()
        if result.get("code") == "empty":
            st.info(result.get("message", "当前没有待聚合的对话"))
        elif result.get("ok"):
            if result.get("event_count") or result.get("update_count"):
                st.success(f"{result.get('message', '聚合完成')}：归档 {result.get('event_count', 0)} 条记忆、{result.get('update_count', 0)} 条档案更新")
            else:
                st.success(f"{result.get('message', '聚合完成')}，本轮没有提取到新记忆")
        else:
            st.error(f"{result.get('message', '聚合失败')}，缓存未清空，可重试")

if inp := st.chat_input("有什么想和sana分享的？"):
    st.session_state.messages.append({"role": "user", "content": inp})
    with st.chat_message("user", avatar=_USER_AVATAR):
        st.markdown(inp)
    with st.spinner("Thinking..."):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reply = agent.chat(inp)
        log_text = _clean_visible_text(buf.getvalue())
        if log_text:
            st.session_state.console_logs.append(log_text)
    _chat = _clean_visible_text(reply.get("chat", ""))
    segments = _segments_for({"segments": reply.get("segments"), "content": _chat})
    if not segments:
        segments = [{"text": _chat or "[Empty response]", "delay": 0.0}]
    st.session_state.messages.append({
        "role": "assistant",
        "content": _chat,
        "segments": segments,
        "thinking": reply.get("thinking", ""),
        "tool_trace": reply.get("tool_trace", {}),
    })
    _render_live_assistant(segments, reply.get("thinking", ""), reply.get("tool_trace", {}))
    st.rerun()
