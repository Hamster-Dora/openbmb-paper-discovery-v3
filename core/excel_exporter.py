"""
Excel 线索导出（PRD 3.4 / TODO 阶段四）：SUCCESS + 深度分阈值 + 合法邮箱 + LLM 判定生态相关。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import argparse
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Final

import pandas as pd

from database.models import Paper, init_db

logger = logging.getLogger(__name__)

# 正文 / 多行邮箱字段中至少包含一处常见格式的邮箱（PRD「格式合法」的工程近似）
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}\b"
)


def _default_min_score() -> int:
    return int(os.getenv("EXCEL_EXPORT_MIN_SCORE", "3"))


def _downloads_dir() -> Path:
    d = Path(os.path.expanduser("~/Downloads")).resolve()
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
    return d


def text_contains_valid_email(text: str | None) -> bool:
    """判断字符串中是否出现至少一个形如 user@domain.tld 的邮箱片段。"""
    if not text or not str(text).strip():
        return False
    return _EMAIL_PATTERN.search(text) is not None


def is_ecosystem_relevant_row(core_product: str | None) -> bool:
    """解析 ``core_product`` JSON，要求 ``ecosystem_relevant is True``（PRD：经 LLM 判定为相关）。"""
    if not core_product or not str(core_product).strip():
        return False
    try:
        data = json.loads(core_product)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("ecosystem_relevant"))


def fetch_export_rows(*, min_score: int, only_unexported: bool = False) -> list[dict[str, Any]]:
    """
    从 SQLite 读取满足 PRD / TODO 条件的论文，转为扁平 dict 列表（供 DataFrame）。

    :param only_unexported: 为 True 时仅尚未标记已导出（clue_exported≠1）的行（增量建联导出）。
    """
    # 与 launchd/入口脚本导入顺序无关，保证已执行「追加 clue_exported 列」等迁移
    init_db()
    cond = (
        (Paper.status == "SUCCESS")
        & (Paper.ai_score.is_null(False))
        & (Paper.ai_score >= min_score)
    )
    if only_unexported:
        # 必须用 Peewee 字段，勿手写 ``papers.clue_exported``：查询里表别名为 t1 时 SQLite 会报
        # no such column: papers.clue_exported
        not_marked = (Paper.clue_exported.is_null(True)) | (Paper.clue_exported != 1)
        cond = cond & not_marked
    q = Paper.select().where(cond).order_by(Paper.ai_score.desc(), Paper.arxiv_id)
    rows: list[dict[str, Any]] = []
    for p in q:
        if not text_contains_valid_email(p.author_email):
            continue
        if not is_ecosystem_relevant_row(p.core_product):
            continue
        extra: dict[str, Any] = {}
        if p.core_product:
            try:
                extra = json.loads(p.core_product)
            except json.JSONDecodeError:
                extra = {}
        rel = extra.get("relationship", "")
        products = extra.get("related_products") or []
        if isinstance(products, list):
            products_str = "; ".join(str(x) for x in products)
        else:
            products_str = str(products)
        rows.append(
            {
                "arxiv_id": p.arxiv_id,
                "publication_date": (p.publication_date or "").strip(),
                "title": p.title,
                "authors": p.authors or "",
                "abs_url": p.abs_url or "",
                "pdf_url": p.pdf_url or "",
                "ai_score": int(p.ai_score),
                "author_email": (p.author_email or "").strip(),
                "related_products": products_str,
                "relationship": rel,
                "is_chinese_team": extra.get("is_chinese_team"),
                "reasoning": extra.get("reasoning") or "",
            }
        )
    return rows


def _mark_clue_rows_exported(arxiv_ids: list[str]) -> None:
    if not arxiv_ids:
        return
    init_db()
    Paper.update(clue_exported=1).where(Paper.arxiv_id.in_(arxiv_ids)).execute()  # type: ignore[arg-type]


def export_clues_excel(
    *,
    min_score: int | None = None,
    output_dir: Path | str | None = None,
    filename_prefix: str = "openbmb_clues",
    only_unexported: bool = False,
    mark_exported: bool = True,
) -> tuple[Path | None, int]:
    """
    导出 xlsx 至用户下载目录（或 ``output_dir``）。

    :param min_score: ``ai_score`` 下限（含）；默认读环境变量 ``EXCEL_EXPORT_MIN_SCORE`` 或 3。
    :param only_unexported: 仅导出尚未记为已导出的线索行；无新行时返回 ``(None, 0)``（不写空表）。
    :param mark_exported: 写盘成功后，将本文件中的 ``arxiv_id`` 置 ``clue_exported=True``。
    :return: ``(xlsx 路径或 None, 行数)``。
    """
    ms = _default_min_score() if min_score is None else int(min_score)
    base = Path(output_dir).resolve() if output_dir else _downloads_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = base / f"{filename_prefix}_{stamp}.xlsx"

    records = fetch_export_rows(min_score=ms, only_unexported=only_unexported)
    if not records:
        n_score_ok = (
            Paper.select()
            .where(
                (Paper.status == "SUCCESS")
                & (Paper.ai_score.is_null(False))
                & (Paper.ai_score >= ms)
            )
            .count()
        )
        if only_unexported:
            logger.info(
                "增量导出 0 行：无未导出的新线索，跳过写 Excel（避免与上次全量重复）",
            )
            return None, 0
        logger.info(
            "导出 0 行：满足 SUCCESS 且 ai_score≥%s 的共 %s 篇；Excel 还要求 author_email 含合法邮箱且 "
            "core_product.ecosystem_relevant=true（见 fetch_export_rows）。",
            ms,
            n_score_ok,
        )
    columns = [
        "arxiv_id",
        "publication_date",
        "title",
        "authors",
        "abs_url",
        "pdf_url",
        "ai_score",
        "author_email",
        "related_products",
        "relationship",
        "is_chinese_team",
        "reasoning",
    ]
    df = pd.DataFrame.from_records(records, columns=columns)
    df.to_excel(out_path, index=False, engine="openpyxl")
    if mark_exported and records:
        ids = [str(r["arxiv_id"]) for r in records]
        _mark_clue_rows_exported(ids)
    n = len(records)
    logger.info("已导出 %s 行 → %s", n, out_path)
    return out_path, n


def main() -> None:
    parser = argparse.ArgumentParser(description="Export SUCCESS clues to ~/Downloads Excel (PRD 3.4).")
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Minimum ai_score (inclusive). Default: EXCEL_EXPORT_MIN_SCORE or 3.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Override output directory (default: ~/Downloads).",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="仅导出尚未标记 clue_exported 的线索行；无新行则不写文件。",
    )
    parser.add_argument(
        "--no-mark-exported",
        action="store_true",
        help="写 Excel 后不把行标记为已导出（调试用，一般勿用）。",
    )
    parser.add_argument(
        "--mark-eligible-without-excel",
        action="store_true",
        help="不生成 xlsx，仅将当前符合导出条件的行全部标记为 clue_exported（用于升级前已手工导出过、避免首次增量再全量重复）。",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.mark_eligible_without_excel:
        ms = _default_min_score() if args.min_score is None else int(args.min_score)
        rec = fetch_export_rows(min_score=ms, only_unexported=False)
        _mark_clue_rows_exported([str(r["arxiv_id"]) for r in rec])
        logger.info("已标记 %s 条为 clue_exported（未写文件）", len(rec))
        print(f"marked {len(rec)} rows")
        return
    path, n = export_clues_excel(
        min_score=args.min_score,
        output_dir=args.output_dir or None,
        only_unexported=bool(args.incremental),
        mark_exported=not args.no_mark_exported,
    )
    if path is not None:
        print(path)
    else:
        print(f"(无新行，{n})")


if __name__ == "__main__":
    main()
