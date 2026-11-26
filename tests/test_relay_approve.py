"""周知の転送 R-2: 判定・確認カード・承認・転送（docs/line-relay-design.md §3-4）。"""
import json
from datetime import datetime, timedelta

import pytest
from linebot.v3.messaging import FlexMessage, TextMessage, TextMessageV2

from app import line_link, line_push, llm, main, relay
from app.deps import set_repo
from app.models import (
    ExternalNotice,
    LineGroup,
    Member,
    NoticeAttachment,
    NoticeRelay,
    ScheduledPostStatus,
)
from app.repository import InMemoryRepository

T0 = datetime(2026, 11, 25, 16, 40)
BODY = "本会の運動方針動画です\nhttps://example.com/v\n\n各LOMメンバーに展開お願いします"


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="sed", name="専務 一郎", line_user_id="USED",
                           officer_role="専務理事"))
    r.upsert_member(Member(member_id="m2", name="会員 二郎", line_user_id="U2"))
    r.save_line_group(LineGroup(group_id="GB", group_name="ブロック", relay_source=True,
                                relay_label="〇〇ブロック協議会"))
    r.save_line_group(LineGroup(group_id="GALL", group_name="LOM 会員全員"))
    settings = r.get_settings()
    settings.line_groups = {"all": "GALL"}
    r.save_settings(settings)
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def pushed(monkeypatch):
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda to, messages: sent.append((to, messages)) or True)
    return sent


@pytest.fixture
def ai(monkeypatch):
    """AI の判定を差し替える。answers に kind を積むと順に返す（"fail" は失敗）。"""
    answers: list[str] = []

    def generate(prompt, **kw):
        kind = answers.pop(0) if answers else "share"
        if kind == "fail":
            raise RuntimeError("boom")
        return llm.Generation(text=json.dumps({
            "kind": kind, "summary": "運動方針動画の視聴とアンケート回答のお願い",
            "deadline": "2026-12-10"}), input_tokens=10, output_tokens=5)

    monkeypatch.setattr(llm, "generate_relay_judgement", generate)
    return answers


def _notice(repo, body=BODY, notice_id="n1", attachments=None) -> ExternalNotice:
    n = ExternalNotice(notice_id=notice_id, source="line_group", source_ref=f"GB:{notice_id}",
                       received_at=T0, subject=body.splitlines()[0] if body else "（本文なし）",
                       body_text=body, attachments=attachments or [],
                       relay=NoticeRelay(group_id="GB", message_ids=[notice_id]))
    repo.upsert_notice(n)
    return n


def _flex_text(message: FlexMessage) -> str:
    return json.dumps(message.contents.to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 判定と確認カード
# --------------------------------------------------------------------------- #
def test_share_sends_card_to_sed(repo, pushed, ai):
    _notice(repo)
    result = relay.process(repo, T0)
    assert result["judged"] == 1 and result["cards"] == 1

    [(to, [card])] = pushed
    assert to == "USED" and isinstance(card, FlexMessage)
    text = _flex_text(card)
    assert "【転送の確認】〇〇ブロック協議会のグループから" in text
    assert "運動方針動画の視聴とアンケート回答のお願い" in text
    assert "送信先: LOM 会員全員（@All）" in text and "期限: 12月10日" in text
    assert "【〇〇ブロック協議会より】" in text and "https://example.com/v" in text
    assert "rly|send|n1" in text and "rly|no|n1" in text

    notice = repo.get_notice("n1")
    assert notice.relay.kind == "share" and notice.relay.card_sent_at == T0
    assert any(a.action == "notice.relay.card" for a in repo.list_audit())
    # 次の回は二度送らない
    relay.process(repo, T0 + timedelta(minutes=5))
    assert len(pushed) == 1


def test_card_waits_until_sed_is_linked(repo, pushed, ai):
    sed = repo.get_member("sed")
    repo.upsert_member(sed.model_copy(update={"line_user_id": None}))
    _notice(repo)
    assert relay.process(repo, T0)["cards"] == 0
    assert repo.get_notice("n1").relay.card_sent_at is None
    repo.upsert_member(sed)
    assert relay.process(repo, T0 + timedelta(minutes=5))["cards"] == 1


def test_officer_only_notifies(repo, pushed, ai):
    ai.append("officer")
    _notice(repo, body="各LOM理事長の皆様\n出欠を12/1までにご回答ください")
    assert relay.process(repo, T0)["officer"] == 1
    [(to, [message])] = pushed
    assert to == "USED" and isinstance(message, TextMessage)
    assert message.text.startswith("【役員あての連絡】〇〇ブロック協議会のグループに届きました")
    assert "rly|" not in message.text


def test_other_is_only_recorded(repo, pushed, ai):
    ai.append("other")
    _notice(repo, body="ありがとうございました！")
    relay.process(repo, T0)
    assert pushed == [] and repo.get_notice("n1").relay.kind == "other"


def test_judge_failures_fall_back_to_share(repo, pushed, ai):
    ai.extend(["fail", "fail", "fail"])
    _notice(repo)
    relay.process(repo, T0)
    assert repo.get_notice("n1").relay.judge_tries == 1 and pushed == []
    relay.process(repo, T0 + timedelta(minutes=5))  # 30分たつまでやり直さない
    assert repo.get_notice("n1").relay.judge_tries == 1
    relay.process(repo, T0 + timedelta(minutes=31))
    relay.process(repo, T0 + timedelta(minutes=62))
    notice = repo.get_notice("n1")
    assert notice.relay.kind == "share" and "判定に失敗" in notice.relay.summary
    assert len(pushed) == 1  # 取りこぼさず専務に確認してもらう


def test_quiet_hours_wait_until_morning(repo, pushed, ai):
    _notice(repo)
    night = datetime(2026, 11, 25, 22, 0)
    assert relay.process(repo, night)["cards"] == 0
    assert repo.get_notice("n1").relay.kind == "share" and pushed == []
    assert relay.process(repo, datetime(2026, 11, 26, 8, 0))["cards"] == 1


def test_unanswered_card_expires_after_7_days(repo, pushed, ai):
    _notice(repo)
    relay.process(repo, T0)
    assert relay.process(repo, T0 + timedelta(days=6))["expired"] == 0
    assert relay.process(repo, T0 + timedelta(days=7))["expired"] == 1
    member = repo.get_member("sed")
    [reply] = relay.handle_postback(repo, member, "rly|send|n1", T0 + timedelta(days=7))
    assert "期限" in reply.text


# --------------------------------------------------------------------------- #
# ボタン
# --------------------------------------------------------------------------- #
def _ready(repo, pushed, ai, **kw):
    _notice(repo, **kw)
    relay.process(repo, T0)
    pushed.clear()
    return repo.get_member("sed")


def test_two_step_approval_sends_to_all_group(repo, pushed, ai):
    sed = _ready(repo, pushed, ai)
    [confirm] = relay.handle_postback(repo, sed, "rly|send|n1", T0)
    assert isinstance(confirm, FlexMessage)
    assert confirm.alt_text == ("「LOM 会員全員」に @All で送ります。"
                                "グループへの送信は取り消せません。")
    assert "rly|go|n1" in _flex_text(confirm) and pushed == []  # まだ送らない

    [reply] = relay.handle_postback(repo, sed, "rly|go|n1", T0)
    assert reply.text == "「LOM 会員全員」に転送しました。"
    [(to, [message])] = pushed
    assert to == "GALL" and isinstance(message, TextMessageV2)
    assert message.text == "{everyone}\n【〇〇ブロック協議会より】\n" + BODY
    assert message.substitution["everyone"].mentionee.type == "all"

    notice = repo.get_notice("n1")
    assert notice.status == "delivered" and notice.relay.sent_at == T0
    assert notice.relay.approved_by == "sed" and notice.relay.target_group_id == "GALL"
    actions = {a.action for a in repo.list_audit()}
    assert {"notice.relay.approve", "notice.relay.sent"} <= actions

    # 二度押しは送らない
    [again] = relay.handle_postback(repo, sed, "rly|go|n1", T0)
    assert "すでに" in again.text and len(pushed) == 1


def test_only_sed_can_press(repo, pushed, ai):
    _ready(repo, pushed, ai)
    [reply] = relay.handle_postback(repo, repo.get_member("m2"), "rly|go|n1", T0)
    assert "専務理事だけ" in reply.text and pushed == []


def test_reject_archives(repo, pushed, ai):
    sed = _ready(repo, pushed, ai)
    [reply] = relay.handle_postback(repo, sed, "rly|no|n1", T0)
    assert "送らない" in reply.text
    assert repo.get_notice("n1").status == "archived"
    [later] = relay.handle_postback(repo, sed, "rly|go|n1", T0)
    assert "送らない" in later.text and pushed == []


def test_cancel_does_nothing(repo, pushed, ai):
    sed = _ready(repo, pushed, ai)
    [reply] = relay.handle_postback(repo, sed, "rly|cancel|n1", T0)
    assert "やめました" in reply.text and pushed == []
    assert repo.get_notice("n1").relay.approved_at is None


def test_approval_in_quiet_hours_sends_in_the_morning(repo, pushed, ai):
    sed = _ready(repo, pushed, ai)
    night = datetime(2026, 11, 25, 23, 0)
    [reply] = relay.handle_postback(repo, sed, "rly|go|n1", night)
    assert "11月26日 08:00" in reply.text and pushed == []
    [post] = repo.list_scheduled_posts()
    assert post.kind == "relay" and post.send_at == datetime(2026, 11, 26, 8, 0)

    line_link.send_due_posts(repo, datetime(2026, 11, 26, 8, 0))
    [(to, _)] = pushed
    assert to == "GALL"
    assert repo.list_scheduled_posts()[0].status == ScheduledPostStatus.sent
    assert repo.get_notice("n1").status == "delivered"


def test_target_group_must_be_set(repo, pushed, ai):
    sed = _ready(repo, pushed, ai)
    settings = repo.get_settings()
    settings.line_groups = {}
    repo.save_settings(settings)
    [reply] = relay.handle_postback(repo, sed, "rly|send|n1", T0)
    assert "転送先" in reply.text


def test_postback_is_routed_from_main(repo, pushed, ai):
    _ready(repo, pushed, ai)
    [reply] = main.handle_postback("USED", "rly|cancel|n1")
    assert "やめました" in reply.text


# --------------------------------------------------------------------------- #
# 転送する文面
# --------------------------------------------------------------------------- #
def test_long_body_is_split_without_changes(repo):
    body = "\n".join(f"{i:04d} " + "あ" * 90 for i in range(80))  # 約7,500字
    notice = _notice(repo, body=body)
    messages = relay.relay_messages(repo, notice)
    assert len(messages) == 2 and all(len(m.text) <= 5000 for m in messages)
    joined = messages[0].text.removeprefix("{everyone}\n") + "\n" + messages[1].text
    assert joined == "【〇〇ブロック協議会より】\n" + body


def test_braces_are_escaped_for_mention(repo):
    notice = _notice(repo, body="締切は{12/1}です")
    [message] = relay.relay_messages(repo, notice)
    assert message.text.endswith("締切は{{12/1}}です")


def test_attachments_get_signed_links(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agent.example")
    notice = _notice(repo, attachments=[
        NoticeAttachment(name="案内.pdf", storage_uri="gs://b/line/content/m9/案内.pdf"),
        NoticeAttachment(name="保存できなかった.pdf")])
    [message] = relay.relay_messages(repo, notice)
    assert "📎 案内.pdf\nhttps://agent.example/files/" in message.text
    assert message.text.endswith("📎 保存できなかった.pdf")
