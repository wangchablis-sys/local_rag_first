#!/usr/bin/env python3
"""抓取公开网页和附件，先写入 snapshots，确认后再更新 source。

默认只抓取，不修改 source：

    python crawler.py

查看报告和 snapshots/url_paths.json。路径不对时先改映射，再执行：

    python crawler.py --apply-latest

抓取并立即写入 source：

    python crawler.py --apply

消失的网址只记入报告，不删除 source 里的旧文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

import config


USER_AGENT = "WHULocalRAG/1.0 (personal local knowledge base)"
REQUEST_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 45
MAX_BYTES = 40 * 1024 * 1024
MAX_ATTACHMENTS_PER_PAGE = 40

FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
HTML_EXTENSIONS = {".html", ".htm"}
KNOWN_EXTENSIONS = FILE_EXTENSIONS | HTML_EXTENSIONS

URL_LIST_PATH = config.SOURCE_DIR / "网页链接.txt"
URL_MAP_PATH = config.SNAPSHOTS_DIR / "url_paths.json"
LATEST_CRAWL_PATH = config.SNAPSHOTS_DIR / "latest_crawl.txt"


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value.strip())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def read_seed_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到网址清单：{path}")

    urls: list[str] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if urlsplit(line).scheme not in {"http", "https"}:
            raise ValueError(f"第 {line_number} 行不是网址：{line}")

        if line not in seen:
            seen.add(line)
            urls.append(line)

    return urls


def is_whu_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "whu.edu.cn" or host.endswith(".whu.edu.cn")


def safe_filename(name: str) -> str:
    filename = unquote(Path(name).name)
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    filename = filename.strip(" .")
    return filename[:180] or "download"


def safe_relative_path(value: str) -> str:
    path = value.replace("\\", "/").strip()
    parts = [part for part in path.split("/") if part and part != "."]

    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"非法相对路径：{value}")

    return "/".join(parts)


def content_disposition_filename(header: str | None) -> str | None:
    if not header:
        return None

    message = EmailMessage()
    message["Content-Disposition"] = header
    filename = message.get_filename()

    if filename:
        return safe_filename(filename)

    return None


def extension_for(url: str, content_type: str, filename: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()

        if suffix in KNOWN_EXTENSIONS:
            return suffix

    url_suffix = Path(unquote(urlsplit(url).path)).suffix.lower()

    if url_suffix in KNOWN_EXTENSIONS:
        return url_suffix

    content_type = content_type.lower().split(";", 1)[0].strip()
    by_type = {
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    return by_type.get(content_type, url_suffix)


def is_attachment_url(url: str) -> bool:
    path = unquote(urlsplit(url).path).lower()

    if Path(path).suffix.lower() in FILE_EXTENSIONS:
        return True

    return path.endswith("/download.jsp") or "/download.jsp" in path


def extract_attachment_urls(page_url: str, html_bytes: bytes) -> list[str]:
    parser = LinkExtractor()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    found: list[str] = []
    seen: set[str] = set()

    for href in parser.links:
        absolute = urljoin(page_url, href)
        absolute = absolute.split("#", 1)[0]

        if not is_whu_url(absolute) or not is_attachment_url(absolute):
            continue

        if absolute not in seen:
            seen.add(absolute)
            found.append(absolute)

        if len(found) >= MAX_ATTACHMENTS_PER_PAGE:
            break

    return found


def index_source_files() -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}

    for path in config.SOURCE_DIR.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(config.SOURCE_DIR).as_posix()
        matches.setdefault(path.name.lower(), []).append(relative)

    return matches


def load_url_map() -> dict:
    if not URL_MAP_PATH.exists():
        return {}

    return json.loads(URL_MAP_PATH.read_text(encoding="utf-8"))


def save_url_map(url_map: dict) -> None:
    URL_MAP_PATH.write_text(
        json.dumps(url_map, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def choose_relative_path(
    url: str,
    filename: str,
    url_map: dict,
    source_index: dict[str, list[str]],
) -> tuple[str, bool]:
    existing = url_map.get(url)

    if existing and existing.get("path"):
        return safe_relative_path(existing["path"]), bool(
            existing.get("matched_existing", False)
        )

    matches = source_index.get(filename.lower(), [])

    if len(matches) == 1:
        return matches[0], True

    host = (urlsplit(url).hostname or "unknown").lower()
    relative = f"06_网络采集/{host}/{filename}"
    used = {
        safe_relative_path(item["path"])
        for item in url_map.values()
        if item.get("path")
    }
    candidate = relative

    if candidate in used or (config.SOURCE_DIR / candidate).exists():
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        candidate = f"06_网络采集/{host}/{stem}_{digest}{suffix}"

    return safe_relative_path(candidate), False


class RobotsCache:
    def __init__(self):
        self.parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self.parsers:
            parser = RobotFileParser()
            robots_url = f"{origin}/robots.txt"

            try:
                request = Request(robots_url, headers={"User-Agent": USER_AGENT})

                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    text = response.read(1024 * 1024).decode(
                        "utf-8",
                        errors="replace",
                    )

                parser.parse(text.splitlines())
                self.parsers[origin] = parser
            except Exception:
                self.parsers[origin] = None

        parser = self.parsers[origin]

        if parser is None:
            return True, "robots_unavailable_assumed_allowed"

        if parser.can_fetch(USER_AGENT, url):
            return True, ""

        return False, "blocked_by_robots"


def fetch(url: str) -> dict:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BYTES + 1)
            return {
                "ok": True,
                "status": getattr(response, "status", 200),
                "final_url": response.geturl(),
                "content_type": response.headers.get("Content-Type", ""),
                "content_disposition": response.headers.get(
                    "Content-Disposition",
                    "",
                ),
                "body": body,
                "error": "",
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "final_url": exc.geturl() or url,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "content_disposition": "",
            "body": exc.read(65536),
            "error": f"HTTP {exc.code}",
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": 0,
            "final_url": url,
            "content_type": "",
            "content_disposition": "",
            "body": b"",
            "error": str(exc.reason),
        }


def load_previous_manifest() -> dict[str, dict]:
    if not LATEST_CRAWL_PATH.exists():
        return {}

    run_name = LATEST_CRAWL_PATH.read_text(encoding="utf-8").strip()

    if not run_name:
        return {}

    manifest_path = config.SNAPSHOTS_DIR / run_name / "manifest.json"

    if not manifest_path.exists():
        return {}

    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        entry["url"]: entry
        for entry in entries
        if entry.get("url") and entry.get("sha256")
    }


def decide_change(url: str, digest: str, previous: dict[str, dict]) -> str:
    old = previous.get(url)

    if old is None:
        return "added"

    if old.get("sha256") == digest:
        return "unchanged"

    return "modified"


def content_matches_extension(extension: str, content_type: str) -> bool:
    content_type = content_type.lower()

    if extension in HTML_EXTENSIONS:
        return "html" in content_type or "text/plain" in content_type or content_type == ""

    if "text/html" in content_type or "application/xhtml" in content_type:
        return False

    return True


def crawl(apply_after: bool) -> Path:
    config.ensure_directories()
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = config.SNAPSHOTS_DIR / f"crawl_{run_id}"
    files_dir = run_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    seeds = read_seed_urls(URL_LIST_PATH)
    previous = load_previous_manifest()
    url_map = load_url_map()
    source_index = index_source_files()
    robots = RobotsCache()
    entries: list[dict] = []
    seen_urls: set[str] = set()
    queue: list[tuple[str, bool]] = [(url, True) for url in seeds]

    while queue:
        url, discover_attachments = queue.pop(0)

        if url in seen_urls:
            continue

        seen_urls.add(url)
        print(f"抓取：{url}")

        if not is_whu_url(url):
            entries.append(
                {
                    "url": url,
                    "status": "failed",
                    "error": "outside_whu_domain",
                }
            )
            continue

        allowed, robots_note = robots.allowed(url)

        if not allowed:
            entries.append(
                {
                    "url": url,
                    "status": "blocked_by_robots",
                    "error": robots_note,
                }
            )
            print("  robots.txt 不允许抓取")
            time.sleep(REQUEST_INTERVAL_SECONDS)
            continue

        result = fetch(url)
        time.sleep(REQUEST_INTERVAL_SECONDS)

        if not result["ok"]:
            entries.append(
                {
                    "url": url,
                    "final_url": result["final_url"],
                    "status": "failed",
                    "http_status": result["status"],
                    "error": result["error"],
                }
            )
            print(f"  失败：{result['error']}")
            continue

        if not is_whu_url(result["final_url"]):
            entries.append(
                {
                    "url": url,
                    "final_url": result["final_url"],
                    "status": "failed",
                    "error": "redirected_outside_whu_domain",
                }
            )
            continue

        if len(result["body"]) > MAX_BYTES:
            entries.append(
                {
                    "url": url,
                    "final_url": result["final_url"],
                    "status": "failed",
                    "error": "file_too_large",
                }
            )
            print("  失败：文件超过 40MB")
            continue

        filename = content_disposition_filename(result["content_disposition"])
        extension = extension_for(
            result["final_url"],
            result["content_type"],
            filename,
        )

        if extension not in KNOWN_EXTENSIONS:
            entries.append(
                {
                    "url": url,
                    "final_url": result["final_url"],
                    "status": "failed",
                    "content_type": result["content_type"],
                    "error": "unsupported_type",
                }
            )
            print(f"  跳过不支持的类型：{result['content_type'] or extension}")
            continue

        if not content_matches_extension(extension, result["content_type"]):
            entries.append(
                {
                    "url": url,
                    "final_url": result["final_url"],
                    "status": "failed",
                    "content_type": result["content_type"],
                    "error": "content_type_mismatch",
                }
            )
            print("  失败：返回的网页内容和文件类型不一致")
            continue

        if not filename:
            url_name = safe_filename(unquote(urlsplit(result["final_url"]).path))
            filename = url_name if Path(url_name).suffix else f"{url_name}{extension}"

        if Path(filename).suffix.lower() not in KNOWN_EXTENSIONS:
            filename = f"{Path(filename).stem}{extension}"

        relative_path, matched_existing = choose_relative_path(
            url,
            safe_filename(filename),
            url_map,
            source_index,
        )
        url_map[url] = {
            "path": relative_path,
            "matched_existing": matched_existing,
        }
        digest = sha256_bytes(result["body"])
        change = decide_change(url, digest, previous)
        snapshot_path = files_dir / relative_path
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(result["body"])

        entry = {
            "url": url,
            "final_url": result["final_url"],
            "path": relative_path,
            "matched_existing": matched_existing,
            "sha256": digest,
            "bytes": len(result["body"]),
            "content_type": result["content_type"],
            "snapshot": str(snapshot_path),
            "status": change,
            "error": "",
            "robots": robots_note,
        }
        entries.append(entry)
        print(f"  {change} -> {relative_path}")

        if discover_attachments and extension in HTML_EXTENSIONS:
            for attachment_url in extract_attachment_urls(result["final_url"], result["body"]):
                if attachment_url not in seen_urls:
                    queue.append((attachment_url, False))

    missing = []

    for url, old in sorted(previous.items()):
        if url in seen_urls:
            continue

        missing.append(
            {
                "url": url,
                "path": old.get("path", ""),
                "sha256": old.get("sha256", ""),
                "status": "missing_from_list",
                "action": "retained_in_source",
            }
        )

    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "missing.json").write_text(
        json.dumps(missing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    save_url_map(url_map)
    LATEST_CRAWL_PATH.write_text(run_dir.name + "\n", encoding="utf-8")

    report = {
        "run_id": run_id,
        "snapshot": str(run_dir),
        "added": sum(item.get("status") == "added" for item in entries),
        "modified": sum(item.get("status") == "modified" for item in entries),
        "unchanged": sum(item.get("status") == "unchanged" for item in entries),
        "failed": sum(item.get("status") == "failed" for item in entries),
        "blocked_by_robots": sum(
            item.get("status") == "blocked_by_robots" for item in entries
        ),
        "missing_from_list": len(missing),
        "unmatched_new_paths": [
            item["path"]
            for item in entries
            if item.get("status") in {"added", "modified", "unchanged"}
            and not item.get("matched_existing")
        ],
    }
    report_path = config.REPORTS_DIR / f"crawl_{run_id}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n抓取完成")
    print(f"新增：{report['added']}")
    print(f"变化：{report['modified']}")
    print(f"未变化：{report['unchanged']}")
    print(f"失败：{report['failed']}")
    print(f"robots 拒绝：{report['blocked_by_robots']}")
    print(f"清单中消失但未删除：{report['missing_from_list']}")
    print(f"快照：{run_dir}")
    print(f"路径映射：{URL_MAP_PATH}")
    print(f"报告：{report_path}")

    if report["unmatched_new_paths"]:
        print(
            "以下文件没有对应到现有 source 路径，"
            "确认 url_paths.json 后再写入 source："
        )

        for relative_path in report["unmatched_new_paths"]:
            print(f"  {relative_path}")

    if apply_after:
        apply_latest()

    return run_dir


def apply_latest() -> None:
    if not LATEST_CRAWL_PATH.exists():
        raise FileNotFoundError("还没有抓取快照，请先运行 python crawler.py")

    run_name = LATEST_CRAWL_PATH.read_text(encoding="utf-8").strip()
    run_dir = config.SNAPSHOTS_DIR / run_name
    manifest_path = run_dir / "manifest.json"
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    url_map = load_url_map()
    copied = 0
    skipped = 0

    for entry in entries:
        if entry.get("status") not in {"added", "modified", "unchanged"}:
            continue

        url = entry["url"]
        mapped = url_map.get(url, {})
        relative_path = safe_relative_path(
            mapped.get("path") or entry.get("path") or ""
        )
        snapshot_path = Path(entry["snapshot"])

        if not snapshot_path.is_file():
            print(f"跳过，快照不存在：{snapshot_path}")
            continue

        destination = config.SOURCE_DIR / relative_path
        new_hash = sha256_bytes(snapshot_path.read_bytes())

        if destination.is_file() and sha256_file(destination) == new_hash:
            skipped += 1
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, destination)
        copied += 1
        print(f"写入 source：{relative_path}")

    print(f"\n已写入 {copied} 个文件，{skipped} 个文件内容未变化")
    print("没有删除任何 source 文件")
    print("接下来可运行：python ingest.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="抓取武汉大学公开页面和附件，并按需更新 source"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="抓取完成后，把新增和变化的文件写入 source",
    )
    parser.add_argument(
        "--apply-latest",
        action="store_true",
        help="不重新抓取，把最近一次快照写入 source",
    )
    args = parser.parse_args()

    if args.apply and args.apply_latest:
        raise SystemExit("--apply 和 --apply-latest 只能选择一个")

    if args.apply_latest:
        apply_latest()
        return

    crawl(apply_after=args.apply)


if __name__ == "__main__":
    main()
