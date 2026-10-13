from __future__ import annotations

import requests
import chromadb

import config


def session() -> requests.Session:
    connection = requests.Session()
    connection.trust_env = False
    return connection


def collection():
    client = chromadb.PersistentClient(path=str(config.CHROMA_PERSIST_DIR))
    formal = config.CHROMA_COLLECTION_NAME
    names = [item.name for item in client.list_collections()]
    if formal in names and client.get_collection(formal).count() > 0:
        return client.get_collection(formal), formal
    pointer = config.CHROMA_PERSIST_DIR / "candidate_collection.txt"
    if not pointer.exists():
        raise RuntimeError("还没有候选集合，请先运行 python ingest.py")
    name = pointer.read_text(encoding="utf-8").strip()
    return client.get_collection(name), name


def embed(connection: requests.Session, text: str) -> list[float]:
    response = connection.post(
        f"{config.OLLAMA_BASE_URL}/api/embed",
        json={"model": config.EMBEDDING_MODEL, "input": [text], "truncate": True},
        timeout=300,
    )
    response.raise_for_status()
    vectors = response.json().get("embeddings") or []
    if len(vectors) != 1:
        raise RuntimeError("问题向量生成失败")
    return vectors[0]


def answer(question: str) -> dict:
    connection = session()
    store, store_name = collection()
    found = store.query(
        query_embeddings=[embed(connection, question)],
        n_results=config.RETRIEVAL_TOP_K,
        include=["documents", "metadatas", "distances"],
    )

    documents = found["documents"][0]
    metadatas = found["metadatas"][0]
    distances = found["distances"][0]
    blocks = []
    citations = []

    for index, (document, metadata, distance) in enumerate(
        zip(documents, metadatas, distances),
        start=1,
    ):
        filename = metadata["original_filename"]
        page = metadata["physical_page"]
        source = metadata["source"]
        blocks.append(
            f"[{index}] 文件：{filename}；物理页：{page}；路径：{source}\n{document}"
        )
        citations.append(
            {
                "index": index,
                "filename": filename,
                "physical_page": page,
                "source": source,
                "distance": distance,
            }
        )

    context = "\n\n".join(blocks) if blocks else "没有检索到资料。"
    response = connection.post(
        f"{config.OLLAMA_BASE_URL}/api/chat",
        json={
            "model": config.CHAT_MODEL,
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是武汉大学资料问答助手。"
                        "只根据给定资料回答。"
                        "每条结论后面标注【文件名 第N页】，N 使用物理页。"
                        "资料不足以回答时，直接说明知识库中没有足够依据。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"问题：{question}\n\n资料：\n{context}",
                },
            ],
        },
        timeout=600,
    )
    response.raise_for_status()
    message = response.json().get("message", {})
    return {
        "collection": store_name,
        "answer": message.get("content", "").strip(),
        "citations": citations,
    }
