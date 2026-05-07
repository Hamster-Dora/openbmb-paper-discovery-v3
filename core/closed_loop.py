"""
联调闭环（PRD 3.4 / TODO 阶段四）：并发抽空 PENDING 消费者；**仅当**队列无剩余 PENDING 时导出 Excel 并飞书通知。

若本轮处理后 PENDING 数量未减少（常见：``funnel_error`` / ``llm_error`` 等仍占 PENDING），则停止 drain，**不**导出（避免死循环）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import CONSUMER_MAX_CONCURRENCY, FEISHU_WEBHOOK_URL
from core.consumer_pipeline import consume_pending_concurrent
from core.excel_exporter import _default_min_score, export_clues_excel
from database.models import Paper
from utils.feishu_webhook import send_lark_message

logger = logging.getLogger(__name__)


def export_excel_and_notify_sync(
    *,
    min_score: int | None = None,
    notify_feishu: bool = True,
    feishu_intro: str | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    """
    导出线索 Excel 并可选飞书（不检查 PENDING；由调用方保证队列已空或仅需导出当前库内 SUCCESS）。

    ``feishu_intro`` 为飞书首行说明；默认与 PRD 3.4 闭环文案一致。

    :param incremental: 为 True 时只导出 ``clue_exported==False`` 的新增线索；0 行则不写盘、不发飞书（避免重复全量）。
    """
    ms = _default_min_score() if min_score is None else int(min_score)
    path, row_count = export_clues_excel(
        min_score=ms, only_unexported=incremental, mark_exported=True
    )
    intro = feishu_intro or "[OpenBMB 论文挖掘] 消费者队列已清空（PRD 3.4）。"
    out: dict[str, Any] = {
        "excel_path": str(path) if path is not None else None,
        "export_row_count": row_count,
        "feishu_sent": False,
    }
    if path is None and incremental:
        out["skipped_no_new_clues"] = True
        logger.info("增量导出无新行，跳过飞书（clue 已全部 mark 或无符合条件的新增）")
        return out
    if not notify_feishu:
        return out
    if not FEISHU_WEBHOOK_URL.strip():
        logger.warning("FEISHU_WEBHOOK_URL 未配置，跳过飞书通知")
        return out
    text = (
        f"{intro}\n"
        f"线索 Excel：{path}\n"
        f"本批行数（评分≥{ms}、合法邮箱、ecosystem_relevant"
        f"{'，仅未导出' if incremental else '，全量'}）：{row_count}"
    )
    try:
        send_lark_message(text)
        out["feishu_sent"] = True
    except Exception as e:
        logger.exception("飞书 Webhook 发送失败")
        out["feishu_error"] = str(e)
    return out


async def run_closed_loop_async(
    *,
    batch_size: int | None = None,
    concurrency: int | None = None,
    export_min_score: int | None = None,
    notify_feishu: bool = True,
    max_rounds: int | None = None,
    feishu_intro: str | None = None,
) -> dict[str, Any]:
    """
    多轮调用 ``consume_pending_concurrent`` 直至：

    - ``PENDING`` 为 0；或
    - 本轮后 ``PENDING`` 数量未变（无法继续消化）；或
    - 出现 ``budget_hold_stop``；或
    - 达到 ``max_rounds``（环境变量 ``CLOSED_LOOP_MAX_ROUNDS``：默认 1_000_000；``0`` 或负数表示**不限制**轮数，仅由上述条件结束）。

    仅当最终 ``PENDING==0`` 时：``export_clues_excel`` +（可选）``send_lark_message``。

    :param feishu_intro: 队列清空时飞书首行；``None`` 用默认 PRD 文案（``main_cron`` 定时任务可传入专用说明）。
    """
    batch = int(os.getenv("CLOSED_LOOP_BATCH_SIZE", "32")) if batch_size is None else int(batch_size)
    conc_raw = int(os.getenv("CONSUMER_CONCURRENCY", "8")) if concurrency is None else int(concurrency)
    conc = max(1, min(CONSUMER_MAX_CONCURRENCY, conc_raw))
    ms = _default_min_score() if export_min_score is None else int(export_min_score)
    if max_rounds is None:
        _mr = os.getenv("CLOSED_LOOP_MAX_ROUNDS", "1000000").strip()
        cap = int(_mr) if _mr else 1_000_000
    else:
        cap = int(max_rounds)
    unlimited_rounds = cap <= 0

    total_stats: Counter[str] = Counter()
    rounds = 0

    while True:
        cur = Paper.select().where(Paper.status == "PENDING").count()
        if cur == 0:
            break

        batch_stats = await consume_pending_concurrent(limit=max(1, batch), concurrency=conc)
        total_stats.update(batch_stats)
        rounds += 1

        new_cur = Paper.select().where(Paper.status == "PENDING").count()
        logger.info(
            "closed_loop 第 %s 轮：PENDING %s → %s，本批 %s",
            rounds,
            cur,
            new_cur,
            dict(batch_stats),
        )
        if batch_stats.get("budget_hold_stop", 0):
            logger.warning("预算熔断，停止 drain；剩余 PENDING=%s", new_cur)
            break
        if new_cur == cur:
            logger.warning(
                "本轮后 PENDING 仍为 %s，无法继续减少（多为 funnel_error 或其它未改状态异常）。停止 drain；不触发「队列清空」导出。",
                cur,
            )
            break
        if not unlimited_rounds and rounds >= cap:
            logger.warning(
                "达到 max_rounds=%s，停止 drain；剩余 PENDING=%s。"
                "自动化大批量可设 CLOSED_LOOP_MAX_ROUNDS=0 不限制轮数（仍会在预算或 PENDING 不降时停止）。",
                cap,
                new_cur,
            )
            break

    pending_left = Paper.select().where(Paper.status == "PENDING").count()
    result: dict[str, Any] = {
        "pending_left": pending_left,
        "rounds": rounds,
        "consumer_stats": dict(total_stats),
        "concurrency_used": conc,
    }

    if pending_left == 0:
        ex = export_excel_and_notify_sync(
            min_score=ms,
            notify_feishu=notify_feishu,
            feishu_intro=feishu_intro,
        )
        result["excel_path"] = ex.get("excel_path")
        result["export_row_count"] = ex.get("export_row_count", 0)
        result["feishu_sent"] = ex.get("feishu_sent", False)
        if "feishu_error" in ex:
            result["feishu_error"] = ex["feishu_error"]
    else:
        result["excel_path"] = None
        result["export_row_count"] = 0
        result["feishu_sent"] = False

    return result


def run_closed_loop_sync(**kwargs: Any) -> dict[str, Any]:
    """同步入口（shell / cron 调用）。"""
    return asyncio.run(run_closed_loop_async(**kwargs))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drain PENDING with concurrent consumer; export + Feishu when queue empty (PRD 3.4).",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Papers per round (env CLOSED_LOOP_BATCH_SIZE).")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "Parallel consumer workers (env CONSUMER_CONCURRENCY; "
            f"capped at {CONSUMER_MAX_CONCURRENCY} via CONSUMER_MAX_CONCURRENCY)."
        ),
    )
    parser.add_argument("--min-score", type=int, default=None, help="Excel export min ai_score (inclusive).")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Max drain rounds (0=unlimited; default env CLOSED_LOOP_MAX_ROUNDS or 1000000).",
    )
    parser.add_argument("--no-feishu", action="store_true", help="Skip Feishu webhook.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = run_closed_loop_sync(
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        export_min_score=args.min_score,
        notify_feishu=not args.no_feishu,
        max_rounds=args.max_rounds,
    )
    logger.info("closed_loop 结果: %s", out)


if __name__ == "__main__":
    main()
