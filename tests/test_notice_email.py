"""会議の案内メール（docs/meeting-notice-design.md 段階 B）。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import gmail, main, meeting_dispatch, notice_layout
from app.deps import set_repo
from app.models import (
    Contact,
    DocNumberSetting,
    EmailStyle,
    Event,
    EventType,
    MeetingNoticeSetting,
    Member,
    MemberType,
    Settings,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from scripts.sync_member_emails import plan
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
START = datetime(2026, 11, 13, 19, 0)
NOW = datetime(2026, 11, 6, 9, 0)


# --------------------------------------------------------------------------- #
# レイアウト記法
# --------------------------------------------------------------------------- #
def test_layout_to_html():
    body = ("[右] 令和8年11月6日\n[題] ご案内\n[中] 記\n[字下げ] 下記の通り\n\n"
            "資料　[アジェンダ](https://docs.google.com/d/A) <b>x</b> https://meet.google.com/abc")
    out = notice_layout.to_html(body, EmailStyle(font_family="Open Sans", font_size=11))
    assert 'text-align:right"><span style="font-size:11pt;font-family:\'Open Sans\'' in out
    assert "font-size:20pt" in out and "font-weight:700" in out
    assert "text-align:center" in out and "text-indent:11pt" in out
    assert "<br>" in out
    assert '<a href="https://docs.google.com/d/A">アジェンダ</a>' in out
    assert '<a href="https://meet.google.com/abc">https://meet.google.com/abc</a>' in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "<b>" not in out  # 値は HTML として扱わない
    assert "[右]" not in out


def test_layout_to_text_and_font_sanitized():
    text = notice_layout.to_text("[右] 以上\n[字下げ] 本文\n[アジェンダ](https://x.example/a)")
    assert text == "以上\n　本文\nアジェンダ（https://x.example/a）"
    out = notice_layout.to_html("a", EmailStyle(font_family="x';background:url(y)"))
    assert "url(" not in out


# --------------------------------------------------------------------------- #
# 宛先・文書番号・下書き
# --------------------------------------------------------------------------- #
def _member(mid, name, email=None, role=None, line=None, **kw):
    return Member(member_id=mid, name=name, officer_role=role, line_user_id=line,
                  contact=Contact(email=email), **kw)


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("LOM_GOOGLE_ACCOUNT", "office@example.org")
    r = InMemoryRepository()
    r.upsert_member(_member("m1", "青木 一郎", "aoki@example.org", "理事長", "U1"))
    r.upsert_member(_member("m2", "江口 隆之", "eguchi@example.org", "専務理事", "U2"))
    r.upsert_member(_member("m3", "杉山 正人", "sugi@example.org"))  # LINE 未連携でもメールは届ける
    r.upsert_member(_member("m4", "田村 光", None, line="U4"))
    r.upsert_member(_member("m5", "OB 太郎", "ob@example.org", member_type=MemberType.ob))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.理事会, title="第2回理事会", datetime_start=START,
        datetime_end=datetime(2026, 11, 13, 21, 0), location="会館 or オンライン",
        meet_url="https://meet.google.com/abc-defg-hij", fiscal_year=2027,
        target_scope=TargetScope(kind=TargetScopeKind.custom,
                                 value=["m1", "m2", "m3", "m4", "m5"])))
    r.upsert_event(Event(
        event_id="ev2", type=EventType.理事会, title="第3回理事会",
        datetime_start=datetime(2026, 12, 10, 19), fiscal_year=2027,
        target_scope=TargetScope(kind=TargetScopeKind.custom, value=["m1"])))
    r.save_settings(Settings(
        meeting_notices={"理事会": MeetingNoticeSetting(
            audience="理事各位", sender_role="理事長", sender_title="理事長予定者",
            cc_roles=["専務理事"])},
        doc_number=DocNumberSetting(enabled=True, prefix="○○発", term="27",
                                    start_seq={"2027": 4})))
    set_repo(r)
    drafts = []

    def fake_draft(**kw):
        drafts.append(kw)
        return kw.get("draft_id") or f"d{len(drafts)}"

    monkeypatch.setattr(meeting_dispatch.gmail, "save_draft", fake_draft)
    r.drafts = drafts
    yield r
    set_repo(None)


def test_recipients_bcc_by_default(repo):
    setting = meeting_dispatch.notice_setting(repo, repo.get_event("ev1"))
    rcpt = meeting_dispatch.recipients(repo, repo.get_event("ev1"), setting)
    assert rcpt["mode"] == "bcc" and rcpt["to"] == ["office@example.org"]
    assert rcpt["cc"] == ["eguchi@example.org"]
    assert rcpt["bcc"] == ["aoki@example.org", "sugi@example.org"]  # OB は入らない
    assert rcpt["missing"] == [{"member_id": "m4", "name": "田村 光"}]
    assert rcpt["count"] == 3

    to_mode = setting.model_copy(update={"to_mode": "to", "cc_roles": []})
    rcpt = meeting_dispatch.recipients(repo, repo.get_event("ev1"), to_mode)
    assert rcpt["to"] == ["aoki@example.org", "eguchi@example.org", "sugi@example.org"]
    assert rcpt["bcc"] == []


def test_email_view_fills_sender_and_doc_number(repo):
    view = client.get("/api/events/ev1/dispatch").json()["email"]
    assert view["template_id"] == "email_meeting_notice"
    assert view["doc_number"] == "○○発第27-004号"
    body = view["preview"]["body"]
    assert "理事各位" in body and "理事長予定者　青木 一郎" in body
    assert "[右] ○○発第27-004号" in body
    assert view["preview"]["subject"] == "【2027年度】第2回理事会開催のご案内"
    assert '<a href="https://meet.google.com/abc-defg-hij">' in view["preview"]["html"]
    assert view["hints"] == []


def test_make_draft_reuses_number_and_draft(repo):
    res = client.post("/api/events/ev1/dispatch/email-draft",
                      json={"template_id": "email_meeting_notice", "expected_count": 3})
    assert res.status_code == 200, res.text
    sent = repo.drafts[0]
    assert sent["bcc"] == ["aoki@example.org", "sugi@example.org"] and sent["draft_id"] is None
    assert "<p" in sent["html"] and "○○発第27-004号" in sent["text"]
    email = repo.get_meeting_dispatch("ev1").email
    assert (email.doc_seq, email.draft_id, email.fiscal_year) == (4, "d1", 2027)

    # 作り直すときは同じ番号・同じ下書き
    client.post("/api/events/ev1/dispatch/email-draft",
                json={"template_id": "email_meeting_notice", "expected_count": 3})
    assert repo.drafts[1]["draft_id"] == "d1"
    assert repo.get_meeting_dispatch("ev1").email.doc_number == "○○発第27-004号"
    # 別の会議は続きの番号
    assert client.get("/api/events/ev2/dispatch").json()["email"]["doc_number"] == \
        "○○発第27-005号"
    assert any(a.action == "dispatch.email_draft" for a in repo.list_audit())


def test_make_draft_refuses_changed_recipients_and_reports_gmail_errors(repo, monkeypatch):
    res = client.post("/api/events/ev1/dispatch/email-draft",
                      json={"template_id": "email_meeting_notice", "expected_count": 2})
    assert res.status_code == 409 and repo.drafts == []

    def fail(**_):
        raise gmail.DraftError("Gmail に下書きを作る権限がありません。")

    monkeypatch.setattr(meeting_dispatch.gmail, "save_draft", fail)
    res = client.post("/api/events/ev1/dispatch/email-draft",
                      json={"template_id": "email_meeting_notice", "expected_count": 3})
    assert res.status_code == 503 and "権限" in res.json()["detail"]
    assert repo.get_meeting_dispatch("ev1") is None  # 番号を使い切らない


def test_doc_number_disabled_drops_line(repo):
    repo.save_settings(repo.get_settings().model_copy(update={"doc_number": DocNumberSetting()}))
    view = client.get("/api/events/ev1/dispatch").json()["email"]
    assert view["doc_number"] is None
    assert "発第" not in view["preview"]["body"]


def test_preview_endpoint_returns_html_for_email(repo):
    res = client.post("/api/notice-templates/preview",
                      json={"body": "[右] {{doc_number}}\n{{audience}}", "channel": "email",
                            "event_id": "ev1"}).json()
    assert res["text"] == "[右] ○○発第27-004号\n理事各位"
    assert "text-align:right" in res["html"]


def test_raw_message_has_html_and_text():
    import base64
    import email

    raw = gmail._raw(to=["a@example.org"], cc=[], bcc=["b@example.org"], subject="【案内】会議",
                     html="<p>本文</p>", text="本文")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["Bcc"] == "b@example.org" and msg.is_multipart()
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain", "text/html"]


def test_sync_member_emails_plan():
    registered = [_member("m1", "青木 一郎"), _member("m2", "江口隆之", "old@example.org")]
    roster = [_member("r1", "青木　一郎", "aoki@example.org"),
              _member("r2", "江口 隆之", "new@example.org")]
    assert [(m.member_id, e, s) for m, e, s in plan(registered, roster)] == [
        ("m1", "aoki@example.org", "追加"), ("m2", "new@example.org", "既にあり")]
    assert plan(registered, roster, overwrite=True)[1][2] == "変更"
