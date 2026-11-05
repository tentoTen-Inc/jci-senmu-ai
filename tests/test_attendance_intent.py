"""チャットで届いた出欠の連絡の受付（F4-7）のテスト。"""
import json
from datetime import datetime

import pytest

from app import llm
from app.attendance_intent import (
    decode_answers,
    encode_answers,
    handle_confirm_postback,
    try_attendance_intent,
)
from app.member_menu import handle_member_text
from app.models import (
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    LateLeave,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 8, 10, 16, 0)


def answer(event_id="e1", status="欠席", late_leave=None, note=None) -> dict:
    return {"event_id": event_id, "status": status, "late_leave": late_leave, "note": note}


def intent_json(*answers, confident=True) -> str:
    return json.dumps({"answers": list(answers) or [answer()], "confident": confident},
                      ensure_ascii=False)


def fake_llm(monkeypatch, text: str, captured: dict | None = None):
    def fake(text_, events, today, **kw):
        if captured is not None:
            captured.update(text=text_, events=events, today=today)
        return llm.Generation(text=text, input_tokens=400, output_tokens=40)

    monkeypatch.setattr(llm, "generate_attendance_intent", fake)


@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="太郎", line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="花子", line_user_id="U2"))
    r.upsert_event(Event(
        event_id="e1", type=EventType.五役会, title="第2回五役会",
        datetime_start=datetime(2026, 8, 10, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    r.upsert_event(Event(
        event_id="e2", type=EventType.理事会, title="第2回理事会",
        datetime_start=datetime(2026, 8, 18, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    r.upsert_event(Event(
        event_id="e3", type=EventType.理事会, title="理事長だけの会議",
        datetime_start=datetime(2026, 8, 20, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.officer, value=["理事長"]),
        status=EventStatus.open,
    ))
    return r


def member(repo, mid="m1") -> Member:
    return repo.get_member(mid)


def test_confirms_before_registering(monkeypatch, repo):
    """読み取れても登録はしない。本人が押す［この内容で登録］を返す。"""
    captured = {}
    fake_llm(monkeypatch, intent_json(answer(note="出張のため")), captured)
    messages = try_attendance_intent(repo, member(repo), "本日の五役は欠席になりそうです", now=NOW)

    msg = messages[0]
    assert "よろしいですか" in msg.text and "8月10日 第2回五役会：欠席（出張のため）" in msg.text
    assert [i.action.label for i in msg.quick_reply.items] == ["この内容で登録", "ちがう"]
    assert repo.get_attendance("e1", "m1") is None
    # 候補は本人が対象の会議だけ。「本日」がわかるよう今日の日付も渡す
    assert "e1" in captured["events"] and "e3" not in captured["events"]
    assert captured["today"].startswith("2026-08-10(月)")
    logs = repo.list_inference_logs()
    assert [(x.kind, x.ok) for x in logs] == [("attendance_intent", True)]


def test_registers_several_meetings_when_confirmed(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json(
        answer("e1", "Web出席", note="音声のみの可能性あり"),
        answer("e2", "出席", late_leave="遅刻", note="シフト次第で再連絡")))
    messages = try_attendance_intent(
        repo, member(repo), "本日はWebで、理事会は今のところ遅れて出席します", now=NOW)
    data = messages[0].quick_reply.items[0].action.data

    reply = handle_confirm_postback(repo, member(repo), data, now=NOW)
    assert "登録しました" in reply[0].text
    first, second = repo.get_attendance("e1", "m1"), repo.get_attendance("e2", "m1")
    assert (first.status, first.late_leave, first.free_text) == (
        AttendanceStatus.WEB出席, LateLeave.全日程参加, "音声のみの可能性あり")
    assert (second.status, second.late_leave) == (AttendanceStatus.出席, LateLeave.遅刻)


def test_only_the_sender_can_confirm_in_group(monkeypatch, repo):
    """グループではほかの人もボタンを押せるので、押した人が本人でなければ登録しない。"""
    fake_llm(monkeypatch, intent_json())
    messages = try_attendance_intent(repo, member(repo), "欠席します", now=NOW, in_group=True)
    assert messages[0].text.startswith("太郎さん、")
    data = messages[0].quick_reply.items[0].action.data

    reply = handle_confirm_postback(repo, member(repo, "m2"), data, now=NOW)
    assert "太郎さんだけ" in reply[0].text
    assert repo.get_attendance("e1", "m2") is None and repo.get_attendance("e1", "m1") is None


def test_meeting_that_just_started_is_a_candidate(monkeypatch, repo):
    """始まった直後の「やっぱり参加難しいです」も受け付ける。"""
    captured = {}
    fake_llm(monkeypatch, intent_json(), captured)
    later = datetime(2026, 8, 10, 19, 1)
    assert try_attendance_intent(repo, member(repo), "やっぱり参加難しいです", now=later)
    assert "e1" in captured["events"]


def test_postback_round_trip_and_long_notes():
    answers = [llm.AttendanceAnswer(event_id="e1", status="欠席", note="出張|のため;=")]
    member_id, decoded = decode_answers(encode_answers("m1", answers))
    assert member_id == "m1" and decoded[0].note == "出張 のため"
    many = [llm.AttendanceAnswer(event_id=f"event_{i:04d}", status="欠席", note="あ" * 30)
            for i in range(8)]
    data = encode_answers("m1", many)
    assert len(data) <= 300 and len(decode_answers(data)[1]) == 8  # メモを外して収める


def test_drops_invalid_answers(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json(
        answer("e99"), answer("e3"), answer("e1", "たぶん"), answer("e2", "欠席", "遅刻")))
    messages = try_attendance_intent(repo, member(repo), "欠席で", now=NOW)
    assert "第2回理事会：欠席" in messages[0].text  # 欠席の遅刻は外す
    assert "e99" not in messages[0].text and "理事長だけ" not in messages[0].text

    fake_llm(monkeypatch, intent_json(answer("e99")))
    assert try_attendance_intent(repo, member(repo), "欠席で", now=NOW) is None


def test_not_confident_falls_through(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json(confident=False))
    assert try_attendance_intent(repo, member(repo), "出欠どうしようかな", now=NOW) is None


def test_llm_failure_falls_through_and_is_recorded(monkeypatch, repo):
    def boom(*a, **kw):
        raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(llm, "generate_attendance_intent", boom)
    assert try_attendance_intent(repo, member(repo), "来週は欠席で", now=NOW) is None
    logs = repo.list_inference_logs()
    assert len(logs) == 1 and logs[0].ok is False


def test_no_candidate_events_skips_llm(monkeypatch, repo):
    monkeypatch.setattr(llm, "generate_attendance_intent",
                        lambda *a, **kw: pytest.fail("候補がなければ AI を呼ばない"))
    assert try_attendance_intent(repo, member(repo), "欠席で", now=datetime(2027, 1, 1)) is None
    assert repo.list_inference_logs() == []


def test_cancelled_meeting_is_not_registered(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json())
    data = try_attendance_intent(repo, member(repo), "欠席", now=NOW)[0].quick_reply.items[0] \
        .action.data
    event = repo.get_event("e1")
    repo.upsert_event(event.model_copy(update={"status": EventStatus.cancelled}))
    reply = handle_confirm_postback(repo, member(repo), data, now=NOW)
    assert "登録しませんでした" in reply[0].text and repo.get_attendance("e1", "m1") is None


# --------------------------------------------------------------------------- #
# チャットの振り分け
# --------------------------------------------------------------------------- #
def test_routing_attendance_message_returns_confirmation(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json())
    monkeypatch.setattr("app.member_menu.answer_member_question",
                        lambda *a, **kw: pytest.fail("出欠の連絡は質問として答えない"))
    messages = handle_member_text(repo, member(repo), "本日の五役ですが\n欠席になりそうです",
                                  now=NOW)
    assert messages[0].quick_reply.items[0].action.data.startswith("att_nl|m1|e1=欠席")
    assert repo.get_attendance("e1", "m1") is None


def test_routing_other_text_does_not_call_llm(monkeypatch, repo):
    monkeypatch.setattr(llm, "generate_attendance_intent",
                        lambda *a, **kw: pytest.fail("出欠の言葉がなければ読み取らない"))
    monkeypatch.setattr("app.member_menu.answer_member_question", lambda *a, **kw: None)
    handle_member_text(repo, member(repo), "来月の予算書はどこにありますか", now=NOW)


def test_routing_menu_action_keeps_existing_prompt(monkeypatch, repo):
    """リッチメニューの「出欠を回答」は従来どおり出欠依頼を返す（AI を呼ばない）。"""
    monkeypatch.setattr(llm, "generate_attendance_intent",
                        lambda *a, **kw: pytest.fail("メニューでは読み取らない"))
    messages = handle_member_text(repo, member(repo), "menu|出欠を回答", now=NOW)
    assert "第2回五役会" in messages[0].text
    assert [i.action.label for i in messages[0].quick_reply.items] == ["出席", "Web出席", "欠席"]


def test_routing_falls_back_to_button_when_not_understood(monkeypatch, repo):
    fake_llm(monkeypatch, intent_json(confident=False))
    monkeypatch.setattr("app.member_menu.answer_member_question", lambda *a, **kw: None)
    messages = handle_member_text(repo, member(repo), "来週の例会は欠席でお願いします", now=NOW)
    assert messages[0].quick_reply.items[0].action.data == "menu|予定と出欠"
