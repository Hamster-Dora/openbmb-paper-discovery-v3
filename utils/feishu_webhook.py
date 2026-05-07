"""飞书群机器人 Webhook：文本消息（PRD 3.4 导出完成后通知）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from config.settings import FEISHU_WEBHOOK_URL
from utils.ssl_context import build_ssl_context


def _text_payload(text_content: str) -> dict[str, Any]:
    """飞书自定义机器人 — `msg_type: text` 标准 JSON body。"""
    return {"msg_type": "text", "content": {"text": text_content}}


def send_lark_message(text_content: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
    """
    向 `FEISHU_WEBHOOK_URL` 发送一条文本消息。

    返回响应 JSON（若 body 为空则返回空 dict）。未配置 Webhook 时抛出 `ValueError`。
    """
    url = FEISHU_WEBHOOK_URL.strip()
    if not url:
        raise ValueError("FEISHU_WEBHOOK_URL is empty; set it in `.env`.")

    payload = _text_payload(text_content)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    ctx = build_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Feishu webhook HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Feishu webhook request failed: {e}") from e

    if not raw.strip():
        return {}
    return json.loads(raw)
