"""外部グループからの周知の転送 R-1: 取り込み（docs/line-relay-design.md §2）。"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import lake, lake_api, line_worker, main, relay
from app.deps import set_repo
from app.jpdate import JST
from app.models import LineGroup, Member
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
T0 = datetime(2026, 11, 25, 16, 24)


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=JST).timestamp() * 1000)


def _msg(message_id: str, at: datetime, *, text: str | None = "本文", type_: str = "text",
         group: str = "GB", user: str = "UX", **message) -> dict:
    payload = {
        "type": "message", "timestamp": _ms(at), "webhookEventId": f"evt-{message_id}",
        "source": {"type": "group", "groupId": group, "userId": user},
        "message": {"type": type_, "id": message_id, **({"text": text} if text else {}),
                    **message},
    }
    return lake.envelope("webhook", payload, id=payload["webhookEventId"])


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="会員 一郎", line_user_id="U1"))
    r.save_line_group(LineGroup(group_id="GB", group_name="ブロック", profile_at=T0,
                                relay_source=True, relay_label="〇〇ブロック協議会"))
    r.save_line_group(LineGroup(group_id="GL", group_name="LOM", profile_at=T0))
    set_repo(r)
    relay._source_cache.clear()
    line_worker._known_groups.clear()
    line_worker._member_groups.clear()
    yield r
    set_repo(None)
    relay._source_cache.clear()
    line_worker._known_groups.clear()
    line_worker._member_groups.clear()


@pytest.fixture
def fakes(monkeypatch):
    calls = {"uploads": [], "line": []}
    monkeypatch.setenv("LINE_CONTENT_BUCKET", "lake-bucket")
    monkeypatch.setattr(lake, "publish", lambda envs: True)

    def line_get(url, limit=None):
        calls["line"].append(url)
        if url.endswith("/content"):
            return 200, {"Content-Type": "image/jpeg"}, b"jpeg"
        if "/member/UX" in url:
            return 200, {}, json.dumps({"displayName": "ブロック事務局"}).encode()
        return 404, {}, b""

    monkeypatch.setattr(line_worker, "_line_get", line_get)
    monkeypatch.setattr(line_worker, "_gcs_upload",
                        lambda name, raw, ctype, meta: calls["uploads"].append(name))
    monkeypatch.setattr(line_worker, "_gcs_delete_prefix", lambda prefix: 0)
    return calls


# --------------------------------------------------------------------------- #
# ワーカーでの取り込み
# --------------------------------------------------------------------------- #
def test_source_group_text_is_buffered(repo, fakes):
    assert line_worker.handle_envelope(repo, _msg("a1", T0, text="動画を見てください"), T0) \
        == "relay_buffered"
    [post] = repo.list_relay_posts()
    assert (post.message_id, post.group_id, post.user_id) == ("a1", "GB", "UX")
    assert post.text == "動画を見てください" and post.sent_at == T0


def test_other_group_is_not_buffered(repo, fakes):
    assert line_worker.handle_envelope(repo, _msg("a1", T0, group="GL"), T0) == "noop"
    assert repo.list_relay_posts() == []


def test_left_or_unmarked_group_is_not_source(repo, fakes):
    group = repo.get_line_group("GB")
    group.left_at = T0
    repo.save_line_group(group)
    assert line_worker.handle_envelope(repo, _msg("a1", T0), T0) == "noop"


def test_sticker_is_not_buffered(repo, fakes):
    env = _msg("a1", T0, text=None, type_="sticker")
    assert line_worker.handle_envelope(repo, env, T0) == "noop"
    assert repo.list_relay_posts() == []


def test_non_member_image_in_source_group_is_stored(repo, fakes):
    env = _msg("img1", T0, text=None, type_="image")
    assert line_worker.handle_envelope(repo, env, T0) == "stored"
    assert fakes["uploads"] == ["line/content/img1/img1.jpg"]
    [post] = repo.list_relay_posts()
    assert post.attachment.storage_uri == "gs://lake-bucket/line/content/img1/img1.jpg"
    assert post.attachment.mime == "image/jpeg"


def test_non_member_image_elsewhere_is_still_skipped(repo, fakes):
    env = _msg("img1", T0, text=None, type_="image", group="GL")
    assert line_worker.handle_envelope(repo, env, T0) == "skipped_non_member"
    assert fakes["uploads"] == [] and repo.list_relay_posts() == []


def test_unsent_post_is_dropped(repo, fakes):
    line_worker.handle_envelope(repo, _msg("a1", T0), T0)
    unsend = lake.envelope("webhook", {
        "type": "unsend", "timestamp": _ms(T0), "webhookEventId": "evt-u",
        "source": {"type": "group", "groupId": "GB", "userId": "UX"},
        "unsend": {"messageId": "a1"}}, id="evt-u")
    line_worker.handle_envelope(repo, unsend, T0)
    assert repo.list_relay_posts() == []


# --------------------------------------------------------------------------- #
# まとめて確定
# --------------------------------------------------------------------------- #
def _feed(repo, *items):
    for env in items:
        line_worker.handle_envelope(repo, env, T0)


def test_consecutive_posts_are_merged(repo, fakes):
    _feed(repo,
          _msg("a1", T0, text="本会の運動方針動画です\nhttps://example.com/v"),
          _msg("a2", T0 + timedelta(minutes=2), text="各LOMメンバーに展開お願いします"))

    assert relay.finalize(repo, T0 + timedelta(minutes=11)) == []  # 最後の投稿から10分たっていない
    [notice] = relay.finalize(repo, T0 + timedelta(minutes=12))

    assert notice.source == "line_group" and notice.source_ref == "GB:a1"
    assert notice.subject == "本会の運動方針動画です"
    assert notice.body_text == ("本会の運動方針動画です\nhttps://example.com/v\n\n"
                                "各LOMメンバーに展開お願いします")
    assert notice.from_name == "ブロック事務局"
    assert notice.received_at == T0
    assert notice.relay.group_id == "GB" and notice.relay.message_ids == ["a1", "a2"]
    assert notice.relay.kind is None  # 判定は R-2
    assert repo.get_notice(notice.notice_id) is not None
    assert repo.list_relay_posts() == []


def test_gap_or_other_sender_splits(repo, fakes):
    _feed(repo,
          _msg("a1", T0, text="一件目"),
          _msg("a2", T0 + timedelta(minutes=15), text="二件目"),
          _msg("b1", T0 + timedelta(minutes=1), text="別の人", user="UY"))
    notices = relay.finalize(repo, T0 + timedelta(minutes=30))
    assert sorted(n.body_text for n in notices) == ["一件目", "二件目", "別の人"]


def test_image_only_post_uses_file_name_as_subject(repo, fakes):
    _feed(repo, _msg("f1", T0, text=None, type_="file", fileName="案内.pdf", fileSize=10))
    [notice] = relay.finalize(repo, T0 + timedelta(minutes=20))
    assert notice.subject == "案内.pdf" and notice.body_text == ""
    assert notice.attachments[0].name == "案内.pdf"


def test_finalize_is_idempotent(repo, fakes):
    _feed(repo, _msg("a1", T0))
    [first] = relay.finalize(repo, T0 + timedelta(minutes=20))
    # Pub/Sub の再送で同じ投稿がまた溜まっても、同じ連絡を二重に作らない
    _feed(repo, _msg("a1", T0))
    assert relay.finalize(repo, T0 + timedelta(minutes=25)) == []
    assert [n.notice_id for n in repo.list_notices()] == [first.notice_id]
    assert repo.list_relay_posts() == []


def test_tasks_relay_endpoint(repo, fakes, monkeypatch):
    _feed(repo, _msg("a1", T0))
    monkeypatch.setattr(main, "datetime", type("D", (), {"now": staticmethod(
        lambda: T0 + timedelta(minutes=20))}))
    res = client.post("/tasks/relay")
    assert res.status_code == 200 and len(res.json()["created"]) == 1
    # 同じ5分枠の二度目は何もしない
    assert client.post("/tasks/relay").json()["skipped"] == "already_ran"


def test_tasks_relay_requires_token(repo):
    public = TestClient(main.app)
    assert public.post("/tasks/relay").status_code == 401


# --------------------------------------------------------------------------- #
# 管理 API
# --------------------------------------------------------------------------- #
def test_put_relay_source(repo):
    res = client.put("/api/line/groups/GL/relay",
                     json={"relay_source": True, "relay_label": " 〇〇ブロック "})
    assert res.status_code == 200
    group = repo.get_line_group("GL")
    assert group.relay_source and group.relay_label == "〇〇ブロック"
    assert repo.list_audit()[0].action == "line_group.relay"

    res = client.put("/api/line/groups/GL/relay", json={"relay_source": False})
    assert res.status_code == 200 and not repo.get_line_group("GL").relay_source


def test_put_relay_source_needs_label_and_known_group(repo):
    assert client.put("/api/line/groups/GL/relay",
                      json={"relay_source": True}).status_code == 400
    assert client.put("/api/line/groups/NOPE/relay",
                      json={"relay_source": False}).status_code == 404


def test_line_groups_list_shows_relay(repo, monkeypatch):
    monkeypatch.setenv("LINE_EVENTS_TOPIC", "line-events")
    monkeypatch.setattr(lake_api.lake_query, "query", lambda sql, params: [
        {"group_id": "GB", "group_name": "ブロック"}, {"group_id": "GL", "group_name": "LOM"}])
    rows = {r["group_id"]: r for r in client.get("/api/line/groups").json()}
    assert rows["GB"]["relay_source"] and rows["GB"]["relay_label"] == "〇〇ブロック協議会"
    assert not rows["GL"]["relay_source"]
