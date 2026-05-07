"""
HTTPS 证书校验：与 `urllib` / `aiohttp` 共用（公司代理解密、macOS Python 缺根证书等）。

环境变量（任选其一为 1/true/yes 即关闭校验，仅可信网络）：
  `HTTPS_INSECURE_SSL`、`FEISHU_WEBHOOK_INSECURE_SSL`、`SEMANTIC_SCHOLAR_INSECURE_SSL`

自定义 CA：`FEISHU_WEBHOOK_CA_BUNDLE`、`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`
"""

from __future__ import annotations

import os
import ssl
import warnings


def _insecure_enabled() -> bool:
    for key in (
        "HTTPS_INSECURE_SSL",
        "FEISHU_WEBHOOK_INSECURE_SSL",
        "SEMANTIC_SCHOLAR_INSECURE_SSL",
    ):
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def build_ssl_context() -> ssl.SSLContext:
    """供 `urllib.request.urlopen(..., context=...)` 与 `aiohttp.TCPConnector(ssl=...)` 使用。"""
    if _insecure_enabled():
        warnings.warn(
            "TLS certificate verification is disabled (see HTTPS_INSECURE_SSL / "
            "FEISHU_WEBHOOK_INSECURE_SSL / SEMANTIC_SCHOLAR_INSECURE_SSL).",
            UserWarning,
            stacklevel=2,
        )
        return ssl._create_unverified_context()

    for env_key in (
        "FEISHU_WEBHOOK_CA_BUNDLE",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        path = os.environ.get(env_key, "").strip()
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
