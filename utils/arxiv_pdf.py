"""
ArXiv 页面与 PDF 直链：入库时优先使用 arxiv.org（摘要页 + PDF），便于阅读与下载。

当 S2 未返回 ``openAccessPdf.url`` 时，只要 ``externalIds.ArXiv`` 可规范化，即补 ``https://arxiv.org/pdf/{id}.pdf``
（含新版 ``YYMM.NNNNN`` 与旧版 ``cs.LG/0701515`` 等常见形式）。
"""

from __future__ import annotations

import re

# 新版 id：YYMM.NNNNN，可选版本后缀 v1、v2…
_MODERN_ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

# ArXiv id 中允许的字符（来自 S2 的 ArXiv 字段，保守放行）
_SAFE_ARXIV_ID = re.compile(r"^[A-Za-z0-9./+\-]+$")


def _normalize_arxiv_external_id(arxiv_id: str) -> str | None:
    """去掉 ``arxiv:`` 前缀；拒绝 ``s2:`` 占位与明显非法串。"""
    aid = (arxiv_id or "").strip()
    if aid.lower().startswith("arxiv:"):
        aid = aid[6:].lstrip()
    if not aid or aid.startswith("s2:"):
        return None
    if len(aid) > 200:
        return None
    if not _SAFE_ARXIV_ID.fullmatch(aid):
        return None
    return aid


def arxiv_abs_url_from_id(arxiv_id: str) -> str | None:
    """
    由 ArXiv 外部 id 生成 **摘要页** ``https://arxiv.org/abs/...``。

    无有效 id 时返回 ``None``（纯 S2 论文请继续用 Semantic Scholar 的 ``url``）。
    """
    aid = _normalize_arxiv_external_id(arxiv_id)
    if not aid:
        return None
    return f"https://arxiv.org/abs/{aid}"


def arxiv_pdf_url_from_id(arxiv_id: str) -> str | None:
    """
    由 ArXiv 外部 id 生成 **PDF** ``https://arxiv.org/pdf/....pdf``。

    新版与含 ``/`` 的旧版 id 均使用同一 URL 模式（与 arxiv.org 行为一致）。
    """
    aid = _normalize_arxiv_external_id(arxiv_id)
    if not aid:
        return None
    if _MODERN_ARXIV_ID.match(aid) or "/" in aid:
        return f"https://arxiv.org/pdf/{aid}.pdf"
    return None


def resolvable_open_pdf_url(arxiv_id: str, stored_pdf_url: str | None) -> str:
    """
    可下载的 PDF 直链（与 ``local_funnel.apply_regex_funnel`` 中判定一致）：

    1. 优先用已入库/待入库的 ``pdf_url``（含 S2 ``openAccessPdf`` 等）；
    2. 否则尝试 ``arxiv_pdf_url_from_id(arxiv_id)``（``s2:`` 占位时一般为空）。

    返回非空表示漏斗一可尝试下载；空串则对应 ``NO_PDF``（生产者可直接写入该状态，避免进 PENDING）。
    """
    u = (stored_pdf_url or "").strip()
    if not u:
        u = (arxiv_pdf_url_from_id(arxiv_id) or "").strip()
    return u
