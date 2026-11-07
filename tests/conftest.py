"""テスト共通設定。

アプリ import 前に LINE シークレットを環境変数で固定し、
Secret Manager への実アクセスを避ける。
"""
import os
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth import jwt
from google.auth.crypt import es256

os.environ.setdefault("LINE_CHANNEL_SECRET", "test_channel_secret")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "PLACEHOLDER_SET_ME")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("ADMIN_API_SECRET", "test-admin-secret")
# 管理 API のテストは X-Admin-Token で呼ぶので、公開サービスでも開いた状態で動かす。
# 既定で閉じていることは test_api_foundation で確かめる
os.environ.setdefault("PUBLIC_ADMIN_API", "on")
os.environ.setdefault("ADMIN_BASE_URL", "https://admin.example.org")
os.environ.setdefault("IAP_AUDIENCE", "/projects/1/locations/asia-northeast1/services/test-admin")

TEST_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ADMIN_AUTH = {"X-Admin-Token": os.environ["ADMIN_API_SECRET"]}

# IAP の署名を真似るテスト用の鍵（ES256）。本物の公開鍵の代わりに app.iap に渡す。
# conftest は pytest と `from tests.conftest import ...` で 2 回読まれるため、鍵は毎回同じにする
_IAP_KEY = ec.derive_private_key(20261101, ec.SECP256R1())
_IAP_KID = "test-iap-key"
_IAP_PUBLIC_PEM = _IAP_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def iap_jwt(email: str, *, audience: str | None = None, issuer: str = "https://cloud.google.com/iap",
            key=None) -> str:
    """IAP が付ける X-Goog-IAP-JWT-Assertion と同じ形の JWT を作る。"""
    pem = (key or _IAP_KEY).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    signer = es256.ES256Signer.from_string(pem, key_id=_IAP_KID)
    now = int(time.time())
    claims = {"iss": issuer, "aud": audience or os.environ["IAP_AUDIENCE"], "email": email,
              "sub": f"accounts.google.com:{email}", "iat": now, "exp": now + 3600}
    return jwt.encode(signer, claims).decode()


def iap_headers(email: str) -> dict:
    """管理サービス（ROLE=admin）に IAP 経由で届いたときのヘッダ。"""
    return {"X-Goog-IAP-JWT-Assertion": iap_jwt(email),
            "X-Goog-Authenticated-User-Email": f"accounts.google.com:{email}"}


@pytest.fixture(autouse=True)
def _no_secret_manager(monkeypatch):
    """本物の Secret Manager に繋がない（手元の gcloud の認証で読みに行き、待たされていた）。

    シークレットは環境変数で渡す。テストで要るものは上の os.environ.setdefault で入れている。
    """
    from app import config

    monkeypatch.setattr(config, "_from_secret_manager", lambda secret_id: None)


@pytest.fixture(autouse=True)
def _iap_test_keys(monkeypatch):
    """IAP の公開鍵をネットから取らず、テスト用の鍵を使う。"""
    from app import iap

    monkeypatch.setattr(iap, "_fetch_certs", lambda: {_IAP_KID: _IAP_PUBLIC_PEM})
    monkeypatch.setattr(iap, "_certs", {})


@pytest.fixture(autouse=True)
def _no_live_firestore():
    """リポジトリを注入していないテストが本物の Firestore に繋ぎに行かないようにする。"""
    from app import deps
    from app.repository import InMemoryRepository

    injected = deps._repo is None
    if injected:
        deps.set_repo(InMemoryRepository())
    yield
    if injected:
        deps.set_repo(None)
