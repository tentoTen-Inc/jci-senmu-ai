"""LOM ごとの名前・URL（app/lom.py）のテスト。"""
from fastapi.testclient import TestClient

from app import lom, main
from app.models import LineGroup, Member, Settings
from tests.conftest import ADMIN_AUTH

LOM_ENV = {"LOM_NAME": "サンプル青年会議所", "LOM_SHORT_NAME": "サンプルJC",
           "BOT_NAME": "サンプル専務AI", "LINE_BASIC_ID": "@sample", "LOM_ID": "sample"}


def test_defaults_are_generic(monkeypatch):
    for name in [*LOM_ENV, "ADMIN_BASE_URL"]:
        monkeypatch.delenv(name, raising=False)
    assert lom.lom_name() == "青年会議所" and lom.bot_name() == "専務AI"
    assert lom.friend_url() is None and lom.admin_url() is None
    assert lom.lom_id() == "lom"


def test_env_values_are_used(monkeypatch):
    for k, v in LOM_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ADMIN_BASE_URL", "https://admin.example.org/")
    assert lom.admin_url("/app/feedback") == "https://admin.example.org/app/feedback"
    assert lom.friend_url() == "https://line.me/R/ti/p/@sample"
    assert Member(member_id="m1", name="山田 太郎").lom_id == "sample"
    assert "サンプルJCのサンプル専務AIです" in main.welcome_text()


def test_lom_api_returns_profile_and_groups(monkeypatch):
    from app.deps import get_repo

    monkeypatch.setenv("BOT_NAME", "サンプル専務AI")
    repo = get_repo()
    repo.save_settings(Settings(line_groups={"goyaku": "G1"}))
    repo.save_line_group(LineGroup(group_id="G1", group_name="2027年度 五役"))
    repo.save_line_group(LineGroup(group_id="G2", group_name=None))  # 名前が無いものは出さない
    body = TestClient(main.app, headers=ADMIN_AUTH).get("/api/lom").json()
    assert body["bot_name"] == "サンプル専務AI"
    assert body["line_groups"] == {"goyaku": "G1"}
    assert body["groups"] == [{"group_id": "G1", "group_name": "2027年度 五役"}]
    assert body["drive_reader_sa"].startswith("drive-reader@")


def test_liff_page_shows_bot_name(monkeypatch):
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    monkeypatch.setenv("BOT_NAME", "<サンプル>専務AI")
    html = TestClient(main.app).get("/liff/poll").text
    assert "<title>&lt;サンプル&gt;専務AI</title>" in html
