# 武汉大学本地 RAG 知识库

这是一个运行在 **WSL Ubuntu** 中的本地检索增强生成（RAG）项目。项目能够整理武汉大学相关的 PDF、Word、Excel 和网页资料，使用 Ollama 生成文本向量，将向量持久化到 Chroma，并通过 Streamlit 提供带来源引用的中文问答界面。

> 本项目不是武汉大学官方系统。模型生成内容仅供参考，重要事项请以原始文件和学校官方通知为准。

## 工作流程

```text
PDF / Word / Excel / HTML
          │
          ▼
    文本提取与清洗
          │
          ▼
  按物理页切片（1600 字符）
          │
          ▼
qwen3-embedding:0.6b 生成向量
          │
          ▼
       Chroma 检索
          │
          ▼
 qwen3.5:4b 根据资料生成答案
          │
          ▼
 Streamlit 展示答案、文件名和页码
```

## 主要功能

- 支持 `.pdf`、`.doc`、`.docx`、`.xls`、`.xlsx`、`.html` 和 `.htm`。
- PDF 切片不跨越原始物理页面，便于追溯引用。
- 以文件哈希识别新增、更新和未变化的资料，支持增量入库。
- 新索引先写入候选 Chroma 集合，避免构建失败破坏上一份可用索引。
- 回答仅使用检索到的资料，并显示原始文件、物理页码和向量距离。
- 可抓取 `whu.edu.cn` 域名下的公开网页及其附件；默认先保存快照，确认后再更新正式资料目录。
- 所有模型和数据库均可在本机运行，问答内容无需发送给云端大模型服务。

## 技术栈与默认配置

| 项目 | 默认值 |
| --- | --- |
| 问答模型 | `qwen3.5:4b` |
| 向量模型 | `qwen3-embedding:0.6b` |
| 模型服务 | Ollama，`http://127.0.0.1:11434` |
| 向量数据库 | Chroma PersistentClient |
| Chroma 集合 | `whu_knowledge_base`，或最新候选集合 |
| 切片大小 | 1600 字符 |
| 切片重叠 | 160 字符 |
| 默认召回数量 | 5 个文本块 |
| Web 界面 | Streamlit，`http://127.0.0.1:8501` |

这些参数集中在 [`config.py`](config.py) 中。

## 目录结构

```text
local_rag_first/
├── app.py              # Streamlit 问答界面
├── config.py           # 路径、模型、切片和检索配置
├── crawler.py          # 武大公开网页及附件抓取器
├── ingest.py           # 文档解析、切片、向量化和增量入库
├── rag.py              # 向量检索、提示词组装和 Ollama 问答
├── requirements.txt    # Python 依赖
├── start.sh            # WSL/Linux 启动脚本
└── data/whu-rag/学校知识库/
    ├── source/         # 正式原始资料
    ├── normalized/     # 规范化中间结果
    ├── snapshots/      # 网页抓取快照和路径映射
    ├── chunks/         # 可审查的切片 JSONL
    ├── reviews/        # 提取结果复核材料
    ├── reports/        # 入库和抓取报告
    └── chroma/         # Chroma 持久化数据
```

## 环境要求

- Windows 11 与 WSL 2 Ubuntu，或普通 Linux 环境
- Python 3 和 `venv`
- Ollama
- LibreOffice：用于把旧版及 Office 文档转换成可解析格式
- NVIDIA GPU 为可选项；没有 GPU 时可使用 CPU，但生成速度会明显降低

在 Ubuntu 中安装基础依赖：

```bash
sudo apt update
sudo apt install -y python3-venv libreoffice
```

## 安装

克隆仓库并进入项目：

```bash
git clone https://github.com/wangchablis-sys/local_rag_first.git
cd local_rag_first
```

创建 Python 虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

下载默认模型：

```bash
ollama pull qwen3.5:4b
ollama pull qwen3-embedding:0.6b
```

如果项目不在 `/home/wyf/local_rag_first`，请先修改 `config.py` 中的 `PROJECT_DIR`。在其他电脑上使用时，还应根据实际情况修改 `WINDOWS_SOURCE_DIR`。

## 启动 Ollama

在一个 WSL 终端中运行：

```bash
ollama serve
```

看到下面的信息说明服务已经启动：

```text
Listening on 127.0.0.1:11434
```

可以在另一个终端检查服务和模型：

```bash
curl http://127.0.0.1:11434/api/tags
ollama list
```

同一时刻只需要运行一个 `ollama serve`。

## 准备资料并构建向量库

把资料放入：

```text
data/whu-rag/学校知识库/source/
```

然后执行：

```bash
source .venv/bin/activate
python ingest.py
```

入库过程会：

1. 检查 Ollama 与模型是否可用；
2. 解析受支持的文件；
3. 按物理页清洗并切分文本；
4. 使用 `qwen3-embedding:0.6b` 批量生成向量；
5. 把索引保存到 Chroma 候选集合；
6. 在 `reports/` 写入本次构建报告，在 `chunks/` 写入可审查的切片文件。

再次执行 `python ingest.py` 时，未变化的文件会复用已有向量。源目录中消失的文件只会被列为待确认删除项，不会立即从上一份索引中删除。

## 抓取公开网页

将需要抓取的武汉大学公开网址逐行写入：

```text
data/whu-rag/学校知识库/source/网页链接.txt
```

推荐先只抓取快照：

```bash
python crawler.py
```

检查抓取报告、最新快照以及 `snapshots/url_paths.json`，确认网页和附件的目标路径后再应用：

```bash
python crawler.py --apply-latest
```

也可以抓取完成后立即写入 `source/`：

```bash
python crawler.py --apply
```

抓取器仅接受 `whu.edu.cn` 及其子域名，遵守 `robots.txt`，请求间隔默认为 1 秒，单个文件上限为 40 MB。消失的网址只记入报告，不自动删除旧资料。

资料更新完成后，需要再次执行：

```bash
python ingest.py
```

## 启动问答界面

确保 Ollama 正在运行，然后执行：

```bash
./start.sh
```

如果脚本没有执行权限：

```bash
chmod +x start.sh
./start.sh
```

也可以直接运行：

```bash
source .venv/bin/activate
streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

在 Windows 浏览器打开：

```text
http://127.0.0.1:8501
```

一次问答会先使用向量模型生成问题向量，从 Chroma 召回最接近的资料片段，再把这些片段交给问答模型生成回答。因此第一次提问可能因模型加载而更慢。

## 常用检查命令

```bash
# 查看已经下载的 Ollama 模型
ollama list

# 查看当前加载到内存或显存中的模型
ollama ps

# 检查 Ollama API
curl http://127.0.0.1:11434/api/tags

# 查看 GPU 使用情况
nvidia-smi

# 查看 Git 状态
git status
```

## 常见问题

### 无法连接 Ollama

确认 `ollama serve` 正在运行，并检查：

```bash
curl http://127.0.0.1:11434/api/tags
```

### 提示找不到模型

```bash
ollama pull qwen3.5:4b
ollama pull qwen3-embedding:0.6b
```

### Word 或 Excel 解析失败

确认已经安装 LibreOffice：

```bash
libreoffice --version
```

### 第一次问答很慢

第一次请求需要把向量模型和问答模型加载到内存或显存中。还可以检查 `ollama ps` 和 `nvidia-smi`，并适当减小 `RETRIEVAL_TOP_K` 或上下文长度。

### 修改配置后仍复用旧索引

`ingest.py` 会检查嵌入模型、向量维度、切片配置和提取器版本。与旧索引不兼容时会自动选择整库重建；普通文件变化则使用增量模式。

## 数据与安全说明

- 不要把 API Key、密码或其他密钥写进代码或提交到 Git。
- 使用公开仓库前，请确认原始资料允许公开传播，并删除包含个人信息或受限内容的文件。
- `source/`、`chroma/`、`snapshots/`、`reports/`、虚拟环境和模型文件通常体积较大，生产仓库可按实际发布需求使用 `.gitignore` 或 Git LFS 管理。
- Ollama 默认监听 `127.0.0.1`，仅供本机访问；如改为对局域网开放，应额外配置访问控制和防火墙。

## 免责声明

本项目用于本地资料检索和辅助问答。大模型可能误解资料或生成错误内容，引用页码也应结合原文复核。涉及学籍、成绩、奖助学金和学校规章等重要事项时，请以武汉大学官方文件及相关管理部门的最新解释为准。
