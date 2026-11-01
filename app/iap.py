"""IAP（管理サービス jci-sed-admin の前段）が付ける署名付きヘッダを検証する。

IAP を通ったリクエストには、ログインした人のメールアドレスが 2 通りで付く。
- ``X-Goog-Authenticated-User-Email``: ただのヘッダ。IAP を経ずに届いた要求なら誰でも付けられる
- ``X-Goog-IAP-JWT-Assertion``: IAP が署名した JWT。宛先（aud）がこのサービスかも確かめられる

IAP の設定が外れた・別経路で届いたときにも管理 API に入れないよう、JWT の署名・発行者・宛先を
確かめ、そのメールアドレスだけを信用する（https://docs.cloud.google.com/iap/docs/signed-headers-howto）。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

from google.auth import jwt

logger = logging.getLogger(__name__)

JWT_HEADER = "X-Goog-IAP-JWT-Assertion"
EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
ISSUER = "https://cloud.google.com/iap"
CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/"
CERTS_TTL_SECONDS = 3600
TIMEOUT_SECONDS = 5

_certs: dict[str, str] = {}
_certs_at = 0.0
_audience: str | None = None


def _get(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:  # noqa: S310 - 固定の URL
        return res.read().decode()


def _fetch_certs() -> dict[str, str]:
    return json.loads(_get(CERTS_URL))


def _public_keys() -> dict[str, str]:
    """IAP の公開鍵（鍵 ID → PEM）。1 時間ごとに取り直す（鍵は定期的に入れ替わる）。"""
    global _certs, _certs_at
    if not _certs or time.time() - _certs_at > CERTS_TTL_SECONDS:
        _certs = _fetch_certs()
        _certs_at = time.time()
    return _certs


def _metadata(path: str) -> str:
    return _get(METADATA_URL + path, {"Metadata-Flavor": "Google"}).strip()


def expected_audience() -> str | None:
    """このサービスの IAP の宛先 ``/projects/<番号>/locations/<リージョン>/services/<名前>``。

    環境変数 IAP_AUDIENCE があればそれを使う。なければ Cloud Run のメタデータ（プロジェクト番号・
    リージョン）と K_SERVICE（サービス名）から求める。求められなければ None（IAP では通さない）。
    """
    global _audience
    explicit = os.environ.get("IAP_AUDIENCE", "").strip()
    if explicit:
        return explicit
    if _audience:
        return _audience
    service = os.environ.get("K_SERVICE", "").strip()
    if not service:
        return None
    try:
        number = _metadata("project/numeric-project-id")
        region = _metadata("instance/region").rsplit("/", 1)[-1]  # projects/<番号>/regions/<名前>
    except Exception:  # noqa: BLE001 - 取れなければ通さない（次の要求で取り直す）
        logger.exception("IAP の宛先をメタデータから求められませんでした")
        return None
    _audience = f"/projects/{number}/locations/{region}/services/{service}"
    return _audience


def verified_email(assertion: str | None) -> str | None:
    """IAP の JWT を検証し、ログインした人のメールアドレスを返す。検証できなければ None。"""
    if not assertion:
        return None
    audience = expected_audience()
    if not audience:
        return None
    try:
        claims = jwt.decode(assertion, certs=_public_keys(), audience=audience)
    except Exception as exc:  # noqa: BLE001 - 署名・期限・宛先のどれが違っても通さない
        logger.warning("IAP の JWT を検証できませんでした: %s", exc)
        return None
    if claims.get("iss") != ISSUER:
        logger.warning("IAP の JWT の発行者が違います: %s", claims.get("iss"))
        return None
    return claims.get("email") or None
