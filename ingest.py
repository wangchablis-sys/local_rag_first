from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import chromadb
import fitz
import requests

import config


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".html",
    ".htm",
}
OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx"}
HTML_EXTENSIONS = {".html", ".htm"}
HTML_MIN_TEXT_CHARS = 80

# 抽取规则变化时改这个值，增量入库才会放弃旧向量并整库重建
EXTRACTOR_VERSION = "html-v1-page-chunks"

EMBED_BATCH_SIZE = 16
CHROMA_BATCH_SIZE = 100
STATE_PATH = config.CHROMA_PERSIST_DIR / "incremental_state.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("\u00a0", " ")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _HtmlNode:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]):
        self.tag = tag
        self.attrs = {
            key: value or ""
            for key, value in attrs
        }
        self.children: list[_HtmlNode | str] = []


class _HtmlTreeBuilder(HTMLParser):
    """把网页解析成简单树，便于只取正文区域。"""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document", [])
        self.stack = [self.root]
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "title":
            self._in_title = True
            return

        node = _HtmlNode(tag, attrs)
        self.stack[-1].children.append(node)

        if tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "title":
            self._in_title = False
            return

        if tag in self._VOID_TAGS:
            return

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return

        if data:
            self.stack[-1].children.append(data)

    def title(self) -> str:
        return clean_text("".join(self.title_parts))


_HTML_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
}

_HTML_BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "br",
    "section",
    "article",
    "table",
    "blockquote",
}


def _render_html_node(node: _HtmlNode | str) -> str:
    if isinstance(node, str):
        return node

    if node.tag in _HTML_SKIP_TAGS:
        return ""

    rendered = "".join(
        _render_html_node(child)
        for child in node.children
    )

    if node.tag in _HTML_BLOCK_TAGS:
        return f"\n{rendered}\n"

    return rendered


def _iter_html_nodes(node: _HtmlNode | str):
    if isinstance(node, str):
        return

    yield node

    for child in node.children:
        yield from _iter_html_nodes(child)


def _class_tokens(node: _HtmlNode) -> set[str]:
    return set(node.attrs.get("class", "").lower().split())


def _longest_html_region(root: _HtmlNode, predicate) -> str:
    best = ""

    for node in _iter_html_nodes(root):
        if not predicate(node):
            continue

        text = clean_text(_render_html_node(node))

        if len(text) > len(best):
            best = text

    return best


def _html_title(raw_title: str) -> str:
    title = re.split(
        r"\s*(?:--|[—–]|-\s*武汉大学)",
        raw_title,
        maxsplit=1,
    )[0]
    return clean_text(title)


def _html_attachment_labels(raw_html: str) -> list[str]:
    labels: list[str] = []
    pattern = re.compile(
        r"<a\b[^>]*>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(raw_html):
        label = clean_text(
            re.sub(r"<[^>]+>", "", match.group(1))
        )

        if re.search(
            r"\.(pdf|doc|docx|xls|xlsx|zip|rar)$",
            label,
            flags=re.IGNORECASE,
        ):
            labels.append(label)

    unique: list[str] = []
    seen: set[str] = set()

    for label in labels:
        if label not in seen:
            seen.add(label)
            unique.append(label)

    return unique


def _select_html_article(root: _HtmlNode) -> str:
    regions = [
        _longest_html_region(
            root,
            lambda node: node.attrs.get("id", "").lower().startswith(
                "vsb_content"
            ),
        ),
        _longest_html_region(
            root,
            lambda node: "v_news_content" in _class_tokens(node),
        ),
        _longest_html_region(
            root,
            lambda node: "col-main" in _class_tokens(node),
        ),
        _longest_html_region(
            root,
            lambda node: "article" in _class_tokens(node),
        ),
        _longest_html_region(
            root,
            lambda node: "content" in _class_tokens(node),
        ),
    ]
    regions = [region for region in regions if region]
    long_regions = [
        region
        for region in regions
        if len(region) >= HTML_MIN_TEXT_CHARS
    ]

    if long_regions:
        return max(long_regions, key=len)

    if regions:
        return max(regions, key=len)

    return clean_text(_render_html_node(root))


def extract_html_document(source: Path) -> dict:
    """抽取已保存网页的正文。网页没有 PDF 物理页，整页记为第 1 页。"""

    raw_html = source.read_text(
        encoding="utf-8",
        errors="replace",
    )
    parser = _HtmlTreeBuilder()
    parser.feed(raw_html)

    title = _html_title(parser.title())
    article = _select_html_article(parser.root)
    missing_markers = (
        "Page Not Found",
        "页面不存在",
    )

    if any(
        marker in parser.title() or marker in article
        for marker in missing_markers
    ):
        return {
            "pages": [],
            "text": article,
            "title": title,
            "skip_reason": "html_page_not_found",
        }

    for label in _html_attachment_labels(raw_html):
        if label not in article:
            article = f"{article}\n附件：{label}".strip()

    article = clean_text(article)

    if len(article) < HTML_MIN_TEXT_CHARS:
        return {
            "pages": [
                {
                    "physical_page": 1,
                    "display_page": None,
                    "text": article,
                }
            ],
            "text": article,
            "title": title,
            "skip_reason": (
                "html_no_extractable_text_or_image_only"
            ),
        }

    if title and title not in article[: len(title) + 40]:
        article = clean_text(f"{title}\n\n{article}")

    return {
        "pages": [
            {
                "physical_page": 1,
                "display_page": None,
                "text": article,
            }
        ],
        "text": article,
        "title": title,
        "skip_reason": None,
    }


def find_libreoffice() -> str:
    binary = shutil.which("libreoffice") or shutil.which("soffice")

    if not binary:
        raise RuntimeError(
            "未找到 LibreOffice，请执行：\n"
            "sudo apt update && "
            "sudo apt install -y libreoffice-writer libreoffice-calc"
        )

    return binary


def convert_office_to_pdf(source: Path, source_id: str) -> Path:
    """在工作目录生成 PDF，不修改 source 原文件。"""

    libreoffice = find_libreoffice()

    cache_dir = (
        config.NORMALIZED_DIR
        / "_rendered_pdf"
        / source_id
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    cached_pdf = cache_dir / f"{source.stem}.pdf"

    with tempfile.TemporaryDirectory(prefix="whu-office-") as temp_name:
        temp_dir = Path(temp_name)
        output_dir = temp_dir / "output"
        profile_dir = temp_dir / "profile"

        output_dir.mkdir()
        profile_dir.mkdir()

        command = [
            libreoffice,
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source),
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

        generated = sorted(output_dir.glob("*.pdf"))

        if result.returncode != 0 or len(generated) != 1:
            details = (result.stdout + "\n" + result.stderr).strip()
            raise RuntimeError(
                f"LibreOffice 转换失败：{source}\n{details}"
            )

        shutil.copy2(generated[0], cached_pdf)

    return cached_pdf


def extract_pdf_pages(pdf_path: Path) -> list[dict]:
    """按 PDF 物理页提取文字。"""

    pages: list[dict] = []

    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document):
            text = clean_text(
                page.get_text("text", sort=True)
            )

            try:
                display_page = page.get_label().strip() or None
            except Exception:
                display_page = None

            pages.append(
                {
                    "physical_page": index + 1,
                    "display_page": display_page,
                    "text": text,
                }
            )

    return pages


def split_page_text(text: str) -> list[str]:
    """只在当前页面内部切片，绝不跨页。"""

    size = config.CHUNK_SIZE
    overlap = config.CHUNK_OVERLAP

    if size <= 0:
        raise ValueError("CHUNK_SIZE 必须大于 0")

    if overlap < 0 or overlap >= size:
        raise ValueError(
            "CHUNK_OVERLAP 必须大于等于 0 且小于 CHUNK_SIZE"
        )

    chunks: list[str] = []
    start = 0
    text_length = len(text)

    delimiters = (
        "\n\n",
        "\n",
        "。",
        "！",
        "？",
        "；",
    )

    while start < text_length:
        proposed_end = min(start + size, text_length)
        end = proposed_end

        if proposed_end < text_length:
            search_start = start + size // 2
            best_position = -1
            best_width = 0

            for delimiter in delimiters:
                position = text.rfind(
                    delimiter,
                    search_start,
                    proposed_end,
                )

                if position > best_position:
                    best_position = position
                    best_width = len(delimiter)

            if best_position >= search_start:
                end = best_position + best_width

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        next_start = max(0, end - overlap)

        if next_start <= start:
            next_start = end

        start = next_start

    return chunks


def write_normalized_markdown(
    source_id: str,
    original_filename: str,
    pages: list[dict],
) -> Path:
    """保存带原始页码标记的规范化文本。"""

    destination = (
        config.NORMALIZED_DIR
        / f"{source_id}.md"
    )

    blocks: list[str] = []

    for page in pages:
        marker = {
            "original_filename": original_filename,
            "physical_page": page["physical_page"],
            "display_page": page["display_page"],
        }

        blocks.append(
            "<!-- university-rag-page "
            + json.dumps(marker, ensure_ascii=False)
            + " -->\n\n"
            + page["text"]
            + "\n"
        )

    destination.write_text(
        "\n".join(blocks),
        encoding="utf-8",
    )

    return destination


def make_source_chunks(
    source: Path,
    relative_source: str,
    fingerprint: str,
    source_id: str,
    pages: list[dict],
    converted_from_office: bool,
    source_kind: str,
) -> list[dict]:
    records: list[dict] = []

    for page in pages:
        page_chunks = split_page_text(page["text"])

        for page_chunk_index, text in enumerate(
            page_chunks,
            start=1,
        ):
            raw_id = (
                f"{source_id}|"
                f"{page['physical_page']}|"
                f"{page_chunk_index}|"
                f"{text}"
            )

            chunk_id = hashlib.sha256(
                raw_id.encode("utf-8")
            ).hexdigest()

            records.append(
                {
                    "chunk_id": chunk_id,
                    "text": text,
                    "source_id": source_id,
                    "source": relative_source,
                    "original_filename": source.name,
                    "physical_page": page["physical_page"],
                    "display_page": page["display_page"],
                    "page_chunk_index": page_chunk_index,
                    "coordinates": {
                        "physical_page": page["physical_page"],
                        "display_page": page["display_page"],
                    },
                    "fingerprint": fingerprint,
                    "file_type": source.suffix.lower(),
                    "source_kind": source_kind,
                    "converted_from_office": (
                        converted_from_office
                    ),
                    "review_status": "pending_review",
                }
            )

    return records


def create_ollama_session() -> requests.Session:
    """本地 Ollama 请求不经过 Windows 代理。"""

    session = requests.Session()
    session.trust_env = False
    return session


def check_ollama(session: requests.Session) -> None:
    try:
        response = session.get(
            f"{config.OLLAMA_BASE_URL}/api/version",
            timeout=10,
        )
        response.raise_for_status()

        print(
            "Ollama 已连接：",
            response.json().get("version", "unknown"),
        )

    except requests.RequestException as exc:
        raise RuntimeError(
            "无法连接 Ollama。\n"
            "请先在另一个终端运行：\n"
            "/usr/bin/ollama serve"
        ) from exc


def embed_texts(
    session: requests.Session,
    texts: list[str],
) -> list[list[float]]:
    vectors: list[list[float]] = []
    total = len(texts)

    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[
            start:start + EMBED_BATCH_SIZE
        ]

        response = session.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={
                "model": config.EMBEDDING_MODEL,
                "input": batch,
                "truncate": True,
            },
            timeout=600,
        )

        response.raise_for_status()

        embeddings = response.json().get("embeddings")

        if not embeddings:
            raise RuntimeError(
                "Ollama 没有返回 embeddings"
            )

        if len(embeddings) != len(batch):
            raise RuntimeError(
                "Ollama 返回的向量数量与输入数量不一致"
            )

        vectors.extend(embeddings)

        completed = min(
            start + len(batch),
            total,
        )

        print(
            f"向量生成进度：{completed}/{total}"
        )

    dimensions = {
        len(vector)
        for vector in vectors
    }

    if len(dimensions) != 1:
        raise RuntimeError(
            f"向量维度不一致：{sorted(dimensions)}"
        )

    return vectors


def make_chroma_metadata(chunk: dict) -> dict:
    """Chroma 元数据只能使用简单值，不能存嵌套字典。"""

    return {
        "source_id": chunk["source_id"],
        "source": chunk["source"],
        "original_filename": chunk["original_filename"],
        "physical_page": chunk["physical_page"],
        "display_page": chunk["display_page"] or "",
        "page_chunk_index": chunk["page_chunk_index"],
        "fingerprint": chunk["fingerprint"],
        "file_type": chunk["file_type"],
        "source_kind": chunk["source_kind"],
        "converted_from_office": (
            chunk["converted_from_office"]
        ),
        "review_status": chunk["review_status"],
    }


def chroma_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=str(config.CHROMA_PERSIST_DIR)
    )


def collection_names(client: chromadb.PersistentClient) -> set[str]:
    names: set[str] = set()

    for item in client.list_collections():
        name = getattr(item, "name", None)

        if name is None and isinstance(item, str):
            name = item

        if name:
            names.add(name)

    return names


def vector_to_list(vector) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()

    return [float(value) for value in vector]


def open_baseline(
    client: chromadb.PersistentClient,
) -> tuple[chromadb.Collection | None, str | None]:
    """优先沿用最近一次候选集合，否则使用已有正式集合。"""

    names = collection_names(client)
    pointer = (
        config.CHROMA_PERSIST_DIR
        / "candidate_collection.txt"
    )

    candidates: list[str] = []

    if pointer.exists():
        candidates.append(
            pointer.read_text(encoding="utf-8").strip()
        )

    candidates.append(config.CHROMA_COLLECTION_NAME)

    for name in candidates:
        if name not in names:
            continue

        collection = client.get_collection(name)

        if collection.count() > 0:
            return collection, name

    return None, None


def incompatible_index_settings(metadata: dict | None) -> str | None:
    if not metadata:
        return "上一份集合没有元数据"

    expected = {
        "embedding_model": config.EMBEDDING_MODEL,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "extractor_version": EXTRACTOR_VERSION,
    }

    for key, value in expected.items():
        actual = metadata.get(key)

        if actual is None:
            return f"上一份集合缺少 {key}，需要整库重建"

        if str(actual) != str(value):
            return (
                f"{key} 由 {actual} 变为 {value}，需要整库重建"
            )

    return None


def load_collection_records(collection) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    offset = 0
    page_size = 200

    while True:
        batch = collection.get(
            include=["documents", "metadatas", "embeddings"],
            limit=page_size,
            offset=offset,
        )
        ids = batch.get("ids") or []

        if not ids:
            break

        fresh_ids = [
            chunk_id
            for chunk_id in ids
            if chunk_id not in seen
        ]

        if not fresh_ids:
            break

        embeddings = batch.get("embeddings")

        if embeddings is None:
            raise RuntimeError(
                "上一份集合没有返回向量，无法增量复制"
            )

        if hasattr(embeddings, "tolist"):
            embeddings = embeddings.tolist()

        documents = batch.get("documents") or []
        metadatas = batch.get("metadatas") or []

        for index, chunk_id in enumerate(ids):
            if chunk_id in seen:
                continue

            seen.add(chunk_id)
            metadata = metadatas[index] or {}

            records.append(
                {
                    "chunk_id": chunk_id,
                    "text": documents[index],
                    "embedding": vector_to_list(embeddings[index]),
                    "metadata": metadata,
                    "source": metadata.get("source", ""),
                    "fingerprint": metadata.get("fingerprint", ""),
                }
            )

        offset += len(ids)

        if len(ids) < page_size:
            break

    return records


def chunk_from_stored_record(record: dict) -> dict:
    metadata = record["metadata"]
    display_page = metadata.get("display_page") or None

    if display_page == "":
        display_page = None

    physical_page = metadata.get("physical_page", 1)

    return {
        "chunk_id": record["chunk_id"],
        "text": record["text"],
        "source_id": metadata.get("source_id", ""),
        "source": metadata.get("source", ""),
        "original_filename": metadata.get(
            "original_filename",
            "",
        ),
        "physical_page": physical_page,
        "display_page": display_page,
        "page_chunk_index": metadata.get(
            "page_chunk_index",
            1,
        ),
        "coordinates": {
            "physical_page": physical_page,
            "display_page": display_page,
        },
        "fingerprint": metadata.get("fingerprint", ""),
        "file_type": metadata.get("file_type", ""),
        "source_kind": metadata.get("source_kind", "pdf"),
        "converted_from_office": bool(
            metadata.get("converted_from_office", False)
        ),
        "review_status": metadata.get(
            "review_status",
            "pending_review",
        ),
    }


def same_source_fingerprint(records: list[dict], fingerprint: str) -> bool:
    fingerprints = {
        record.get("fingerprint", "")
        for record in records
    }
    return fingerprints == {fingerprint}


def load_incremental_state() -> dict | None:
    if not STATE_PATH.exists():
        return None

    return json.loads(
        STATE_PATH.read_text(encoding="utf-8")
    )


def write_incremental_state(
    sources: dict,
    collection_name: str | None,
    vector_dimension: int | None,
) -> None:
    payload = {
        "extractor_version": EXTRACTOR_VERSION,
        "embedding_model": config.EMBEDDING_MODEL,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "vector_dimension": vector_dimension,
        "collection": collection_name,
        "sources": sources,
    }
    STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ingest_one_source(
    source: Path,
    report: dict,
    fingerprint: str | None = None,
) -> tuple[list[dict], str]:
    """抽取单个文件。返回文本块和状态：indexed、pending、failed、skipped。"""

    relative_source = source.relative_to(
        config.SOURCE_DIR
    ).as_posix()
    extension = source.suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        report["skipped"].append(
            {
                "source": relative_source,
                "reason": "unsupported_extension",
            }
        )
        return [], "skipped"

    print(f"\n处理：{relative_source}")

    try:
        if fingerprint is None:
            fingerprint = sha256_file(source)

        source_id = hashlib.sha256(
            relative_source.encode("utf-8")
        ).hexdigest()[:24]

        converted_from_office = extension in OFFICE_EXTENSIONS
        source_kind = (
            "html"
            if extension in HTML_EXTENSIONS
            else "pdf"
        )

        if extension in HTML_EXTENSIONS:
            html_result = extract_html_document(source)

            if html_result["skip_reason"]:
                if (
                    html_result["skip_reason"]
                    != "html_page_not_found"
                    and html_result["text"]
                ):
                    write_normalized_markdown(
                        source_id,
                        source.name,
                        html_result["pages"],
                    )

                report["pending_review"].append(
                    {
                        "source": relative_source,
                        "reason": html_result["skip_reason"],
                        "preview": html_result["text"][:300],
                        "blank_pages": [],
                    }
                )
                print(
                    "待复核："
                    f"{relative_source}: "
                    f"{html_result['skip_reason']}"
                )
                return [], "pending"

            pages = html_result["pages"]

        else:
            if converted_from_office:
                pdf_path = convert_office_to_pdf(
                    source,
                    source_id,
                )
            else:
                pdf_path = source

            pages = extract_pdf_pages(pdf_path)

        if not pages:
            raise RuntimeError("没有检测到任何页面")

        normalized_path = write_normalized_markdown(
            source_id,
            source.name,
            pages,
        )
        chunks = make_source_chunks(
            source=source,
            relative_source=relative_source,
            fingerprint=fingerprint,
            source_id=source_id,
            pages=pages,
            converted_from_office=converted_from_office,
            source_kind=source_kind,
        )
        blank_pages = [
            page["physical_page"]
            for page in pages
            if not page["text"]
        ]

        if not chunks:
            report["pending_review"].append(
                {
                    "source": relative_source,
                    "reason": "no_extractable_text_or_ocr_required",
                    "blank_pages": blank_pages,
                }
            )
            return [], "pending"

        report["success"].append(
            {
                "source": relative_source,
                "pages": len(pages),
                "chunks": len(chunks),
                "normalized": str(normalized_path),
            }
        )

        if source_kind == "html":
            review_reason = "html_has_no_pdf_pages_requires_review"
        elif converted_from_office:
            review_reason = (
                "office_rendered_page_layout_requires_review"
            )
        else:
            review_reason = (
                "page_labels_and_extraction_require_review"
            )

        report["pending_review"].append(
            {
                "source": relative_source,
                "reason": review_reason,
                "blank_pages": blank_pages,
            }
        )
        return chunks, "indexed"

    except Exception as exc:
        report["failed"].append(
            {
                "source": relative_source,
                "error": str(exc),
            }
        )
        print(f"失败：{relative_source}: {exc}")
        return [], "failed"


def save_candidate_index(
    chunks: list[dict],
    vectors: list[list[float]],
    build_id: str,
    build_mode: str,
) -> tuple[str, int]:
    """写入新的候选集合，不覆盖旧集合。"""

    client = chroma_client()

    collection_name = (
        f"{config.CHROMA_COLLECTION_NAME}"
        f"_candidate_{build_id}"
    )

    vector_dimension = len(vectors[0])

    collection = client.create_collection(
        name=collection_name,
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": config.EMBEDDING_MODEL,
            "vector_dimension": vector_dimension,
            "chunk_size": config.CHUNK_SIZE,
            "chunk_overlap": config.CHUNK_OVERLAP,
            "extractor_version": EXTRACTOR_VERSION,
            "build_status": "pending_review",
            "build_mode": build_mode,
            "build_id": build_id,
        },
    )

    try:
        for start in range(
            0,
            len(chunks),
            CHROMA_BATCH_SIZE,
        ):
            batch_chunks = chunks[
                start:start + CHROMA_BATCH_SIZE
            ]

            batch_vectors = vectors[
                start:start + CHROMA_BATCH_SIZE
            ]

            collection.add(
                ids=[
                    chunk["chunk_id"]
                    for chunk in batch_chunks
                ],
                documents=[
                    chunk["text"]
                    for chunk in batch_chunks
                ],
                metadatas=[
                    make_chroma_metadata(chunk)
                    for chunk in batch_chunks
                ],
                embeddings=batch_vectors,
            )

            completed = min(
                start + len(batch_chunks),
                len(chunks),
            )

            print(
                "Chroma 写入进度："
                f"{completed}/{len(chunks)}"
            )

    except Exception:
        # 只删除本次未完成的候选集合，不影响旧数据库
        client.delete_collection(collection_name)
        raise

    return collection_name, vector_dimension


def remember_source(
    state_sources: dict,
    relative_source: str,
    fingerprint: str,
    status: str,
    chunks: int,
) -> None:
    state_sources[relative_source] = {
        "fingerprint": fingerprint,
        "status": status,
        "chunks": chunks,
    }


def write_chunk_file(build_id: str, chunks: list[dict]) -> Path:
    chunk_path = (
        config.CHUNKS_DIR
        / f"chunks_candidate_{build_id}.jsonl"
    )

    with chunk_path.open("w", encoding="utf-8") as file:
        for chunk in chunks:
            file.write(
                json.dumps(chunk, ensure_ascii=False) + "\n"
            )

    return chunk_path


def write_report(build_id: str, report: dict) -> Path:
    report_path = (
        config.REPORTS_DIR
        / f"ingest_{build_id}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


def main() -> None:
    config.validate_config()
    config.ensure_directories()

    session = create_ollama_session()
    check_ollama(session)

    build_id = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    source_files = sorted(
        path
        for path in config.SOURCE_DIR.rglob("*")
        if path.is_file()
    )
    current_sources = {
        path.relative_to(config.SOURCE_DIR).as_posix()
        for path in source_files
    }

    report: dict = {
        "build_id": build_id,
        "embedding_model": config.EMBEDDING_MODEL,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "extractor_version": EXTRACTOR_VERSION,
        "success": [],
        "skipped": [],
        "pending_review": [],
        "failed": [],
        "reused": [],
        "added": [],
        "updated": [],
        "unchanged_pending": [],
        "deletion_candidates": [],
    }

    client = chroma_client()
    baseline, baseline_name = open_baseline(client)
    previous_state = load_incremental_state()
    rebuild_reason = None

    if baseline is None:
        rebuild_reason = "没有上一份向量集合"
    else:
        rebuild_reason = incompatible_index_settings(
            baseline.metadata
        )

    if rebuild_reason is None and previous_state is not None:
        rebuild_reason = incompatible_index_settings(
            previous_state
        )

    stored_records: list[dict] = []

    if rebuild_reason is None and baseline is not None:
        try:
            stored_records = load_collection_records(baseline)
        except Exception as exc:
            rebuild_reason = f"无法读取上一份向量：{exc}"

    grouped: dict[str, list[dict]] = {}

    for record in stored_records:
        grouped.setdefault(record["source"], []).append(record)

    mode = "full" if rebuild_reason else "incremental"
    state_sources: dict = {}
    reused_records: list[dict] = []
    new_chunks: list[dict] = []
    dropped_old_vectors = False

    print(f"入库模式：{mode}")

    if rebuild_reason:
        print(f"原因：{rebuild_reason}")

    if mode == "full":
        for source in source_files:
            chunks, status = ingest_one_source(source, report)

            if status == "skipped":
                continue

            relative_source = source.relative_to(
                config.SOURCE_DIR
            ).as_posix()
            fingerprint = sha256_file(source)
            remember_source(
                state_sources,
                relative_source,
                fingerprint,
                status,
                len(chunks),
            )

            if status == "indexed":
                new_chunks.extend(chunks)

    else:
        previous_sources = (
            previous_state.get("sources", {})
            if previous_state
            else {}
        )

        for source in source_files:
            relative_source = source.relative_to(
                config.SOURCE_DIR
            ).as_posix()
            extension = source.suffix.lower()

            if extension not in SUPPORTED_EXTENSIONS:
                report["skipped"].append(
                    {
                        "source": relative_source,
                        "reason": "unsupported_extension",
                    }
                )
                continue

            fingerprint = sha256_file(source)
            old_records = grouped.get(relative_source, [])
            previous = previous_sources.get(relative_source, {})

            if old_records and same_source_fingerprint(
                old_records,
                fingerprint,
            ):
                reused_records.extend(old_records)
                remember_source(
                    state_sources,
                    relative_source,
                    fingerprint,
                    "indexed",
                    len(old_records),
                )
                report["reused"].append(
                    {
                        "source": relative_source,
                        "chunks": len(old_records),
                    }
                )
                print(f"复用：{relative_source}")
                continue

            if (
                not old_records
                and previous.get("status") == "pending"
                and previous.get("fingerprint") == fingerprint
            ):
                remember_source(
                    state_sources,
                    relative_source,
                    fingerprint,
                    "pending",
                    0,
                )
                report["unchanged_pending"].append(
                    {
                        "source": relative_source,
                        "fingerprint": fingerprint,
                    }
                )
                print(f"跳过未变化的待复核文件：{relative_source}")
                continue

            chunks, status = ingest_one_source(
                source,
                report,
                fingerprint=fingerprint,
            )
            remember_source(
                state_sources,
                relative_source,
                fingerprint,
                status,
                len(chunks),
            )

            if status == "indexed":
                new_chunks.extend(chunks)
                bucket = "updated" if old_records else "added"
                report[bucket].append(
                    {
                        "source": relative_source,
                        "chunks": len(chunks),
                    }
                )
                continue

            if status == "failed" and old_records:
                reused_records.extend(old_records)
                report["failed"][-1]["retained_previous_chunks"] = (
                    len(old_records)
                )
                remember_source(
                    state_sources,
                    relative_source,
                    old_records[0]["fingerprint"],
                    "indexed",
                    len(old_records),
                )
                continue

            if status == "pending" and old_records:
                dropped_old_vectors = True
                report["updated"].append(
                    {
                        "source": relative_source,
                        "chunks": 0,
                        "removed_chunks": len(old_records),
                        "reason": "new_version_has_no_indexable_text",
                    }
                )

        for relative_source, old_records in sorted(grouped.items()):
            if relative_source in current_sources:
                continue

            reused_records.extend(old_records)
            report["deletion_candidates"].append(
                {
                    "source": relative_source,
                    "chunks": len(old_records),
                    "fingerprint": old_records[0].get("fingerprint", ""),
                    "action": "retained_pending_confirmation",
                }
            )

    content_changed = bool(new_chunks) or dropped_old_vectors
    report["mode"] = mode
    report["baseline_collection"] = baseline_name
    report["full_rebuild_reason"] = rebuild_reason

    if mode == "incremental" and not content_changed:
        write_incremental_state(
            state_sources,
            baseline_name,
            baseline.metadata.get("vector_dimension")
            if baseline is not None and baseline.metadata
            else None,
        )
        report.update(
            {
                "candidate_collection": baseline_name,
                "chunks": len(reused_records),
                "embedded_chunks": 0,
                "chroma_directory": str(config.CHROMA_PERSIST_DIR),
                "status": "unchanged",
            }
        )
        report_path = write_report(build_id, report)
        print("\n没有文件变化，未新建向量集合")
        print(f"继续使用：{baseline_name}")
        print(f"复用文件：{len(report['reused'])}")
        print(f"待删除确认：{len(report['deletion_candidates'])}")
        print(f"报告：{report_path}")
        return

    paired: list[tuple[dict, list[float]]] = [
        (chunk_from_stored_record(record), record["embedding"])
        for record in reused_records
    ]

    if new_chunks:
        print(f"\n需要重新嵌入 {len(new_chunks)} 个文本块")
        new_vectors = embed_texts(
            session,
            [chunk["text"] for chunk in new_chunks],
        )

        if paired and len(paired[0][1]) != len(new_vectors[0]):
            raise RuntimeError(
                "新旧向量维度不一致，已停止写入。"
                "请把 EXTRACTOR_VERSION 改为新值后整库重建"
            )

        paired.extend(zip(new_chunks, new_vectors))

    if not paired:
        report_path = write_report(build_id, report)
        raise RuntimeError(
            "没有生成任何可向量化文本块，"
            f"请查看报告：{report_path}"
        )

    paired.sort(
        key=lambda item: (
            item[0]["source"],
            item[0]["physical_page"],
            item[0]["page_chunk_index"],
        )
    )
    all_chunks = [chunk for chunk, _vector in paired]
    vectors = [vector for _chunk, vector in paired]
    chunk_path = write_chunk_file(build_id, all_chunks)

    collection_name, vector_dimension = save_candidate_index(
        all_chunks,
        vectors,
        build_id,
        mode,
    )
    write_incremental_state(
        state_sources,
        collection_name,
        vector_dimension,
    )

    report.update(
        {
            "candidate_collection": collection_name,
            "vector_dimension": vector_dimension,
            "chunks": len(all_chunks),
            "embedded_chunks": len(new_chunks),
            "chunk_file": str(chunk_path),
            "chroma_directory": str(config.CHROMA_PERSIST_DIR),
            "status": "pending_review",
        }
    )
    report_path = write_report(build_id, report)

    candidate_pointer = (
        config.CHROMA_PERSIST_DIR
        / "candidate_collection.txt"
    )
    candidate_pointer.write_text(
        collection_name + "\n",
        encoding="utf-8",
    )

    print("\n候选向量库构建完成")
    print(f"模式：{mode}")
    print(f"集合：{collection_name}")
    print(f"文本块：{len(all_chunks)}")
    print(f"其中新嵌入：{len(new_chunks)}")
    print(f"复用文件：{len(report['reused'])}")
    print(f"新增文件：{len(report['added'])}")
    print(f"更新文件：{len(report['updated'])}")
    print(f"待删除确认：{len(report['deletion_candidates'])}")
    print(f"向量维度：{vector_dimension}")
    print(f"报告：{report_path}")
    print(
        "状态：pending_review；"
        "完成页面、来源日期和来源等级复核后再正式验收"
    )


if __name__ == "__main__":
    main()