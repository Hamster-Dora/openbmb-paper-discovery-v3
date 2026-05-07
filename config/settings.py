"""Application configuration: env-backed secrets and static defaults (PRD / TODO)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Load `.env` from project root (same directory as this package's parent).
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _first_nonempty(*names: str) -> str:
    for name in names:
        v = os.getenv(name, "").strip()
        if v:
            return v
    return ""


def _env_int_bounded(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        v = default
    else:
        try:
            v = int(raw)
        except ValueError:
            v = default
    return max(lo, min(hi, v))


# 消费者协程上限（``closed_loop`` / ``consumer_pipeline`` 共用）。默认 64；可升至 512。
# 过高可能触发 LLM 网关限流或加剧 SQLite 锁等待。
CONSUMER_MAX_CONCURRENCY: Final[int] = _env_int_bounded("CONSUMER_MAX_CONCURRENCY", 64, 1, 512)


# Semantic Scholar API — apply at semanticscholar.org/product/api (not LLM Center).
SEMANTIC_SCHOLAR_KEY: Final[str] = os.getenv("SEMANTIC_SCHOLAR_KEY", "").strip()

# LLM Center: create in 个人中心 → 创建个人 Key（与官方 Google Gemini 无关，走公司网关）。
MODELBEST_API_KEY: Final[str] = _first_nonempty("MODELBEST_API_KEY", "GEMINI_API_KEY")
GEMINI_API_KEY: Final[str] = MODELBEST_API_KEY

# 公司文档「填写 API 地址」：外网 …/llm ，内网 …/llm（.co）。此为根路径，不含 /v1。
LLM_CENTER_API_ROOT: Final[str] = os.getenv(
    "LLM_CENTER_API_ROOT",
    "https://llm-center.ali.modelbest.cn/llm",
).strip().rstrip("/")

# OpenAI 兼容 Chat Completions；若平台调整路径，可用 LLM_CHAT_COMPLETIONS_URL 覆盖完整地址。
LLM_CHAT_COMPLETIONS_URL: Final[str] = (
    os.getenv("LLM_CHAT_COMPLETIONS_URL")
    or f"{LLM_CENTER_API_ROOT}/v1/chat/completions"
).strip()

# 模型 ID：LLMCenter 模型列表里复制「模型 ID」（如 gemini-3.1-pro-preview），不是展示名称。
LLM_MODEL_ID: Final[str] = _first_nonempty(
    "LLM_MODEL_ID",
    "GEMINI_MODEL_ID",
) or "gemini-3.1-pro-preview"
GEMINI_MODEL_ID: Final[str] = LLM_MODEL_ID

# 飞书群机器人 Webhook（开放平台创建机器人后拿到 URL）。
FEISHU_WEBHOOK_URL: Final[str] = os.getenv("FEISHU_WEBHOOK_URL", "").strip()

REGEX_KEYWORDS: Final[list[str]] = [
    "MiniCPM",
    "VoxCPM",
    "EdgeClaw",
    "UltraRAG",
    "ChatDev",
    "MiniCPM-o",
    "ModelBest",
    "OpenBMB",
    "MiniCPM-V",
]

# 泛化检索：`paper/search` 为全文关键词（非 arXiv 分区）。单 query 单日最多 1000 条，由 S2 相关性排序；
# 本地再用 REGEX_KEYWORDS 收紧。已去掉 ``cs.*`` / ``eess.*``（易误解成分区、且与全文检索重复）；
# 合并明显同义/缩写（如 RAG、PPO）以减少请求；主题词优先**完整短语**（如 Large Language Models），
# 单独 ``LLM`` 在 S2 全文检索里命中常偏少，故不单独占一条。
QUERY_TAGS: Final[list[str]] = [
    # 1 核心大模型与轻量化 (MiniCPM, ModelBest, OpenBMB)
    "Large Language Models",
    "Foundation Models",
    "Efficient LLM",
    "Small Language Models",
    "Parameter-Efficient Fine-Tuning",
    "PEFT",
    "LoRA",
    "Model Quantization",
    "Model Compression",
    # 2 语音、音频与全模态 (VoxCPM, MiniCPM-o)
    "Text-to-Speech",
    "TTS",
    "Voice Cloning",
    "Zero-shot Voice Cloning",
    "Spoken Dialogue Systems",
    "Audio Generation",
    "Omni-modal Models",
    "End-to-End Speech Models",
    # 3 多智能体与软件工程自动化 (ChatDev)
    "Agent",
    "Autonomous Agents",
    "Multi-Agent Systems",
    "Agentic Workflow",
    "LLM-based Software Engineering",
    "Automated Code Generation",
    "Role-playing Agents",
    "Collaborative AI",
    # 4 边缘计算与资源调度 (EdgeClaw)
    "Edge AI",
    "Edge Computing",
    "On-device AI",
    "Serverless Edge Computing",
    "Resource Scheduling",
    "Resource Allocation",
    "Reinforcement Learning",
    "Proximal Policy Optimization",
    # 5 检索增强与知识 (UltraRAG)
    "Retrieval-Augmented Generation",
    "RAG",
    "Information Retrieval",
    "Knowledge Graphs",
    "Vector Databases",
    "Semantic Search",
    # 6 多模态视觉 (MiniCPM-V)
    "Multimodal",
    "MLLM",
    "Vision-Language Models",
    "Visual Question Answering",
    "Image Understanding",
]

# On each month's 1st (key = month 1–12), run a crawl for the listed conferences
# (approx. release in prior month per PRD NFR; e.g. CVPR ~Feb -> crawl Mar 1).
CONFERENCE_SCHEDULE: Final[dict[int, tuple[str, ...]]] = {
    2: ("ICLR",),
    3: ("CVPR",),
    6: ("ACL",),
    8: ("ACM_MM",),
    9: ("EMNLP",),
    10: ("NeurIPS",),
}
