"""LINE出欠メッセージ・postback処理のユニットテスト。"""
from datetime import datetime

from app.line_messages import (
    ACTION_ATT,
    ACTION_LATE,
    ACTION_REASON,
    apply_postback,
    build_attendance_request,
    make_postback,
    parse_postback,
)
from app.models import (
    AbsenceReason,
    AttendanceStatus,
    Event,
    EventType,
    LateLeave,
    Member,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 6, 20, 10, 0)


def make_event() -> Event:
    return Event(
        event_id="e1",
        type=EventType.例会,
        title="6月例会",
        datetime_start=datetime(2026, 6, 25, 19, 0),
        location="会館",
    )


def setup():
    repo = InMemoryRepository()
    member = Member(member_id="m1", name="太郎", line_user_id="U_m1")
    repo.upsert_member(member)
    return repo, member


def test_postback_roundtrip():
    data = make_postback(ACTION_ATT, "e1", "出席")
    assert parse_postback(data) == (ACTION_ATT, "e1", "出席")


def test_build_attendance_request_has_three_buttons():
    msg = build_attendance_request(make_event())
    labels = [item.action.label for item in msg.quick_reply.items]
    assert labels == ["出席", "Web出席", "欠席"]
    assert "6月例会" in msg.text
    assert "会館" in msg.text


def test_apply_postback_present_then_lateleave():
    repo, member = setup()
    # 出席を押す → 遅刻早退質問が返る
    msgs = apply_postback(repo, member, make_postback(ACTION_ATT, "e1", "出席"), now=NOW)
    assert "遅刻" in msgs[0].text
    assert repo.get_attendance("e1", "m1").status == AttendanceStatus.出席
    # 全日程参加を押す
    msgs2 = apply_postback(
        repo, member, make_postback(ACTION_LATE, "e1", LateLeave.全日程参加.value), now=NOW
    )
    assert "受け付けました" in msgs2[0].text
    assert repo.get_attendance("e1", "m1").late_leave == LateLeave.全日程参加


def test_apply_postback_absent_then_reason():
    repo, member = setup()
    msgs = apply_postback(repo, member, make_postback(ACTION_ATT, "e1", "欠席"), now=NOW)
    assert "理由" in msgs[0].text
    assert repo.get_attendance("e1", "m1").status == AttendanceStatus.欠席
    msgs2 = apply_postback(
        repo, member, make_postback(ACTION_REASON, "e1", AbsenceReason.仕事.value), now=NOW
    )
    assert "承りました" in msgs2[0].text
    assert AbsenceReason.仕事 in repo.get_attendance("e1", "m1").absence_reasons


def test_apply_postback_web_attendance():
    repo, member = setup()
    apply_postback(repo, member, make_postback(ACTION_ATT, "e1", "Web出席"), now=NOW)
    assert repo.get_attendance("e1", "m1").status == AttendanceStatus.WEB出席
