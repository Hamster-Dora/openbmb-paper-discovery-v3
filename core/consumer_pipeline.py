"""
漏斗 2 消费者：PENDING → 漏斗 1（Regex）→ 预算检查 → LLM Center Chat Completions → 入库 SUCCESS；临时 PDF 必删（PRD 3.2 / 3.3；TODO 阶段三）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import asyncio
import json
import logging
import os
from collections import Counter
from typing import Any, Final

# 多协程下串行化 Peewee 写，减轻 SQLite database is locked
_sqlite_write_lock = asyncio.Lock()

import aiohttp
import yaml

from config.settings import (
    CONSUMER_MAX_CONCURRENCY,
    LLM_CHAT_COMPLETIONS_URL,
    LLM_MODEL_ID,
    MODELBEST_API_KEY,
)
from core.local_funnel import (
    TEMP_PDF_DIR,
    _safe_pdf_filename,
    apply_regex_funnel,
    extract_full_text_with_pymupdf,
)
from database.models import Paper, db
from utils.budget_controller import BudgetLimitExceeded, check_budget, record_usage
from utils.ssl_context import build_ssl_context

logger = logging.getLogger(__name__)

_PROMPTS_PATH: Final[Path] = _PROJECT_ROOT / "config" / "prompts.yaml"

_RELATIONSHIPS: Final[frozenset[str]] = frozenset(
    ("citation_only", "experimental_compare", "core_component", "none")
)

_prompts_cache: dict[str, Any] | None = None


def _load_prompts() -> dict[str, Any]:
    global _prompts_cache
    if _prompts_cache is None:
        _prompts_cache = yaml.safe_load(_PROMPTS_PATH.read_text(encoding="utf-8"))
    return _prompts_cache


def _estimate_tokens(*parts: str) -> tuple[int, int]:
    """粗估 (prompt_tokens, completion_tokens)；completion 用环境变量上限作保守预留。"""
    joined = "\n\n".join(p for p in parts if p)
    prompt_guess = max(1024, len(joined) // 3)
    completion_guess = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "8192"))
    return prompt_guess, completion_guess


def _split_openai_usage(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    """返回 (可计费输入 tokens, 输出 tokens, 缓存命中 tokens)。"""
    if not usage:
        return 0, 0, 0
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    cached = 0
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        cached = int(ptd.get("cached_tokens") or 0)
    billable_in = max(0, pt - cached)
    return billable_in, ct, cached


def _parse_llm_json(raw: str, separator: str, expected_keys: list[str]) -> dict[str, Any]:
    if separator in raw:
        chunk = raw.split(separator, 1)[1].strip()
    else:
        chunk = raw.strip()
    start = chunk.find("{")
    if start < 0:
        raise ValueError("model output has no JSON object")
    obj, _end = json.JSONDecoder().raw_decode(chunk, start)
    if not isinstance(obj, dict):
        raise ValueError("JSON root must be object")
    if set(obj.keys()) != set(expected_keys):
        raise ValueError(f"JSON keys must be exactly {expected_keys}, got {sorted(obj.keys())}")
    rel = obj["relationship"]
    if rel not in _RELATIONSHIPS:
        raise ValueError(f"invalid relationship: {rel!r}")
    score = int(obj["usage_score"])
    if score < 1 or score > 5:
        raise ValueError(f"usage_score out of range: {score}")
    if not isinstance(obj["related_products"], list):
        raise ValueError("related_products must be array")
    if not isinstance(obj["emails"], list):
        raise ValueError("emails must be array")
    if not isinstance(obj["ecosystem_relevant"], bool):
        raise ValueError("ecosystem_relevant must be bool")
    if not isinstance(obj["is_chinese_team"], bool):
        raise ValueError("is_chinese_team must be bool")
    if not isinstance(obj["reasoning"], str):
        raise ValueError("reasoning must be string")
    return obj


async def _chat_completion(
    session: aiohttp.ClientSession,
    *,
    system_prompt: str,
    user_content: str,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": LLM_MODEL_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "8192")),
    }
    async with session.post(LLM_CHAT_COMPLETIONS_URL, json=payload) as resp:
        text = await resp.text()
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM non-JSON ({resp.status}): {text[:800]}") from e
        if resp.status >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status}: {body}")
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM no choices: {body}")
    msg = (choices[0].get("message") or {})
    content = msg.get("content")
    if not content or not isinstance(content, str):
        raise RuntimeError("LLM empty content")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return content, usage


async def _process_one(
    session: aiohttp.ClientSession,
    pdf_session: aiohttp.ClientSession,
    paper: Paper,
) -> str:
    """
    处理单条 PENDING。返回值用于统计；遇 ``BudgetLimitExceeded`` 向上抛出以中断批次。
    """
    arxiv_id = paper.arxiv_id
    try:
        passed = await apply_regex_funnel(arxiv_id, session=pdf_session)
    except Exception:
        logger.exception("漏斗 1 失败 %s", arxiv_id)
        return "funnel_error"

    try:
        row = Paper.get(Paper.arxiv_id == arxiv_id)
    except Paper.DoesNotExist:
        return "missing"

    if row.status == "REJECTED_BY_REGEX":
        return "regex_rejected"
    if row.status == "NO_PDF":
        return "no_pdf"
    if row.status == "PDF_UNREACHABLE":
        return "pdf_unreachable"
    if row.status != "PENDING":
        return "not_pending_after_funnel"

    pdf_path = TEMP_PDF_DIR / _safe_pdf_filename(arxiv_id)
    try:
        full_text = await asyncio.to_thread(extract_full_text_with_pymupdf, pdf_path)
        prompts = _load_prompts()
        system_prompt = prompts["system_prompt"]
        sep = prompts["json_block_separator"]
        expected_keys = list(prompts["expected_response_keys"])

        user_content = f"论文标题：{row.title}\n\n正文（由 PDF 抽取）：\n{full_text}"

        est_in, est_out = _estimate_tokens(system_prompt, user_content)
        check_budget(estimated_input_tokens=est_in, estimated_output_tokens=est_out)

        raw_reply, usage = await _chat_completion(
            session,
            system_prompt=system_prompt,
            user_content=user_content,
        )
        data = _parse_llm_json(raw_reply, sep, expected_keys)

        core_payload = {
            "related_products": data["related_products"],
            "relationship": data["relationship"],
            "ecosystem_relevant": data["ecosystem_relevant"],
            "is_chinese_team": data["is_chinese_team"],
            "reasoning": data["reasoning"],
        }
        emails_joined = "\n".join(str(e) for e in data["emails"])

        async with _sqlite_write_lock:
            with db.atomic():
                Paper.update(
                    ai_score=int(data["usage_score"]),
                    author_email=emails_joined or None,
                    core_product=json.dumps(core_payload, ensure_ascii=False),
                    status="SUCCESS",
                ).where(Paper.arxiv_id == arxiv_id).execute()

        inp, outp, cache = _split_openai_usage(usage)
        record_usage(input_tokens=inp, output_tokens=outp, cache_tokens=cache)
        logger.info("LLM 完成 %s usage_score=%s", arxiv_id, data["usage_score"])
        return "success"
    except BudgetLimitExceeded:
        logger.warning("预算熔断，保留 PENDING：%s", arxiv_id)
        raise
    except Exception:
        logger.exception("LLM 或解析失败 %s", arxiv_id)
        async with _sqlite_write_lock:
            with db.atomic():
                Paper.update(status="LLM_ERROR").where(Paper.arxiv_id == arxiv_id).execute()
        return "llm_error"
    finally:
        if pdf_path.exists():
            try:
                os.remove(pdf_path)
            except OSError as e:
                logger.warning("未能删除临时 PDF %s: %s", pdf_path, e)


async def consume_pending_concurrent(
    limit: int = 32,
    *,
    concurrency: int = 4,
) -> dict[str, int]:
    """
    拉取最多 ``limit`` 条 ``status=PENDING``，以 ``concurrency`` 路协程并发处理（I/O 与 LLM 非阻塞）。

    实际并发不超过 ``config.settings.CONSUMER_MAX_CONCURRENCY``（环境变量 ``CONSUMER_MAX_CONCURRENCY``）。

    ``BudgetLimitExceeded`` 在单条任务内转为统计项 ``budget_hold_stop``（并发场景下不中断已调度任务）。
    """
    if not MODELBEST_API_KEY:
        raise ValueError("MODELBEST_API_KEY 为空，请在 `.env` 配置。")

    if not _PROMPTS_PATH.is_file():
        raise FileNotFoundError(f"缺少 prompts 配置: {_PROMPTS_PATH}")

    concurrency = max(1, min(CONSUMER_MAX_CONCURRENCY, int(concurrency)))

    papers = list(Paper.select().where(Paper.status == "PENDING").limit(limit))
    if not papers:
        return {}

    try:
        db.execute_sql("PRAGMA journal_mode=WAL;")
    except Exception:
        logger.debug("PRAGMA journal_mode WAL skipped", exc_info=True)

    sem = asyncio.Semaphore(max(1, concurrency))
    stats: Counter[str] = Counter()

    timeout = aiohttp.ClientTimeout(total=600)
    pdf_timeout = aiohttp.ClientTimeout(total=300)
    ssl_ctx = build_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=max(8, concurrency * 2))
    pdf_connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=max(8, concurrency * 2))
    headers = {
        "Authorization": f"Bearer {MODELBEST_API_KEY}",
        "Content-Type": "application/json",
    }
    pdf_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OpenBMB-PaperDiscovery/3; +local-funnel)",
        "Accept": "application/pdf,*/*",
    }
    async with aiohttp.ClientSession(
        timeout=pdf_timeout,
        connector=pdf_connector,
        headers=pdf_headers,
    ) as pdf_session, aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=headers,
    ) as session:

        async def run_guarded(paper: Paper) -> str:
            async with sem:
                try:
                    return await _process_one(session, pdf_session, paper)
                except BudgetLimitExceeded:
                    return "budget_hold_stop"

        results = await asyncio.gather(*(run_guarded(p) for p in papers), return_exceptions=True)

    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, Exception):
            raise r
        if isinstance(r, Exception):
            logger.exception("consumer 协程异常: %s", r)
            stats["task_error"] += 1
        else:
            stats[str(r)] += 1

    return dict(stats)


async def consume_pending(limit: int = 8, concurrency: int | None = None) -> dict[str, int]:
    """
    拉取最多 ``limit`` 条 ``status=PENDING`` 并处理。

    ``concurrency`` 默认读 ``CONSUMER_CONCURRENCY`` 环境变量，未设置时为 **1**（与旧版串行一致）。
    """
    conc = int(os.getenv("CONSUMER_CONCURRENCY", "1")) if concurrency is None else int(concurrency)
    return await consume_pending_concurrent(limit=limit, concurrency=max(1, conc))


def run_consumer_sync(limit: int = 8, concurrency: int | None = None) -> dict[str, int]:
    """同步入口（脚本 / 定时任务）。"""
    return asyncio.run(consume_pending(limit, concurrency=concurrency))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run LLM consumer pipeline (funnel 1 + funnel 2).")
    parser.add_argument("--limit", type=int, default=1, help="Max PENDING papers to process this run.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Async concurrency, capped at {CONSUMER_MAX_CONCURRENCY} (env CONSUMER_MAX_CONCURRENCY; default: env CONSUMER_CONCURRENCY or 1).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        out = run_consumer_sync(args.limit, concurrency=args.concurrency)
    except Exception:
        logger.exception("consumer 退出失败")
        raise SystemExit(1) from None
    logger.info("consumer 统计: %s", out)


if __name__ == "__main__":
    main()
