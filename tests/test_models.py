"""ドメインモデルのユニットテスト。"""
from datetime import datetime

from app.models import (
    Attendance,
    AttendanceStatus,
    Event,
    EventType,
    Member,
    MemberStatus,
    MemberType,
)


def test_member_defaults():
    m = Member(member_id="m1", name="猪苗代 太郎")
    assert m.lom_id == "lom"
    assert m.member_type == MemberType.regular
    assert m.status == MemberStatus.active
    assert m.contact.email is None


def test_member_is_deliverable():
    base = dict(member_id="m1", name="X", line_user_id="U1")
    assert Member(**base).is_deliverable is True
    # LINE未連携は配信対象外
    assert Member(member_id="m2", name="Y").is_deliverable is False
    # OBは配信対象外
    assert Member(**base, member_type=MemberType.ob).is_deliverable is False
    # 退会は配信対象外
    assert Member(**base, status=MemberStatus.inactive).is_deliverable is False
    # 外部監事は対象
    assert Member(**base, member_type=MemberType.external_auditor).is_deliverable is True


def test_attendance_doc_id_and_default_status():
    a = Attendance(event_id="e1", member_id="m1")
    assert a.doc_id == "e1_m1"
    assert a.status == AttendanceStatus.未回答


def test_serialization_round_trip():
    e = Event(
        event_id="e1",
        type=EventType.例会,
        title="6月例会",
        datetime_start=datetime(2026, 6, 10, 19, 0),
    )
    dumped = e.model_dump(mode="json")
    restored = Event.model_validate(dumped)
    assert restored == e
    assert dumped["type"] == "例会"


def test_attendance_status_values():
    assert AttendanceStatus.WEB出席.value == "Web出席"
    assert set(AttendanceStatus) >= {
        AttendanceStatus.出席,
        AttendanceStatus.WEB出席,
        AttendanceStatus.欠席,
        AttendanceStatus.委任,
        AttendanceStatus.未回答,
    }
