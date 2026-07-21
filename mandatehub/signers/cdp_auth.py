"""
cdp_auth — Coinbase CDP facilitator の認証ヘッダ生成（任意の `[cdp]` extra）。

CDP の x402 facilitator（https://api.cdp.coinbase.com/platform/v2/x402 の
/verify・/settle）は、CDP API キー（Ed25519）で署名した短命 JWT の Bearer 認証を
要求する。JWT はリクエストごとに `{method} {host}{path}` へバインドされるため、
`RemoteFacilitatorAdapter` の header_hook でエンドポイント別に毎回生成する。

JWT の組み立ては公式 `cdp-sdk` に委譲する（ここに暗号を持ち込まない）。コアは
本モジュールを import しない。`pip install mandatehub[cdp]` で有効化。

実測で確認済みの CDP 固有仕様（2026-07-21、live /verify で isValid=true 取得）:
  - v1 の paymentRequirements は `description` と `mimeType` が必須。
  - 無効な支払いは HTTP 400 + 正規の verify JSON で返る（remote.py が両対応）。
  - self-send（from == payTo）は `self_send_not_allowed` で拒否される。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"


class MissingCdpExtraError(RuntimeError):
    """`cdp-sdk` が無い（`pip install 'mandatehub[cdp]'` が必要）。"""


def cdp_header_hook(
    api_key_id: str,
    api_key_secret: str,
    *,
    facilitator_url: str = CDP_FACILITATOR_URL,
) -> Callable[[str, dict[str, Any]], dict[str, str]]:
    """CDP 用の header_hook を返す。

    使い方:
        hook = cdp_header_hook(key_id, key_secret)
        adapter = RemoteFacilitatorAdapter(CDP_FACILITATOR_URL, network="base-sepolia",
                                           header_hook=hook)
    """
    try:
        from cdp.auth import JwtOptions, generate_jwt
    except ImportError as e:  # pragma: no cover - 環境依存
        raise MissingCdpExtraError(
            "cdp-sdk is not installed. Run: pip install 'mandatehub[cdp]'"
        ) from e

    parts = urlsplit(facilitator_url)
    host = parts.hostname or ""
    base_path = parts.path.rstrip("/")

    def hook(endpoint: str, _body: dict[str, Any]) -> dict[str, str]:
        jwt = generate_jwt(JwtOptions(
            api_key_id=api_key_id,
            api_key_secret=api_key_secret,
            request_method="POST",
            request_host=host,
            request_path=f"{base_path}/{endpoint}",
            expires_in=120,
        ))
        return {"Authorization": f"Bearer {jwt}"}

    return hook


def cdp_header_hook_from_file(
    path: str | Path = "~/.mandatehub-cdp.json",
    *,
    facilitator_url: str = CDP_FACILITATOR_URL,
) -> Callable[[str, dict[str, Any]], dict[str, str]]:
    """`{"keyId": ..., "keySecret": ...}` 形式の鍵ファイルから header_hook を作る。

    鍵はプロセス内でのみ保持し、ログ・例外・戻り値に一切載せない。
    """
    p = Path(path).expanduser()
    cfg = json.loads(p.read_text())
    return cdp_header_hook(cfg["keyId"], cfg["keySecret"], facilitator_url=facilitator_url)
