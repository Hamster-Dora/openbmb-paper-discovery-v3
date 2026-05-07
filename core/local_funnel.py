"""
漏斗 1：本地 PDF 全文 + Regex 预筛（PRD 3.2；零 API 消耗）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Final

import aiohttp
import fitz

from config.settings import REGEX_KEYWORDS
from database.models import Paper, db
from utils.arxiv_pdf import resolvable_open_pdf_url
from utils.ssl_context import build_ssl_context

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_PDF_DIR: Final[Path] = _PROJECT_ROOT / "data" / "temp_pdfs"

# 预编译：任一关键词字面命中（大小写不敏感，兼容 PDF 内大小写变体）
_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(k) for k in REGEX_KEYWORDS),
    flags=re.IGNORECASE,
)


def extract_full_text_with_pymupdf(pdf_path: str | Path) -> str:
    """用 PyMuPDF 提取 PDF **全部页**正文文本。"""
    path = Path(pdf_path)
    parts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text() or "")
    return "\n".join(parts)


async def extract_full_text_with_pymupdf_async(pdf_path: str | Path) -> str:
    """在线程中执行 PyMuPDF 解析，避免阻塞事件循环。"""
    return await asyncio.to_thread(extract_full_text_with_pymupdf, pdf_path)


def text_matches_regex_keywords(text: str) -> bool:
    """全文是否命中 `REGEX_KEYWORDS` 中任一关键词（字面子串，re 引擎）。"""
    if not text or not text.strip():
        return False
    return _KEYWORD_PATTERN.search(text) is not None


def _safe_pdf_filename(arxiv_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in arxiv_id)
    return (safe or "paper")[:200] + ".pdf"


async def download_pdf_async(
    pdf_url: str,
    save_path: str | Path,
    *,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """异步下载 PDF 到 `save_path`（自动创建父目录；可复用外部 session）。"""
    if not pdf_url.strip():
        raise ValueError("pdf_url is empty")

    dest = Path(save_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    async def _fetch_and_write(client: aiohttp.ClientSession) -> None:
        async with client.get(pdf_url, allow_redirects=True) as resp:
            resp.raise_for_status()
            body = await resp.read()
        dest.write_bytes(body)

    if session is not None:
        await _fetch_and_write(session)
        return

    ssl_ctx = build_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OpenBMB-PaperDiscovery/3; +local-funnel)",
        "Accept": "application/pdf,*/*",
    }
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers=headers,
    ) as client:
        await _fetch_and_write(client)


async def apply_regex_funnel(
    arxiv_id: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """
    对单条记录执行漏斗 1：下载 PDF → 全文 → Regex。

    - 命中任一 `REGEX_KEYWORDS`：保留 `data/temp_pdfs/` 下 PDF，返回 True（供后续 LLM）。
    - 未命中：删除 PDF，数据库 `status=REJECTED_BY_REGEX`，返回 False。
    - `pdf_url` 为空且无法推断可下载 PDF：不写临时文件，``status=NO_PDF``，返回 False（不再占用 PENDING 队列）。
    - 有 URL 但下载失败或 PDF 无法解析：``status=PDF_UNREACHABLE``，返回 False（避免 PENDING 死循环重试）。
    """
    paper = Paper.get_or_none(Paper.arxiv_id == arxiv_id)
    if paper is None:
        raise ValueError(f"No paper with arxiv_id={arxiv_id!r}")
    if paper.status != "PENDING":
        raise ValueError(
            f"apply_regex_funnel expects status=PENDING, got {paper.status!r} for {arxiv_id!r}"
        )

    pdf_url = resolvable_open_pdf_url(arxiv_id, paper.pdf_url)
    if not pdf_url:
        logger.debug("apply_regex_funnel: 无可用 pdf_url，标记 NO_PDF: %s", arxiv_id)
        with db.atomic():
            Paper.update(status="NO_PDF").where(Paper.arxiv_id == arxiv_id).execute()
        return False

    TEMP_PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = TEMP_PDF_DIR / _safe_pdf_filename(arxiv_id)

    try:
        await download_pdf_async(pdf_url, pdf_path, session=session)
        text = await extract_full_text_with_pymupdf_async(pdf_path)
    except Exception as e:
        if pdf_path.exists():
            try:
                pdf_path.unlink()
            except OSError:
                pass
        msg = str(e).strip().replace("\n", " ")[:200]
        logger.debug(
            "apply_regex_funnel: PDF 下载/解析失败 → PDF_UNREACHABLE: %s (%s%s)",
            arxiv_id,
            type(e).__name__,
            f": {msg}" if msg else "",
        )
        with db.atomic():
            Paper.update(status="PDF_UNREACHABLE").where(Paper.arxiv_id == arxiv_id).execute()
        return False

    if text_matches_regex_keywords(text):
        return True

    try:
        os.remove(pdf_path)
    except OSError as e:
        logger.warning("Could not remove rejected PDF %s: %s", pdf_path, e)

    with db.atomic():
        Paper.update(status="REJECTED_BY_REGEX").where(Paper.arxiv_id == arxiv_id).execute()
    return False


def run_regex_funnel_sync(arxiv_id: str) -> bool:
    """同步封装，便于脚本 / REPL 测试。"""
    import asyncio

    return asyncio.run(apply_regex_funnel(arxiv_id))
