#!/usr/bin/env python3
"""
手动按日期回溯：对 `config.settings.QUERY_TAGS` 逐条调用 Semantic Scholar 生产者并入库。

- 与 PRD 3.1「常规搜索模式」使用相同的泛化标签复合打捞思路；触发方式为本脚本 + 显式起止日期（非 Cron）。
- **不**走顶会专项轨：顶会日历见 `CONFERENCE_SCHEDULE`，由阶段四 `main_cron.py` 负责。
- 每周一凌晨增量扫描前一周等 **定时** 行为同样归属 `main_cron.py`（常规轨），本脚本仅用于人工指定区间补捞。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from typing import Any, Final

from config.settings import QUERY_TAGS
from core.producer_crawler import run_fetch_and_store

logger = logging.getLogger(__name__)

_DATE_FMT: Final[str] = "%Y-%m-%d"


def _parse_cli_date(s: str) -> date:
    return datetime.strptime(s.strip(), _DATE_FMT).date()


def run_query_tags_backfill(
    start_date: date | str,
    end_date: date | str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    在 [start_date, end_date]（含）内，对 `tags` 中每条查询执行 `run_fetch_and_store`。

    ``tags`` 为 ``None`` 时使用配置中的全部 ``QUERY_TAGS``。
    返回聚合统计（``per_query`` 含各查询的 fetched / rows_written / skipped_no_pdf）。
    """
    queries = list(tags) if tags is not None else list(QUERY_TAGS)
    if not queries:
        raise ValueError("tags must be non-empty")

    per_query: list[dict[str, Any]] = []
    total_fetched = 0
    total_rows_written = 0
    total_skipped_no_pdf = 0

    for i, query in enumerate(queries, 1):
        logger.info("[%s/%s] query=%r", i, len(queries), query)
        r = run_fetch_and_store(start_date, end_date, query)
        fetched = int(r["fetched"])
        rows_written = int(r["rows_written"])
        skipped = int(r.get("skipped_no_pdf", 0))
        per_query.append(
            {
                "query": query,
                "fetched": fetched,
                "rows_written": rows_written,
                "skipped_no_pdf": skipped,
            }
        )
        total_fetched += fetched
        total_rows_written += rows_written
        total_skipped_no_pdf += skipped
        logger.info(
            "  fetched=%s rows_written=%s skipped_no_pdf=%s",
            fetched,
            rows_written,
            skipped,
        )

    return {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "queries_run": len(queries),
        "total_fetched": total_fetched,
        "total_rows_written": total_rows_written,
        "total_skipped_no_pdf": total_skipped_no_pdf,
        "per_query": per_query,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manual backfill: run producer over QUERY_TAGS for each day in [start_date, end_date].",
    )
    parser.add_argument(
        "--start_date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Inclusive start publication date (Semantic Scholar publicationDateOrYear).",
    )
    parser.add_argument(
        "--end_date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Inclusive end publication date.",
    )
    parser.add_argument(
        "--tags",
        default="",
        help=(
            "Optional comma-separated search queries; default: all QUERY_TAGS from config. "
            "Use a single tag for small tests (saves API quota)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        start = _parse_cli_date(args.start_date)
        end = _parse_cli_date(args.end_date)
    except ValueError as e:
        logger.error("Invalid date (use %s): %s", _DATE_FMT, e)
        return 2

    if start > end:
        logger.error("start_date must be <= end_date")
        return 2

    raw = (args.tags or "").strip()
    tag_list = [t.strip() for t in raw.split(",") if t.strip()] if raw else None

    try:
        summary = run_query_tags_backfill(start, end, tag_list)
    except ValueError as e:
        logger.error("%s", e)
        return 2
    except Exception:
        logger.exception("Backfill failed")
        return 1

    logger.info(
        "Done. queries_run=%s total_fetched=%s total_rows_written=%s total_skipped_no_pdf=%s",
        summary["queries_run"],
        summary["total_fetched"],
        summary["total_rows_written"],
        summary.get("total_skipped_no_pdf", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
