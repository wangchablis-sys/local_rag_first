import streamlit as st

import rag


st.set_page_config(
    page_title="武汉大学学生事务助手",
    page_icon="WHU",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    @import url("https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,650&family=Source+Sans+3:wght@400;600;700&display=swap");

    :root {
        --ink: #1c2834;
        --muted: #5d6b7a;
        --line: #e4ddd2;
        --paper: #f6f3ee;
        --card: #fffcf8;
        --green: #0f6b4c;
        --green-deep: #0b4f38;
        --gold: #a6843d;
    }

    html, body, .stApp, [data-testid="stAppViewContainer"],
    [data-testid="stMain"], [data-testid="stSidebar"] {
        font-family: "Source Sans 3", "Segoe UI", sans-serif;
        color: var(--ink) !important;
    }

    .stApp {
        background:
            radial-gradient(circle at top right, rgba(166, 132, 61, 0.16), transparent 28%),
            linear-gradient(180deg, #fbf8f3 0%, var(--paper) 42%, #eef3f0 100%) !important;
    }

    header[data-testid="stHeader"] {
        background: transparent !important;
    }

    header[data-testid="stHeader"] [data-testid="stIconMaterial"],
    header[data-testid="stHeader"] button {
        color: var(--ink) !important;
    }

    .stMarkdown, .stMarkdown p, .stMarkdown li,
    [data-testid="stCaptionContainer"],
    [data-testid="stChatMessage"] {
        color: var(--ink) !important;
    }

    .block-container {
        max-width: 860px;
        padding-top: 2.2rem;
        padding-bottom: 6rem;
    }

    .hero {
        display: flex;
        gap: 1rem;
        align-items: flex-end;
        margin-bottom: 1.4rem;
        padding-bottom: 1.1rem;
        border-bottom: 1px solid var(--line);
    }

    .seal {
        flex: 0 0 auto;
        width: 3.4rem;
        height: 3.4rem;
        border-radius: 0.9rem;
        background: var(--green-deep);
        color: #f8f3e8;
        display: grid;
        place-items: center;
        font-family: "Source Serif 4", serif;
        font-weight: 650;
        letter-spacing: 0.04em;
    }

    .hero-title {
        margin: 0;
        font-family: "Source Serif 4", serif;
        font-size: 2rem;
        font-weight: 650;
        letter-spacing: -0.03em;
        line-height: 1.15;
        color: var(--ink) !important;
    }

    .hero p {
        margin: 0.35rem 0 0;
        color: var(--muted);
        font-size: 0.98rem;
    }

    div[data-testid="stChatMessage"] {
        background: var(--card);
        border: 1px solid var(--line);
        border-radius: 1rem;
        padding: 0.35rem 0.7rem;
        box-shadow: 0 10px 30px rgba(28, 40, 52, 0.04);
    }

    .cite-wrap {
        margin-top: 0.8rem;
        display: grid;
        gap: 0.55rem;
    }

    .cite {
        display: flex;
        gap: 0.75rem;
        align-items: flex-start;
        padding: 0.75rem 0.85rem;
        border-radius: 0.8rem;
        background: #f7f4ee;
        border: 1px solid var(--line);
    }

    .cite-index {
        flex: 0 0 auto;
        width: 1.5rem;
        height: 1.5rem;
        border-radius: 999px;
        background: var(--green);
        color: white;
        display: grid;
        place-items: center;
        font-size: 0.78rem;
        font-weight: 700;
    }

    .cite strong {
        display: block;
        font-size: 0.95rem;
    }

    .cite .meta, .cite .path {
        color: var(--muted);
        font-size: 0.82rem;
        line-height: 1.45;
    }

    .path {
        word-break: break-all;
    }

    [data-testid="stBottom"] > div,
    [data-testid="stBottomBlockContainer"] {
        background: rgba(246, 243, 238, 0.96) !important;
    }

    div[data-testid="stChatInput"] {
        background: transparent !important;
    }

    div[data-testid="stChatInput"] > div {
        background: var(--card) !important;
        border: 1px solid #d9d0c3 !important;
        border-radius: 1rem !important;
        box-shadow: 0 8px 24px rgba(28, 40, 52, 0.06);
    }

    [data-testid="stChatInputTextArea"] {
        color: var(--ink) !important;
        caret-color: var(--green-deep);
    }

    [data-testid="stChatInputTextArea"]::placeholder {
        color: var(--muted) !important;
    }

    [data-testid="stChatInputSubmitButton"] {
        background: var(--green) !important;
        color: #f8f3e8 !important;
        border-radius: 999px !important;
    }

    [data-testid="stChatInputSubmitButton"] [data-testid="stIconMaterial"] {
        color: #f8f3e8 !important;
    }

    .stButton > button {
        border-radius: 999px;
        border: 1px solid #d9d0c3;
        background: white;
        color: var(--green-deep);
        font-weight: 600;
    }

    .stButton > button:hover {
        border-color: var(--green);
        color: var(--green);
    }

    section[data-testid="stSidebar"] {
        background: #fbf8f3;
        border-right: 1px solid var(--line);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

EXAMPLES = [
    "国家助学金如何申请？",
    "转学院需要满足什么条件？",
    "家庭经济困难学生如何认定？",
]


def render_citations(citations: list[dict]) -> None:
    if not citations:
        return

    cards = []
    for item in citations:
        cards.append(
            "<div class='cite'>"
            f"<span class='cite-index'>{item['index']}</span>"
            "<div>"
            f"<strong>{item['filename']}</strong>"
            f"<div class='meta'>第 {item['physical_page']} 页</div>"
            f"<div class='path'>{item['source']}</div>"
            "</div></div>"
        )
    st.markdown(
        "<div class='cite-wrap'>" + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        render_citations(message.get("citations") or [])


def queue_question(question: str) -> None:
    st.session_state.messages.append({"role": "user", "content": question})
    st.session_state.awaiting_answer = True


def complete_answer() -> None:
    question = st.session_state.messages[-1]["content"]
    try:
        with st.spinner("正在查阅已入库的政策文件"):
            result = rag.answer(question)
        answer = result["answer"] or "模型没有返回正文。"
        citations = result["citations"]
    except Exception as exc:
        answer = f"这次没有完成回答：{exc}"
        citations = []

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "citations": citations,
        }
    )
    st.session_state.awaiting_answer = False


if "messages" not in st.session_state:
    st.session_state.messages = []
if "awaiting_answer" not in st.session_state:
    st.session_state.awaiting_answer = False

with st.sidebar:
    st.markdown("**学生事务政策助手**")
    st.caption("回答依据已入库的学校公开文件，并标出文件名和页码。")
    if st.button("清空对话", use_container_width=True):
        st.session_state.messages = []
        st.session_state.awaiting_answer = False
        st.rerun()

st.markdown(
    """
    <div class="hero">
        <div class="seal">WHU</div>
        <div>
            <div class="hero-title">学生事务政策助手</div>
            <p>写下年级、专业和要办的事。按回车发送。</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

for message in st.session_state.messages:
    render_message(message)

if st.session_state.awaiting_answer:
    complete_answer()
    st.rerun()

if not st.session_state.messages:
    columns = st.columns(len(EXAMPLES))
    for column, example in zip(columns, EXAMPLES):
        with column:
            if st.button(example, use_container_width=True):
                queue_question(example)
                st.rerun()

question = st.chat_input("例如：我是 2023 级本科生，国家助学金如何申请？")
if question and question.strip():
    queue_question(question.strip())
    st.rerun()
