"""武汉大学本地 RAG 项目配置。"""

from pathlib import Path


# ============================================================
# 项目路径
# ============================================================

PROJECT_DIR = Path("/home/wyf/local_rag_first")

RAG_PROJECT_DIR = (
    PROJECT_DIR
    / "data"
    / "whu-rag"
)

KNOWLEDGE_BASE_DIR = (
    RAG_PROJECT_DIR
    / "学校知识库"
)

# 已复制到 WSL 中的正式资料目录
SOURCE_DIR = KNOWLEDGE_BASE_DIR / "source"

# Windows 原始资料目录，仅用于追踪来源，不建议直接构建向量库
WINDOWS_SOURCE_DIR = Path(
    "/mnt/c/Users/Wyf/OneDrive/Desktop/"
    "本科比赛/AI+应用校赛/database_1/"
    "学校知识库/source"
)

NORMALIZED_DIR = KNOWLEDGE_BASE_DIR / "normalized"
SNAPSHOTS_DIR = KNOWLEDGE_BASE_DIR / "snapshots"
CHUNKS_DIR = KNOWLEDGE_BASE_DIR / "chunks"
REVIEWS_DIR = KNOWLEDGE_BASE_DIR / "reviews"
REPORTS_DIR = KNOWLEDGE_BASE_DIR / "reports"

MANIFEST_PATH = KNOWLEDGE_BASE_DIR / "manifest.json"
KNOWLEDGE_BASE_CONFIG_PATH = KNOWLEDGE_BASE_DIR / "knowledge-base.yaml"


# ============================================================
# Ollama
# ============================================================

OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# 用于根据检索结果生成最终答案
CHAT_MODEL = "qwen3.5:4b"

# 用于把文档和用户问题转换成向量
EMBEDDING_MODEL = "qwen3-embedding:0.6b"

# Ollama 的内部模型存储目录
# 程序通常不应直接读取这里的哈希文件
OLLAMA_MODELS_DIR = Path("/home/wyf/.ollama/models")

CHAT_MODEL_MANIFEST = (
    OLLAMA_MODELS_DIR
    / "manifests"
    / "registry.ollama.ai"
    / "library"
    / "qwen3.5"
    / "4b"
)

EMBEDDING_MODEL_MANIFEST = (
    OLLAMA_MODELS_DIR
    / "manifests"
    / "registry.ollama.ai"
    / "library"
    / "qwen3-embedding"
    / "0.6b"
)


# ============================================================
# 文本切片
# ============================================================

# 单位为字符，不是 token
CHUNK_SIZE = 1600
CHUNK_OVERLAP = 160
CHUNK_UNIT = "characters"

# 正式知识库不得让切片跨越原始 PDF 物理页面
KEEP_PAGE_BOUNDARIES = True


# ============================================================
# Chroma 向量数据库
# ============================================================

VECTOR_DB_TYPE = "chroma"

CHROMA_PERSIST_DIR = KNOWLEDGE_BASE_DIR / "chroma"

CHROMA_COLLECTION_NAME = "whu_knowledge_base"

# 每次问题默认召回的文本块数量
RETRIEVAL_TOP_K = 5


# ============================================================
# 学校信息
# ============================================================

UNIVERSITY_NAME = "武汉大学"
UNIVERSITY_ABBREVIATION = "WHU"


def ensure_directories() -> None:
    """创建允许由程序写入的工作目录，不修改 source 原始资料。"""

    for directory in (
        NORMALIZED_DIR,
        SNAPSHOTS_DIR,
        CHUNKS_DIR,
        REVIEWS_DIR,
        REPORTS_DIR,
        CHROMA_PERSIST_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def validate_config() -> None:
    """启动前检查关键目录与模型清单。"""

    required_paths = {
        "资料目录": SOURCE_DIR,
        "知识库清单": MANIFEST_PATH,
        "问答模型清单": CHAT_MODEL_MANIFEST,
        "向量模型清单": EMBEDDING_MODEL_MANIFEST,
    }

    missing = [
        f"{name}: {path}"
        for name, path in required_paths.items()
        if not path.exists()
    ]

    if missing:
        details = "\n".join(missing)
        raise FileNotFoundError(
            "以下必要路径不存在：\n"
            f"{details}"
        )


ensure_directories()
