"""周知の転送 R-3: 管理画面からの転送（docs/line-relay-design.md §5）。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_push, main, notices_api, relay
from app.deps import set_repo
from app.models import ExternalNotice, LineGroup, Member, NoticeRelay
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOON = datetime(2026, 11, 26, 12, 0)


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="sed", name="専務 一郎", line_user_id="USED",
                           officer_role="専務理事"))
    r.save_line_group(LineGroup(group_id="GB", group_name="ブロック", relay_source=True,
                                relay_label="〇〇ブロック協議会"))
    r.save_line_group(LineGroup(group_id="GALL", group_name="LOM 会員全員"))
    r.save_line_group(LineGroup(group_id="GRIJI", group_name="LOM 理事会"))
    settings = r.get_settings()
    settings.line_groups = {"all": "GALL", "riji": "GRIJI"}
    r.save_settings(settings)
    r.upsert_notice(ExternalNotice(
        notice_id="n1", source="line_group", source_ref="GB:a1", received_at=NOON,
        subject="アンケートのお願い", body_text="アンケートのお願い\nhttps://example.com/f",
        relay=NoticeRelay(group_id="GB", message_ids=["a1"], kind="other", summary="要約")))
    r.upsert_notice(ExternalNotice(notice_id="g1", source="gmail", received_at=NOON,
                                   subject="メール", body_text="本文"))
    set_repo(r)
    monkeypatch.setattr(notices_api, "datetime", type("D", (), {"now": staticmethod(
        lambda: NOON)}))
    yield r
    set_repo(None)


@pytest.fixture
def pushed(monkeypatch):
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda to, messages: sent.append((to, messages)) or True)
    return sent


DEFAULT = "【〇〇ブロック協議会より】\nアンケートのお願い\nhttps://example.com/f"


def test_relay_view(repo):
    view = client.get("/api/notices/n1/relay").json()
    assert view["label"] == "〇〇ブロック協議会"
    assert view["text"] == view["default_text"] == DEFAULT and not view["edited"]
    assert [(g["key"], g["group_name"]) for g in view["groups"]] == [
        ("all", "LOM 会員全員"), ("riji", "LOM 理事会")]
    assert view["kind"] == "other" and view["post"] is None


def test_send_edited_text_to_board_group(repo, pushed):
    text = DEFAULT + "\n\n※理事の皆さんは今週中にお願いします"
    res = client.post("/api/notices/n1/relay", json={"group_key": "riji", "text": text})
    assert res.status_code == 200
    body = res.json()
    assert body["result"] == "sent" and body["group_name"] == "LOM 理事会"
    [(to, [message])] = pushed
    assert to == "GRIJI" and message.text == "{everyone}\n" + text

    notice = repo.get_notice("n1")
    assert notice.status == "delivered" and notice.relay.text == text
    assert notice.relay.target_group_id == "GRIJI"
    view = body["relay"]
    assert view["edited"] and view["target_group_name"] == "LOM 理事会"
    assert view["post"]["status"] == "sent"
    assert any(a.action == "notice.relay.approve" and a.actor == "unknown"
               for a in repo.list_audit())

    # 二度目は force が要る
    again = client.post("/api/notices/n1/relay", json={"group_key": "all", "text": text})
    assert again.status_code == 409
    forced = client.post("/api/notices/n1/relay",
                         json={"group_key": "all", "text": DEFAULT, "force": True})
    assert forced.status_code == 200 and pushed[-1][0] == "GALL"
    assert repo.get_notice("n1").relay.text is None  # 元の文面に戻したら上書きを消す

    # LINE のカードのボタンは、もう受け付けない
    [reply] = relay.handle_postback(repo, repo.get_member("sed"), "rly|go|n1", NOON)
    assert "すでに" in reply.text


def test_pending_post_blocks_second_send(repo, pushed, monkeypatch):
    night = datetime(2026, 11, 26, 23, 0)
    monkeypatch.setattr(notices_api, "datetime",
                        type("D", (), {"now": staticmethod(lambda: night)}))
    res = client.post("/api/notices/n1/relay", json={"group_key": "all", "text": DEFAULT})
    assert res.json()["result"] == "scheduled" and pushed == []
    assert client.post("/api/notices/n1/relay",
                       json={"group_key": "all", "text": DEFAULT}).status_code == 409


@pytest.mark.parametrize("notice_id,payload,status", [
    ("g1", {"group_key": "all", "text": "x"}, 400),  # LINE グループの連絡ではない
    ("n1", {"group_key": "goyaku", "text": "x"}, 400),  # 設定されていない転送先
    ("n1", {"group_key": "all", "text": "  "}, 400),  # 空の文面
    ("nope", {"group_key": "all", "text": "x"}, 404),
])
def test_send_validation(repo, pushed, notice_id, payload, status):
    assert client.post(f"/api/notices/{notice_id}/relay", json=payload).status_code == status
    assert pushed == []
