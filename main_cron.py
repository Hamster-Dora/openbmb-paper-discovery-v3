#!/usr/bin/env python3
"""
双轨定时调度（PRD 3.1 / TODO 阶段四）。

- **常规轨**：每周一凌晨增量扫描「上一完整自然周（周一至周日）」的 `QUERY_TAGS`。
- **顶会轨**：每月 1 号凌晨（若该月在 `CONFERENCE_SCHEDULE` 中）对对应会议名做 `paper/search`，
  发表日期窗口为**触发日上一个自然月**（与「放榜后次月 1 号启动」对齐）。
- **消费唤醒**（PRD 3.3）：每日 01:00 调用与 `core.closed_loop` 相同的多轮逻辑，直至 **队列为空** 或 **预算熔断** / PENDING 不降。`CRON_CONSUMER_LIMIT` 为**每轮**处理上限（未设置时默认 64，旧版曾为 512），不是全日总量；并发由 `CONSUMER_CONCURRENCY` 控制（未设置时默认 8）。若配置了飞书：
  **预算熔断** 且仍有 `PENDING` → 发提醒；**队列为空** → 由 closed_loop 导出 Excel 并发「已清空」通知（与手动闭环一致）。

**运行方式（二选一）**

1. **长驻进程**（机器在触发时刻需在线；无需打开 IDE）::

       venv/bin/python main_cron.py

2. **系统 cron / launchd**：不设长驻，按时间表调用一次性任务::

       venv/bin/python main_cron.py --job regular
       venv/bin/python main_cron.py --job conference
       venv/bin/python main_cron.py --job consumer
       venv/bin/python main_cron.py --job reminder
       venv/bin/python main_cron.py --job all     # regular+conference+consumer（各自内部判日期）

   推荐 macOS launchd 配置：凌晨 01:00 ``--job all``；下午 18:00 ``--job reminder``。
   笔记本关机则该次触发不会执行；休眠唤醒后 launchd 会补执行。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 先行加载 DB 迁移，避免仅 import 子包时未执行 ``init_db``/``ALTER clue_exported``
import database.models  # noqa: F401

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import CONFERENCE_SCHEDULE, FEISHU_WEBHOOK_URL, QUERY_TAGS
from core.closed_loop import run_closed_loop_sync
from core.producer_crawler import run_fetch_and_store
from database.models import Paper
from main_manual import run_query_tags_backfill
from utils.feishu_webhook import send_lark_message

logger = logging.getLogger(__name__)


def _previous_completed_week(today: date) -> tuple[date, date]:
    """上一完整自然周：周一至周日（以 today 所在周为「本周」）。"""
    this_monday = today - timedelta(days=today.weekday())
    last_week_monday = this_monday - timedelta(days=7)
    last_week_sunday = this_monday - timedelta(days=1)
    return last_week_monday, last_week_sunday


def _previous_calendar_month(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, last_prev


def job_regular_producer(*, force: bool = False) -> None:
    today = date.today()
    if not force and today.weekday() != 0:
        logger.info("常规轨：非周一且未 force，跳过")
        return
    start, end = _previous_completed_week(today)
    logger.info("常规轨：QUERY_TAGS×%s，发表窗口 %s..%s", len(QUERY_TAGS), start, end)
    summary = run_query_tags_backfill(start, end, list(QUERY_TAGS))
    logger.info(
        "常规轨完成 total_fetched=%s total_rows_written=%s total_skipped_no_pdf=%s",
        summary["total_fetched"],
        summary["total_rows_written"],
        summary.get("total_skipped_no_pdf", 0),
    )


def job_conference_producer(*, force: bool = False) -> None:
    today = date.today()
    if not force and today.day != 1:
        logger.info("顶会轨：非每月 1 日且未 force，跳过")
        return
    confs = CONFERENCE_SCHEDULE.get(today.month, ())
    if not confs:
        logger.info("顶会轨：CONFERENCE_SCHEDULE 中无月份 %s 的配置，跳过", today.month)
        return
    start, end = _previous_calendar_month(today)
    for c in confs:
        logger.info("顶会轨：query=%r 发表窗口 %s..%s", c, start, end)
        r = run_fetch_and_store(start, end, c)
        logger.info(
            "  fetched=%s rows_written=%s skipped_no_pdf=%s",
            r["fetched"],
            r["rows_written"],
            r.get("skipped_no_pdf", 0),
        )


def job_consumer_wake() -> None:
    batch = int(os.getenv("CRON_CONSUMER_LIMIT", "64"))
    conc = int(os.getenv("CONSUMER_CONCURRENCY", "8"))
    feishu_on = os.getenv("CRON_CONSUMER_FEISHU", "1").strip().lower() not in ("0", "false", "no")
    feishu_intro = "[OpenBMB 论文挖掘] 定时消费唤醒：消费者队列已清空。"
    pending_before = Paper.select().where(Paper.status == "PENDING").count()
    logger.info(
        "消费唤醒开始：pending_before=%s concurrency=%s closed_loop（与 CLI 一致）batch_size=%s max_rounds=0",
        pending_before,
        conc,
        batch,
    )
    result = run_closed_loop_sync(
        batch_size=batch,
        concurrency=conc,
        max_rounds=0,
        notify_feishu=feishu_on,
        feishu_intro=feishu_intro,
    )
    logger.info("消费唤醒完成: %s", result)

    pending = int(result.get("pending_left", 0) or 0)
    stats = result.get("consumer_stats") or {}
    if not feishu_on:
        return
    if not FEISHU_WEBHOOK_URL.strip():
        logger.warning("消费唤醒：CRON_CONSUMER_FEISHU 开启但 FEISHU_WEBHOOK_URL 未配置，跳过飞书")
        return

    budget_hit = int(stats.get("budget_hold_stop", 0) or 0) > 0
    if pending > 0 and budget_hit:
        try:
            send_lark_message(
                "[OpenBMB 论文挖掘] 定时消费唤醒：本轮多轮 **预算熔断** 后仍有 "
                f"**{pending}** 篇 PENDING。\n"
                "可保持本机运行后再执行 `python -m core.closed_loop`；"
                "或待次日 01:00 定时任务继续；亦可在下一次常规/顶会爬取前再集中处理。\n"
                f"累计消费统计：{stats}"
            )
        except Exception:
            logger.exception("飞书：预算熔断提醒发送失败")


def _next_month_first(today: date) -> date:
    """下个月 1 号。"""
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def job_smart_reminder() -> None:
    """
    下午 18:00 智能提醒：判断今晚/周末是否需要保持电脑开机。

    触发条件（任满足一条即发飞书）：
    1. 周五 → 下周一凌晨有常规轨，周日晚别关机。
    2. 明天是某顶会月 1 号 → 今晚别关机。
    3. 下个月 1 号落在周末且有顶会 → 在其前一个周五提醒别关机。
    4. 仍有 PENDING（熔断积压） → 今晚别关机，凌晨会继续消化。
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    messages: list[str] = []
    pending = Paper.select().where(Paper.status == "PENDING").count()
    logger.info("智能提醒开始：today=%s pending=%s", today, pending)

    # 1) 周五 → 下周一常规轨 02:05
    if today.weekday() == 4:
        messages.append(
            "下周一凌晨 02:05 有**常规轨**爬取任务，请确保周日晚电脑不关机（或设为不休眠）。"
        )

    # 2) 明天是 1 号且该月有顶会配置
    if tomorrow.day == 1:
        confs = CONFERENCE_SCHEDULE.get(tomorrow.month, ())
        if confs:
            messages.append(
                f"明天（{tomorrow}）是 {tomorrow.month} 月 1 日，凌晨 03:05 将触发"
                f"**顶会轨**（{', '.join(confs)}），请今晚保持电脑开机。"
            )

    # 3) 下个月 1 号在周末且有顶会 → 提前到其前一个周五提醒
    nmf = _next_month_first(today)
    confs_next = CONFERENCE_SCHEDULE.get(nmf.month, ())
    if confs_next and nmf.weekday() in (5, 6):
        days_to_fri = (nmf.weekday() - 4) % 7 or 7
        friday_before = nmf - timedelta(days=days_to_fri)
        if today == friday_before:
            messages.append(
                f"下个月 1 号（{nmf}，{_WEEKDAY_CN[nmf.weekday()]}）凌晨有**顶会轨**"
                f"（{', '.join(confs_next)}），因 1 号在周末，请本周五晚起保持电脑开机。"
            )

    # 4) 仍有 PENDING（预算熔断积压）
    if pending > 0:
        messages.append(
            f"当前仍有 **{pending}** 篇 PENDING 待消化（可能因预算熔断积压），"
            "请今晚保持电脑开机，凌晨 01:00 将自动继续消费。"
        )

    if not messages:
        logger.info("智能提醒：今日无需特别提醒，不发飞书")
        return

    if not FEISHU_WEBHOOK_URL.strip():
        logger.warning("智能提醒：有 %s 条消息但 FEISHU_WEBHOOK_URL 未配置", len(messages))
        for m in messages:
            logger.info("  → %s", m)
        return

    body = "[OpenBMB 论文挖掘] 今日提醒：\n\n" + "\n\n".join(f"• {m}" for m in messages)
    try:
        logger.info("智能提醒命中 %s 条：%s", len(messages), " | ".join(messages))
        send_lark_message(body)
        logger.info("智能提醒已发送飞书（%s 条）", len(messages))
    except Exception:
        logger.exception("智能提醒飞书发送失败")


def _add_jobs(scheduler: BlockingScheduler) -> None:
    # PRD：每周一凌晨 — 用 02:00 避开整点拥塞
    scheduler.add_job(
        job_regular_producer,
        CronTrigger(day_of_week="mon", hour=2, minute=5),
        id="regular_producer",
        replace_existing=True,
    )
    # 每月 1 号：由 job 内部判断是否在 CONFERENCE_SCHEDULE
    def _scheduled_conference() -> None:
        job_conference_producer(force=False)

    scheduler.add_job(
        _scheduled_conference,
        CronTrigger(day=1, hour=3, minute=5),
        id="conference_producer",
        replace_existing=True,
    )
    # PRD 3.3：每日 01:00
    scheduler.add_job(
        job_consumer_wake,
        CronTrigger(hour=1, minute=0),
        id="consumer_wake",
        replace_existing=True,
    )
    # 智能提醒：每日 18:00
    scheduler.add_job(
        job_smart_reminder,
        CronTrigger(hour=18, minute=0),
        id="smart_reminder",
        replace_existing=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PRD 3.1 dual-track scheduler + daily consumer wake.")
    parser.add_argument(
        "--job",
        choices=("regular", "conference", "consumer", "reminder", "all"),
        default="",
        help="Run one job once and exit (for system cron). 'all' = regular+conference if applicable+consumer.",
    )
    parser.add_argument(
        "--force-regular",
        action="store_true",
        help="With --job regular/all: run regular crawl even if today is not Monday.",
    )
    parser.add_argument(
        "--force-conference",
        action="store_true",
        help="With --job conference/all: run conference crawl even if not the 1st (uses today's month in schedule).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.job:
        try:
            if args.job in ("regular", "all"):
                job_regular_producer(force=bool(args.force_regular))
            if args.job in ("conference", "all"):
                job_conference_producer(force=bool(args.force_conference))
            if args.job in ("consumer", "all"):
                job_consumer_wake()
            if args.job == "reminder":
                job_smart_reminder()
        except Exception:
            logger.exception("Cron job failed")
            return 1
        return 0

    logger.info(
        "调度器启动（BlockingScheduler）。常规轨=周一 02:05；顶会轨=每月 1 日 03:05；"
        "消费=每日 01:00；智能提醒=每日 18:00。"
        " 退出请 Ctrl+C。若不想长驻，请用系统计划任务调用 --job。"
    )
    scheduler = BlockingScheduler()
    _add_jobs(scheduler)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
