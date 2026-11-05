"""通知テンプレートと会議の LINE 配信（docs/meeting-notice-design.md 段階 A）。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, meeting_dispatch, notice_template
from app.deps import set_repo
from app.jpdate import wareki_date
from app.models import (
    Event,
    EventType,
    LineGroup,
    Member,
    Proposal,
    SchedulePoll,
    Settings,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
START = datetime(2026, 11, 5, 19, 0)


# --------------------------------------------------------------------------- #
# 差し込み
# --------------------------------------------------------------------------- #
def test_render_values_sections_and_optional_lines():
    body = ("{{#late}}遅れてすみません。\n{{/late}}{{event.title}}\n場所：{{?event.place}}\n"
            "{{#proposals}}{{#first}}内容　{{/first}}{{^first}}　　　{{/first}}{{no}} {{title}}\n"
            "{{/proposals}}{{^online}}オンラインはありません{{/online}}")
    ctx = {"late": False, "event.title": "理事会", "event.place": "", "online": False,
           "proposals": [{"no": "第1号議案", "title": "A"}, {"no": "第2号議案", "title": "B"}]}
    assert notice_template.render(body, ctx) == (
        "理事会\n内容　第1号議案 A\n　　　第2号議案 B\nオンラインはありません")
    ctx["late"] = True
    assert notice_template.render(body, ctx).startswith("遅れてすみません。\n理事会")


def test_validate_reports_unknown_names_and_unclosed_blocks():
    assert notice_template.validate("{{event.title}} {{#proposals}}{{no}}{{/proposals}}") == []
    errors = notice_template.validate("{{event.titel}} {{#late}}…")
    assert any("event.titel" in e for e in errors)
    assert any("閉じていません" in e for e in errors)
    assert notice_template.validate("{{no}}")  # 繰り返しの外では使えない
    assert notice_template.validate("{{/late}}")
    assert notice_template.validate("{{ 日本語 }}")


def test_defaults_are_valid_and_lom_neutral():
    items = notice_template.defaults()
    assert {t.template_id for t in items} >= {"line_agenda", "line_correction"}
    for t in items:
        assert notice_template.validate(t.body, t.subject) == []
        assert t.builtin
        # LOM 固有の名前・役職を既定の文面に書かない
        assert not any(w in t.body for w in ("予定者", "五役", "青年会議所"))


def test_wareki_and_deliver_deadline():
    assert wareki_date(START) == "令和8年11月5日(木)"
    assert wareki_date(datetime(2019, 5, 1)) == "令和元年5月1日(水)"
    # 既定は2日前の24:00（＝前日の0:00）
    assert notice_template.deliver_deadline(START) == datetime(2026, 11, 4, 0, 0)
    assert notice_template.deliver_deadline(START, datetime(2026, 11, 2, 12)) == \
        datetime(2026, 11, 2, 12)


# --------------------------------------------------------------------------- #
# 会議の配信物
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="青木 一郎", officer_role="理事長",
                           line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="江口 隆之", officer_role="専務理事",
                           line_user_id="U2"))
    r.upsert_member(Member(member_id="m3", name="杉山 正人"))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.五役会, title="第2回五役会", datetime_start=START,
        datetime_end=datetime(2026, 11, 5, 21, 0), location="会館 or オンライン",
        meet_url="https://meet.google.com/abc-defg-hij",
        target_scope=TargetScope(kind=TargetScopeKind.officer, value=["理事長", "専務理事"])))
    r.save_poll(SchedulePoll(poll_id="poll1", title="第2回五役会", event_type=EventType.五役会,
                             group_id="G", event_id="ev1"))
    r.upsert_proposal(Proposal(proposal_id="p1", title="予算（案）", meeting_poll_id="poll1",
                               owner_member_id="m1", storage_uri="https://drive.google.com/x"))
    r.save_line_group(LineGroup(group_id="Cgoyaku", group_name="五役のグループ",
                                joined_at=datetime(2026, 10, 1)))
    r.save_settings(Settings(line_groups={"goyaku": "Cgoyaku"}))
    set_repo(r)
    sent = []
    monkeypatch.setattr(meeting_dispatch.line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs)) or True)
    r.sent = sent
    yield r
    set_repo(None)


def test_context_uses_event_and_ledger(repo):
    ctx = meeting_dispatch.context(repo, repo.get_event("ev1"),
                                   meeting_dispatch.get_dispatch(repo, "ev1"),
                                   datetime(2026, 11, 3, 9))
    assert ctx["event.date"] == "11月5日(木)" and ctx["event.time"] == "19:00〜21:00"
    assert ctx["online"] and not ctx["agenda"] and not ctx["late"]
    assert ctx["proposals"] == [{"no": "第1号議案", "title": "予算（案）",
                                 "url": "https://drive.google.com/x", "presenter": "青木 一郎"}]
    late = meeting_dispatch.context(repo, repo.get_event("ev1"),
                                    meeting_dispatch.get_dispatch(repo, "ev1"), START)
    assert late["late"]


def test_dispatch_view_and_send_to_group(repo):
    res = client.put("/api/events/ev1/dispatch",
                     json={"agenda_url": "https://docs.google.com/document/d/A/edit"})
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["template_id"] == "line_agenda"
    assert view["destination"] == {"mode": "group", "group_id": "Cgoyaku",
                                   "group_name": "五役のグループ", "targets": 2, "linked": 2}
    assert "https://docs.google.com/document/d/A/edit" in view["preview"]
    assert "オンライン：https://meet.google.com/abc-defg-hij" in view["preview"]

    res = client.post("/api/events/ev1/dispatch/line",
                      json={"template_id": "line_agenda", "expected_group_id": "Cgoyaku"})
    assert res.status_code == 200, res.text
    to, msgs = repo.sent[0]
    assert to == "Cgoyaku"
    payload = msgs[0].to_dict()
    assert payload["type"] == "textV2" and payload["text"].startswith("{everyone}\n")
    assert payload["substitution"]["everyone"]["mentionee"]["type"] == "all"
    dispatch = repo.get_meeting_dispatch("ev1")
    assert dispatch.line.sent_at and dispatch.line.group_name == "五役のグループ"
    assert len(dispatch.history) == 1
    assert any(a.action == "dispatch.line" for a in repo.list_audit())


def test_send_refuses_when_destination_changed(repo):
    res = client.post("/api/events/ev1/dispatch/line",
                      json={"template_id": "line_agenda", "expected_group_id": "Cother"})
    assert res.status_code == 409 and repo.sent == []


def test_braces_are_escaped_for_mentions(repo):
    template = notice_template.get_template(repo, "line_agenda").model_copy(
        update={"body": "{{event.title}} {x}"})
    msg = meeting_dispatch.line_message(template, notice_template.render(template.body, {
        "event.title": "会議"}))
    assert msg.text == "{everyone}\n会議 {{x}}"


def test_individual_send_when_no_group(repo):
    repo.save_settings(Settings())
    view = client.get("/api/events/ev1/dispatch").json()
    assert view["destination"]["mode"] == "individual"
    res = client.post("/api/events/ev1/dispatch/line",
                      json={"template_id": "line_agenda", "expected_group_id": None})
    assert res.status_code == 200, res.text
    assert sorted(to for to, _ in repo.sent) == ["U1", "U2"]
    assert repo.get_meeting_dispatch("ev1").line.recipients == 2


def test_template_crud_preview_and_reset(repo):
    listed = client.get("/api/notice-templates").json()
    assert "event.title" in listed["fields"] and "proposals" in listed["lists"]
    bad = client.put("/api/notice-templates/line_agenda",
                     json={"name": "x", "body": "{{event.titel}}"})
    assert bad.status_code == 400 and "event.titel" in bad.json()["detail"]
    ok = client.put("/api/notice-templates/line_agenda",
                    json={"name": "うちの文面", "body": "{{event.title}}です", "mention": "all",
                          "is_default": True})
    assert ok.status_code == 200 and ok.json()["builtin"]
    assert client.get("/api/events/ev1/dispatch").json()["preview"] == "第2回五役会です"
    created = client.post("/api/notice-templates",
                          json={"name": "理事会用", "body": "{{event.date_wareki}}",
                                "event_types": ["理事会"]}).json()
    assert created["template_id"].startswith("tpl_") and not created["builtin"]
    # 五役会には理事会用のテンプレートは出ない
    names = [t["name"] for t in client.get("/api/events/ev1/dispatch").json()["templates"]]
    assert "理事会用" not in names
    preview = client.post("/api/notice-templates/preview",
                          json={"body": "{{event.date_wareki}}", "event_id": "ev1"}).json()
    assert preview["text"] == "令和8年11月5日(木)"
    sample = client.post("/api/notice-templates/preview", json={"body": "{{event.title}}"})
    assert sample.json()["text"]
    reset = client.post("/api/notice-templates/line_agenda/reset")
    assert reset.status_code == 200
    assert notice_template.get_template(repo, "line_agenda").name == "アジェンダの配信（LINE）"
    assert client.post("/api/notice-templates/tpl_x/reset").status_code == 404


def test_url_must_be_https(repo):
    res = client.put("/api/events/ev1/dispatch", json={"agenda_url": "javascript:alert(1)"})
    assert res.status_code == 400
