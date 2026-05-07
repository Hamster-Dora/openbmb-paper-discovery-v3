"""
日预算熔断：按自然日累计 Token → 人民币（PRD 3.3 / TODO 阶段三）。

定价（公司模型，可通过环境变量覆盖）：
- 输入 ¥/K、输出 ¥/K、缓存命中 ¥/K。
默认日上限见 ``BUDGET_DAILY_LIMIT_CNY``（逼近 33–34 元时拒绝继续请求）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path
from typing import Any, Final

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH: Final[Path] = _PROJECT_ROOT / "data" / "budget_usage.json"
# 并发消费者下保护 ``budget_usage.json`` 读写的互斥锁
_STATE_LOCK: Final[threading.Lock] = threading.Lock()

# 公司定价（元 / 1K tokens）
_INPUT_CNY_PER_1K: Final[float] = float(os.getenv("LLM_PRICE_INPUT_CNY_PER_1K", "0.0040"))
_OUTPUT_CNY_PER_1K: Final[float] = float(os.getenv("LLM_PRICE_OUTPUT_CNY_PER_1K", "0.0240"))
_CACHE_CNY_PER_1K: Final[float] = float(os.getenv("LLM_PRICE_CACHE_CNY_PER_1K", "0.0040"))

# PRD：当日消费逼近 33–34 元熔断；默认取 33，可用环境变量调高至 34 等
_DAILY_LIMIT_CNY: Final[float] = float(os.getenv("BUDGET_DAILY_LIMIT_CNY", "33"))


class BudgetLimitExceeded(Exception):
    """本自然日累计费用（含本次预估）已达到或超过日上限。"""

    def __init__(self, message: str, *, spent_cny: float, limit_cny: float, pending_cny: float) -> None:
        super().__init__(message)
        self.spent_cny = spent_cny
        self.limit_cny = limit_cny
        self.pending_cny = pending_cny


def _today_iso() -> str:
    return date.today().isoformat()


def _cost_cny(input_tokens: int, output_tokens: int, cache_tokens: int) -> float:
    return (
        max(0, input_tokens) / 1000.0 * _INPUT_CNY_PER_1K
        + max(0, output_tokens) / 1000.0 * _OUTPUT_CNY_PER_1K
        + max(0, cache_tokens) / 1000.0 * _CACHE_CNY_PER_1K
    )


def _load_or_reset_state() -> dict[str, Any]:
    today = _today_iso()
    if not _STATE_PATH.is_file():
        return {"date": today, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"date": today, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}
    if raw.get("date") != today:
        return {"date": today, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}
    return {
        "date": today,
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "cache_tokens": int(raw.get("cache_tokens") or 0),
    }


def _save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(_STATE_PATH)


def today_spent_cny() -> float:
    """当前自然日已入账的 Token 费用（元），不含尚未 ``record_usage`` 的调用。"""
    with _STATE_LOCK:
        s = _load_or_reset_state()
        return _cost_cny(s["input_tokens"], s["output_tokens"], s["cache_tokens"])


def record_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_tokens: int = 0,
) -> None:
    """
    在一次 Chat Completions 返回后写入用量（通常解析 usage 字段后调用）。
    新自然日会自动重置计数。
    """
    with _STATE_LOCK:
        s = _load_or_reset_state()
        s["input_tokens"] = int(s["input_tokens"]) + max(0, input_tokens)
        s["output_tokens"] = int(s["output_tokens"]) + max(0, output_tokens)
        s["cache_tokens"] = int(s["cache_tokens"]) + max(0, cache_tokens)
        _save_state(s)


def check_budget(
    *,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    estimated_cache_tokens: int = 0,
) -> None:
    """
    在发起 LLM 请求**前**调用：若「已消费 + 本次预估」达到或超过日上限，抛出 ``BudgetLimitExceeded``。

    ``estimated_*`` 可对齐为：本请求预计的 prompt（含缓存命中部分若单独计价）、预计 completion tokens。
    """
    pending = _cost_cny(estimated_input_tokens, estimated_output_tokens, estimated_cache_tokens)
    with _STATE_LOCK:
        s = _load_or_reset_state()
        spent = _cost_cny(s["input_tokens"], s["output_tokens"], s["cache_tokens"])
        if spent + pending >= _DAILY_LIMIT_CNY:
            raise BudgetLimitExceeded(
                f"日预算已达上限：已消费 ¥{spent:.4f}，本次预估 ¥{pending:.4f}，"
                f"合计 ≥ 限额 ¥{_DAILY_LIMIT_CNY:.4f}（可在环境变量 BUDGET_DAILY_LIMIT_CNY 调整）。",
                spent_cny=spent,
                limit_cny=_DAILY_LIMIT_CNY,
                pending_cny=pending,
            )
