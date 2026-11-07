"""管理サービス基盤(/api・監査ログ・SPA配信)のテスト。"""
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import Member
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH, iap_headers, iap_jwt

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@example.org"}


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1"))
    set_repo(r)
    yield r
    set_repo(None)


def test_api_alias_works():
    # 同じハンドラが /api でも /admin でも応答
    assert client.get("/api/members").status_code == 200
    assert client.get("/admin/members").status_code == 200


def test_api_is_protected():
    noauth = TestClient(main.app)
    assert noauth.get("/api/members").status_code == 401


def test_iap_jwt_authorizes_behind_iap(monkeypatch):
    """IAP で守られた管理サービス（ROLE=admin）では、IAP が署名した JWT を検証して認可する。"""
    monkeypatch.setenv("ROLE", "admin")
    iap_client = TestClient(main.app)
    res = iap_client.get("/api/members", headers=iap_headers("sed@example.org"))
    assert res.status_code == 200


def test_iap_requires_valid_jwt(monkeypatch):
    """メールのヘッダだけ・宛先違い・発行者違い・別の鍵で署名した JWT では入れない。"""
    monkeypatch.setenv("ROLE", "admin")
    iap_client = TestClient(main.app)
    email = {"X-Goog-Authenticated-User-Email": "accounts.google.com:sed@example.org"}
    bad = [
        iap_jwt("sed@example.org", audience="/projects/1/locations/x/services/other"),
        iap_jwt("sed@example.org", issuer="https://example.com"),
        iap_jwt("sed@example.org", key=ec.generate_private_key(ec.SECP256R1())),
        "not-a-jwt",
    ]
    assert iap_client.get("/api/members", headers=email).status_code == 401
    for token in bad:
        headers = email | {"X-Goog-IAP-JWT-Assertion": token}
        assert iap_client.get("/api/members", headers=headers).status_code == 401


def test_iap_actor_comes_from_jwt(repo, monkeypatch):
    """監査に残す人は JWT のメールアドレス。偽装したメールのヘッダは使わない。"""
    monkeypatch.setenv("ROLE", "admin")
    headers = iap_headers("sed@example.org") | {
        "X-Goog-Authenticated-User-Email": "accounts.google.com:attacker@example.com"}
    body = {"kill_switch": False,
            "quiet_hours": {"start": "21:00", "end": "08:00", "tz": "Asia/Tokyo"},
            "rate_limit": {"per_member_per_day": 3, "global_per_min": 30}}
    assert TestClient(main.app).put("/api/settings", json=body, headers=headers).status_code == 200
    actors = {a.actor for a in repo.list_audit() if a.action == "settings.update"}
    assert actors == {"accounts.google.com:sed@example.org"}


def test_spoofed_iap_header_rejected_on_public_service(monkeypatch):
    """公開サービスでは IAP のヘッダを偽装しても入れない（2026-10-12 本番で 200 を確認し修正）。"""
    monkeypatch.delenv("ROLE", raising=False)
    public = TestClient(main.app)
    spoofed = {"X-Goog-Authenticated-User-Email": "accounts.google.com:attacker@example.com"}
    for path in ("/api/members", "/api/gcal/status", "/admin/events", "/tasks/tick"):
        res = public.request("POST" if path == "/tasks/tick" else "GET", path, headers=spoofed)
        assert res.status_code == 401, path


def test_admin_token_works_on_both_services(monkeypatch):
    for role in ("admin", ""):
        monkeypatch.setenv("ROLE", role)
        assert TestClient(main.app).get("/api/members", headers=ADMIN_AUTH).status_code == 200
        wrong = {"X-Admin-Token": "wrong"}
        assert TestClient(main.app).get("/api/members", headers=wrong).status_code == 401


def test_public_service_closes_admin_api_by_default(monkeypatch):
    """公開サービスの既定では、共有シークレットがあっても管理 API に届かない（/tasks だけ通す）。"""
    monkeypatch.delenv("ROLE", raising=False)
    monkeypatch.delenv("PUBLIC_ADMIN_API")
    public = TestClient(main.app)
    for path in ("/api/members", "/admin/events"):
        assert public.get(path, headers=ADMIN_AUTH).status_code == 404, path
    assert public.post("/tasks/tick", headers={"X-Admin-Token": "wrong"}).status_code == 401
    # 管理サービスではそのまま使える
    monkeypatch.setenv("ROLE", "admin")
    assert TestClient(main.app).get("/api/members", headers=ADMIN_AUTH).status_code == 200


def test_api_docs_are_hidden():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert TestClient(main.app).get(path).status_code == 404, path


def test_settings_update_writes_audit(repo):
    body = {
        "kill_switch": True,
        "quiet_hours": {"start": "21:00", "end": "08:00", "tz": "Asia/Tokyo"},
        "rate_limit": {"per_member_per_day": 3, "global_per_min": 30},
    }
    res = client.put("/api/settings", json=body, headers=IAP)
    assert res.status_code == 200
    logs = repo.list_audit()
    assert any(a.action == "settings.update" and a.actor == "sed@example.org" for a in logs)
    # 監査API
    assert client.get("/api/audit-logs").status_code == 200
