import streamlit as st

import rag

st.set_page_config(page_title="武汉大学本地知识库", layout="wide")
st.title("武汉大学本地知识库")
question = st.text_area("问题", height=120, placeholder="例如：国家助学金如何申请？")

if st.button("提问", type="primary") and question.strip():
    with st.spinner("正在检索并生成回答"):
        result = rag.answer(question.strip())
    st.caption(f"使用集合：{result['collection']}")
    st.markdown(result["answer"] or "模型没有返回正文。")
    st.subheader("检索依据")
    for item in result["citations"]:
        st.markdown(
            f"{item['index']}. `{item['filename']}` 第 {item['physical_page']} 页"
            f"（距离 {item['distance']:.4f}）"
        )
        st.caption(item["source"])
