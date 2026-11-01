"""環境設定とシークレット解決。

ローカルでは .env / 環境変数、本番(Cloud Run)では Secret Manager から取得する。
Secret Manager は Cloud Run の --set-secrets で環境変数にマウントする運用を基本とし、
未設定時のみ API 経由で取得するフォールバックを持つ。
"""
import os
import time

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
TZ = os.environ.get("TZ", "Asia/Tokyo")


def behind_iap() -> bool:
    """IAP で守られたサービス（管理サービス jci-sed-admin、環境変数 ROLE=admin）で動いているか。

    IAP の付ける X-Goog-Authenticated-User-Email を信用してよいのはこのときだけ。
    公開サービス（jci-sed-agent）では誰でも同じヘッダを付けて送れるため信用しない。
    """
    return os.environ.get("ROLE", "").strip() == "admin"


def async_line_reply() -> bool:
    """LINE への返信を Webhook の外（Pub/Sub → /pubsub/line-reply）で作るか。

    環境変数 LINE_ASYNC_REPLY=1 で有効。Push サブスクリプション `line-events-reply` を
    作ってから有効にする（scripts/setup_line_reply.sh）。無効の間は Webhook の中で返信する。
    """
    return os.environ.get("LINE_ASYNC_REPLY", "").strip() == "1"


def admin_api_secret() -> str | None:
    """/admin・/tasks 保護用の共有シークレット（環境変数 or Secret Manager）。"""
    return get_secret("ADMIN_API_SECRET", "admin-api-secret")


#: 取れたシークレットは覚える。取れなかったものは少し待ってから取り直す
_secret_cache: dict[str, str] = {}
_secret_failed_at: dict[str, float] = {}
SECRET_TIMEOUT_SECONDS = 10
SECRET_RETRY_SECONDS = 300


def _from_secret_manager(secret_id: str) -> str | None:
    """Secret Manager から最新版を読む。取れなければ None（起動・ヘルスチェックは落とさない）。

    以前は失敗（None）もずっと覚えていたため、起動直後に一度取れないと、インスタンスが
    入れ替わるまで LINE の送信などが止まったままになりえた。今は 5 分たてば取り直す
    （使っていない機能のシークレットを毎回読みに行かないよう、すぐには取り直さない）。
    """
    if secret_id in _secret_cache:
        return _secret_cache[secret_id]
    failed_at = _secret_failed_at.get(secret_id)
    if failed_at is not None and time.monotonic() - failed_at < SECRET_RETRY_SECONDS:
        return None
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        res = client.access_secret_version(name=name, timeout=SECRET_TIMEOUT_SECONDS)
        value = res.payload.data.decode("utf-8")
    except Exception:  # noqa: BLE001 - 起動時に握りつぶしてヘルスチェックは通す
        _secret_failed_at[secret_id] = time.monotonic()
        return None
    _secret_failed_at.pop(secret_id, None)
    _secret_cache[secret_id] = value
    return value


def get_secret(env_key: str, secret_id: str) -> str | None:
    """環境変数優先、なければ Secret Manager。"""
    val = os.environ.get(env_key)
    if val:
        return val
    return _from_secret_manager(secret_id)


def line_channel_secret() -> str | None:
    return get_secret("LINE_CHANNEL_SECRET", "line-channel-secret")


def line_channel_access_token() -> str | None:
    return get_secret("LINE_CHANNEL_ACCESS_TOKEN", "line-channel-access-token")
