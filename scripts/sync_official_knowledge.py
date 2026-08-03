"""抓取航空公司官网公开页面，生成可审计快照并同步 PostgreSQL RAG。

默认执行两个阶段：

1. 从 ``official_sources.json`` 中的固定白名单 URL 下载 HTML，只抽取 ``main``
   正文和与业务关键词相关的块，写入 UTF-8 文本快照；
2. 生成 ``official_policies.json`` 切块文件，并通过应用的 PostgreSQL Knowledge
   Store 计算真实 Embedding、执行增量 upsert。

脚本不接受任意 URL，不删除旧文档，不在应用请求链路实时抓网页。官网页面发生
变化时会生成新的内容 Hash；数据库保留旧版本以便审计。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import tempfile
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from airline_mvp.config import ConfigurationError, RuntimeSettings
from airline_mvp.knowledge import build_knowledge_service, load_policy_documents
from airline_mvp.paths import KNOWLEDGE_ROOT, PROJECT_ROOT, RUNTIME_ROOT
from airline_mvp.persistence import PostgreSQLDatabase


MANIFEST_PATH = KNOWLEDGE_ROOT / "official_sources.json"
CHUNKS_PATH = KNOWLEDGE_ROOT / "official_policies.json"
SNAPSHOT_ROOT = KNOWLEDGE_ROOT / "official_snapshots"
ALLOWED_HOSTS = {"www.emirates.com"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "dt", "dd"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}


class MainTextExtractor(HTMLParser):
    """仅抽取 ``main`` 中的标题、段落和列表项。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.main_depth = 0
        self.skip_depth = 0
        self.current_tag: str | None = None
        self.current_parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag == "main":
            self.main_depth += 1
        if self.main_depth and tag in SKIP_TAGS:
            self.skip_depth += 1
        if self.main_depth and not self.skip_depth and tag in BLOCK_TAGS:
            self._flush()
            self.current_tag = tag

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.main_depth and not self.skip_depth and tag == self.current_tag:
            self._flush()
        if self.main_depth and tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == "main" and self.main_depth:
            self._flush()
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.main_depth and not self.skip_depth and self.current_tag:
            self.current_parts.append(data)

    def _flush(self) -> None:
        text = " ".join(self.current_parts)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if len(text) >= 8:
            self.blocks.append(text)
        self.current_tag = None
        self.current_parts = []


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _load_manifest() -> list[dict[str, Any]]:
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        sources = json.load(handle)
    for source in sources:
        parsed = urlparse(source["url"])
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"官网来源不在白名单中：{source['url']}")
        if not re.fullmatch(r"[a-z0-9_]+", source["sourceId"]):
            raise ValueError(f"非法 sourceId：{source['sourceId']}")
    return sources


def _download(source: dict[str, Any]) -> tuple[str, str, str | None]:
    request = urllib.request.Request(
        source["url"],
        headers={
            "User-Agent": "EchoMind-Airline-MVP-KnowledgeSync/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"官网请求被重定向到非白名单地址：{response.geturl()}")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"不支持的官网内容类型：{content_type}")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("官网响应超过 5MB 安全上限")
        charset = response.headers.get_content_charset() or "utf-8"
    raw = payload.decode(charset, errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = (
        html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
        if title_match
        else source["title"]
    )
    publish_match = re.search(
        r'<meta\s+name="publishdate"\s+content="(\d{8})',
        raw,
        re.I,
    )
    published_at = None
    if publish_match:
        raw_date = publish_match.group(1)
        published_at = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return raw, title, published_at


def _select_blocks(raw: str, keywords: list[str]) -> list[str]:
    parser = MainTextExtractor()
    parser.feed(raw)
    unique: list[str] = []
    seen: set[str] = set()
    for block in parser.blocks:
        normalized = re.sub(r"\s+", " ", block).strip()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(normalized)

    selected_indexes: set[int] = set()
    for index, block in enumerate(unique):
        if any(keyword.lower() in block.lower() for keyword in keywords):
            selected_indexes.update(
                candidate
                for candidate in (index - 1, index, index + 1)
                if 0 <= candidate < len(unique)
            )
    selected = [unique[index] for index in sorted(selected_indexes)]
    if not selected:
        raise ValueError("官网正文中没有找到配置的业务关键词")
    # 防止页面模板异常导致单个来源无限膨胀；完整来源仍可通过 URL 下钻。
    return selected[:120]


def _chunk_blocks(blocks: list[str], max_chars: int = 900) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        if current and length + len(block) + 1 > max_chars:
            chunks.append("\n".join(current))
            current = current[-1:]
            length = sum(len(item) + 1 for item in current)
        current.append(block)
        length += len(block) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def fetch_and_build() -> dict[str, int]:
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records: list[dict[str, Any]] = []
    sources = _load_manifest()
    for source in sources:
        raw, page_title, published_at = _download(source)
        blocks = _select_blocks(raw, source["keywords"])
        clean_text = "\n\n".join(blocks)
        source_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
        snapshot_relative = (
            Path("official_snapshots") / f"{source['sourceId']}.txt"
        )
        snapshot_path = KNOWLEDGE_ROOT / snapshot_relative
        header = (
            f"Title: {page_title}\n"
            f"Source: {source['url']}\n"
            f"Retrieved-At: {retrieved_at}\n"
            f"Published-At: {published_at or 'unknown'}\n"
            f"Content-SHA256: {source_hash}\n\n"
        )
        _atomic_write(snapshot_path, header + clean_text + "\n")

        version = f"{retrieved_at[:10]}.{source_hash[:12]}"
        valid_from = published_at or "2000-01-01"
        chunks = _chunk_blocks(blocks)
        for domain in source["domains"]:
            for index, chunk in enumerate(chunks, start=1):
                chunk_hash = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                records.append(
                    {
                        "documentId": source["sourceId"],
                        "version": version,
                        "title": source["title"],
                        "domain": domain,
                        "documentType": source["documentType"],
                        "authority": "airline_official_web",
                        "validFrom": valid_from,
                        "validTo": None,
                        "status": "active",
                        "section": f"{domain}.web-{index:03d}",
                        "locale": source["locale"],
                        "text": chunk,
                        "carrierCodes": source["carrierCodes"],
                        "sourceUrl": source["url"],
                        "sourcePath": str(snapshot_relative).replace("\\", "/"),
                        "retrievedAt": retrieved_at,
                        "contentSha256": chunk_hash,
                        "metadata": {
                            "sourceId": source["sourceId"],
                            "pageTitle": page_title,
                            "publishedAt": published_at,
                            "sourceContentSha256": source_hash,
                            "extractorVersion": "main-keyword-v1",
                            "chunkIndex": index,
                            "chunkCount": len(chunks),
                        },
                    }
                )

    _atomic_write(
        CHUNKS_PATH,
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
    )
    return {"sources": len(sources), "chunks": len(records)}


def sync_database() -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = RuntimeSettings.from_env()
    if settings.database_backend != "postgres" or not settings.database_url:
        raise ConfigurationError(
            "知识同步需要 AIRLINE_MVP_DATABASE_BACKEND=postgres 和数据库 URL"
        )
    settings = replace(settings, knowledge_backend="postgres")
    database = PostgreSQLDatabase(
        settings.database_url,
        pool_size=settings.database_pool_size,
    )
    try:
        service = build_knowledge_service(
            settings=settings,
            runtime_root=RUNTIME_ROOT,
            database=database,
        )
        return {
            "backend": type(service.store).__name__,
            "embedding": settings.embedding_backend,
            "documents": len(load_policy_documents()),
        }
    finally:
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="只更新官网快照和切块 JSON，不连接 PostgreSQL",
    )
    parser.add_argument(
        "--database-only",
        action="store_true",
        help="只把已有本地切块同步到 PostgreSQL，不访问官网",
    )
    args = parser.parse_args()
    if args.fetch_only and args.database_only:
        parser.error("--fetch-only 与 --database-only 不能同时使用")

    result: dict[str, Any] = {}
    if not args.database_only:
        result["fetch"] = fetch_and_build()
    if not args.fetch_only:
        result["database"] = sync_database()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
