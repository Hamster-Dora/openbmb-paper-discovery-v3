"""
Semantic Scholar 生产者：按查询 + 日期范围拉取元数据，时间切片（PRD 1000 条/策略上限）与分页。

- 全量收录 S2 结果：``arxiv_id`` 优先为 ``externalIds.ArXiv``，否则为 ``s2:{paperId}``。
- 有 ArXiv 时 ``abs_url`` / ``pdf_url`` 优先 arxiv.org；否则用 S2 论文页与 ``openAccessPdf``。
- 仅使用 ``paper/search`` 返回字段，不再逐篇请求 ``paper/{{paperId}}``（避免配额与耗时）。
- 若按与漏斗一相同规则仍**无**可下载 PDF URL，**不入库**（不占用库容；S2 非 ArXiv 的 ``openAccessPdf`` 有直链则照常写入）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Final

import aiohttp
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import SEMANTIC_SCHOLAR_KEY
from database.models import Paper, db
from utils.arxiv_pdf import arxiv_abs_url_from_id, arxiv_pdf_url_from_id, resolvable_open_pdf_url
from utils.ssl_context import build_ssl_context

logger = logging.getLogger(__name__)

S2_SEARCH_URL: Final[str] = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_PAGE_LIMIT: Final[int] = 100
S2_TOTAL_SPLIT_THRESHOLD: Final[int] = 1000
S2_MAX_RETRIEVABLE_PER_QUERY: Final[int] = 1000

_FIELDS: Final[str] = "paperId,title,authors,externalIds,url,openAccessPdf,publicationDate"
_INSERT_CHUNK: Final[int] = 80


def _parse_date(d: date | str) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d.strip(), "%Y-%m-%d").date()


def _clean_text(raw: Any, *, limit: int | None = None) -> str:
    """
    清洗外部返回字符串中的非法 Unicode（尤其是不完整 surrogate）。

    - 保留正常中文/emoji；
    - 对坏字符用 ``?`` 替换，避免在日志/SQLite 编码阶段整批崩溃。
    """
    if raw is None:
        text = ""
    elif isinstance(raw, str):
        text = raw
    else:
        text = str(raw)
    cleaned = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    return cleaned[:limit] if limit is not None else cleaned


def _normalize_publication_date(raw: Any) -> str | None:
    """S2 publicationDate 归一化为 YYYY-MM-DD；不可解析时返回 None。"""
    s = _clean_text(raw).strip()
    if not s:
        return None
    # 常见返回即 YYYY-MM-DD
    if len(s) >= 10:
        head = s[:10]
        try:
            datetime.strptime(head, "%Y-%m-%d")
            return head
        except ValueError:
            pass
    return None


def _paper_to_row(paper: dict[str, Any]) -> dict[str, Any] | None:
    """S2 单条 search/merge 结果 → 待入库行；无可用 PDF 直链时返回 ``None``（不写入库）。"""
    try:
        eid = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
        arxiv = _clean_text(eid.get("ArXiv")).strip()
        pid = _clean_text(paper.get("paperId")).strip()
        arxiv_id = arxiv if arxiv else (f"s2:{pid}" if pid else "s2:unknown")
        authors = paper.get("authors") or []
        names = [_clean_text(a.get("name")) for a in authors if isinstance(a, dict)]
        authors_s = json.dumps(names, ensure_ascii=False)
        oa = paper.get("openAccessPdf")
        pdf_url = ""
        if isinstance(oa, dict):
            pdf_url = _clean_text(oa.get("url")).strip()
        aid_key = _clean_text(arxiv_id, limit=256)
        if not pdf_url:
            pdf_url = _clean_text(arxiv_pdf_url_from_id(aid_key)).strip()
        if not pdf_url and arxiv:
            rt = arxiv.strip()
            if rt.lower().startswith("arxiv:"):
                rt = rt[6:].lstrip()
            if rt:
                pdf_url = f"https://arxiv.org/pdf/{rt}.pdf"
        abs_url = ""
        if arxiv:
            abs_url = _clean_text(arxiv_abs_url_from_id(arxiv)).strip()
            if not abs_url:
                tail = arxiv.strip()
                if tail.lower().startswith("arxiv:"):
                    tail = tail[6:].lstrip()
                if tail:
                    abs_url = f"https://arxiv.org/abs/{tail}"
        if not abs_url:
            abs_url = _clean_text(paper.get("url")).strip()
        st_pdf = _clean_text(pdf_url).strip()
        if not resolvable_open_pdf_url(aid_key, st_pdf):
            return None
        publication_date = _normalize_publication_date(paper.get("publicationDate"))
        return {
            "arxiv_id": aid_key,
            "title": _clean_text(paper.get("title"), limit=8192),
            "authors": _clean_text(authors_s),
            "abs_url": _clean_text(abs_url, limit=8192),
            "pdf_url": _clean_text(st_pdf, limit=8192) if st_pdf else "",
            "publication_date": publication_date,
            "status": "PENDING",
        }
    except Exception as e:
        pid = _clean_text(paper.get("paperId")).strip()
        logger.warning("skip malformed S2 row paperId=%r (%s)", pid or "unknown", e)
        return None


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in (408, 429) or exc.status >= 500
    return isinstance(
        exc,
        (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            aiohttp.ServerConnectionError,
        ),
    )


@retry(
    retry=retry_if_exception(_should_retry),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(8),
    reraise=True,
)
async def _request_search_page(
    session: aiohttp.ClientSession,
    *,
    query: str,
    publication_range: str,
    offset: int,
) -> dict[str, Any]:
    params = {
        "query": query,
        "offset": offset,
        "limit": S2_PAGE_LIMIT,
        "fields": _FIELDS,
        "publicationDateOrYear": publication_range,
    }
    async with session.get(S2_SEARCH_URL, params=params) as resp:
        if resp.status == 429:
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=429,
                message="Too Many Requests",
            )
        resp.raise_for_status()
        return await resp.json()


async def _paginate_from_first_page(
    session: aiohttp.ClientSession,
    query: str,
    publication_range: str,
    first_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = list(first_payload.get("data") or [])
    total = int(first_payload.get("total") or 0)
    cap = min(total, S2_MAX_RETRIEVABLE_PER_QUERY)
    if total > S2_MAX_RETRIEVABLE_PER_QUERY:
        logger.warning(
            "S2 paper/search caps at %s results per query; total reported=%s for "
            "query=%r publicationDateOrYear=%s — truncating.",
            S2_MAX_RETRIEVABLE_PER_QUERY,
            total,
            query,
            publication_range,
        )

    # Prefer server-provided pagination cursor (`next`) over `total`.
    # Semantic Scholar may report a larger total than currently retrievable and
    # return 400 for out-of-range offsets on later pages.
    next_offset = first_payload.get("next")
    if not isinstance(next_offset, int):
        next_offset = len(collected)

    while isinstance(next_offset, int) and next_offset < cap:
        offset = next_offset
        try:
            page = await _request_search_page(
                session,
                query=query,
                publication_range=publication_range,
                offset=offset,
            )
        except aiohttp.ClientResponseError as e:
            if e.status == 400:
                logger.warning(
                    "S2 returned 400 during pagination; stop at offset=%s "
                    "query=%r publicationDateOrYear=%s",
                    offset,
                    query,
                    publication_range,
                )
                break
            raise
        batch = page.get("data") or []
        if not batch:
            break
        collected.extend(batch)
        next_offset = page.get("next")
        if not isinstance(next_offset, int):
            next_offset = offset + len(batch)
        if len(batch) < S2_PAGE_LIMIT:
            break
    return collected


async def _fetch_papers_for_date_range(
    session: aiohttp.ClientSession,
    query: str,
    d_start: date,
    d_end: date,
) -> list[dict[str, Any]]:
    if d_start > d_end:
        return []

    pub = f"{d_start.isoformat()}:{d_end.isoformat()}"
    first = await _request_search_page(session, query=query, publication_range=pub, offset=0)
    total = int(first.get("total") or 0)
    if total == 0:
        return []

    if total > S2_TOTAL_SPLIT_THRESHOLD and d_start < d_end:
        mid = d_start + (d_end - d_start) // 2
        left = await _fetch_papers_for_date_range(session, query, d_start, mid)
        right = await _fetch_papers_for_date_range(session, query, mid + timedelta(days=1), d_end)
        return left + right

    return await _paginate_from_first_page(session, query, pub, first)


async def fetch_papers(
    start_date: date | str,
    end_date: date | str,
    query: str,
) -> list[dict[str, Any]]:
    """调用 Semantic Scholar ``paper/search`` 拉取论文列表（字段见 ``_FIELDS``）。"""
    if not SEMANTIC_SCHOLAR_KEY:
        raise ValueError("SEMANTIC_SCHOLAR_KEY is empty; set it in `.env`.")

    d0 = _parse_date(start_date)
    d1 = _parse_date(end_date)
    if d0 > d1:
        raise ValueError("start_date must be <= end_date")

    headers = {"x-api-key": SEMANTIC_SCHOLAR_KEY}
    timeout = aiohttp.ClientTimeout(total=120)
    ssl_ctx = build_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
        connector=connector,
    ) as session:
        return await _fetch_papers_for_date_range(session, query, d0, d1)


def insert_papers_ignore_conflict(
    papers: list[dict[str, Any]],
) -> dict[str, int]:
    """
    批量 ``INSERT OR IGNORE``。

    无可用 PDF 直链（与 ``resolvable_open_pdf_url``/漏斗一一致）的条目不插入。

    返回 ``rows_written``=参与插入的条数（与此前语义一致），``skipped_no_pdf``=被丢弃的条数。
    """
    if not papers:
        return {"rows_written": 0, "skipped_no_pdf": 0}
    rows: list[dict[str, Any]] = []
    skipped = 0
    for p in papers:
        row = _paper_to_row(p)
        if row is None:
            skipped += 1
        else:
            rows.append(row)
    if not rows:
        return {"rows_written": 0, "skipped_no_pdf": skipped}
    n = 0
    with db.atomic():
        for i in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[i : i + _INSERT_CHUNK]
            Paper.insert_many(chunk).on_conflict_ignore().execute()
            n += len(chunk)
    return {"rows_written": n, "skipped_no_pdf": skipped}


async def fetch_and_store(
    start_date: date | str,
    end_date: date | str,
    query: str,
) -> dict[str, Any]:
    papers = await fetch_papers(start_date, end_date, query)
    ins = insert_papers_ignore_conflict(papers)
    return {
        "fetched": len(papers),
        "rows_written": int(ins["rows_written"]),
        "skipped_no_pdf": int(ins["skipped_no_pdf"]),
    }


def run_fetch_and_store(
    start_date: date | str,
    end_date: date | str,
    query: str,
) -> dict[str, Any]:
    return asyncio.run(fetch_and_store(start_date, end_date, query))
