"""InMemoryRepository のユニットテスト。"""
from datetime import datetime, timedelta

from app.models import (
    Attendance,
    AttendanceStatus,
    DeliveryLog,
    DeliveryResult,
    Event,
    EventStatus,
    EventType,
    InviteCode,
    Member,
    MemberStatus,
    ReminderPolicy,
    ReminderStage,
    Settings,
)
from app.repository import InMemoryRepository


def make_event(event_id="e1", status=EventStatus.open) -> Event:
    return Event(
        event_id=event_id,
        type=EventType.例会,
        title="例会",
        datetime_start=datetime(2026, 6, 10, 19, 0),
        status=status,
    )


def test_member_crud_and_line_lookup():
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="A", line_user_id="U1"))
    repo.upsert_member(Member(member_id="m2", name="B", status=MemberStatus.inactive))

    assert repo.get_member("m1").name == "A"
    assert repo.get_member("zzz") is None
    assert repo.get_member_by_line_user_id("U1").member_id == "m1"
    assert repo.get_member_by_line_user_id("U_none") is None
    assert len(repo.list_members()) == 2
    assert len(repo.list_members(active_only=True)) == 1


def test_repository_isolation():
    """返却オブジェクトを書き換えても内部状態は変わらない。"""
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="A"))
    got = repo.get_member("m1")
    got.name = "CHANGED"
    assert repo.get_member("m1").name == "A"


def test_invite_code_crud():
    repo = InMemoryRepository()
    code = InviteCode(code="ABC123", member_id="m1", expires_at=datetime(2026, 12, 31))
    repo.save_invite_code(code)
    assert repo.get_invite_code("ABC123").member_id == "m1"
    assert repo.get_invite_code("NOPE") is None


def test_event_crud_and_status_filter():
    repo = InMemoryRepository()
    repo.upsert_event(make_event("e1", EventStatus.open))
    repo.upsert_event(make_event("e2", EventStatus.draft))
    assert len(repo.list_events()) == 2
    assert [e.event_id for e in repo.list_events(status=EventStatus.open)] == ["e1"]


def test_attendance_crud():
    repo = InMemoryRepository()
    repo.upsert_attendance(Attendance(event_id="e1", member_id="m1", status=AttendanceStatus.出席))
    repo.upsert_attendance(Attendance(event_id="e1", member_id="m2", status=AttendanceStatus.欠席))
    repo.upsert_attendance(Attendance(event_id="e2", member_id="m1"))
    assert repo.get_attendance("e1", "m1").status == AttendanceStatus.出席
    assert len(repo.list_attendances("e1")) == 2
    assert len(repo.list_attendances("e2")) == 1


def test_policy_and_settings():
    repo = InMemoryRepository()
    # デフォルト settings
    assert repo.get_settings().kill_switch is False
    repo.save_settings(Settings(kill_switch=True))
    assert repo.get_settings().kill_switch is True

    policy = ReminderPolicy(
        policy_id="rp1",
        event_type=EventType.例会,
        stages=[ReminderStage(name="依頼", offset_minutes=-7 * 24 * 60)],
    )
    repo.upsert_policy(policy)
    assert repo.get_policy("rp1").stages[0].name == "依頼"
    assert repo.get_policy("none") is None


def test_delivery_job_and_logs():
    repo = InMemoryRepository()
    now = datetime(2026, 6, 1, 9, 0)
    repo.save_delivery_log(
        DeliveryLog(log_id="l1", job_id="j1", member_id="m1", content="x",
                    result=DeliveryResult.ok, sent_at=now)
    )
    repo.save_delivery_log(
        DeliveryLog(log_id="l2", job_id="j2", member_id="m2", content="y",
                    result=DeliveryResult.failed, sent_at=now + timedelta(minutes=1))
    )
    assert len(repo.list_delivery_logs()) == 2
    assert len(repo.list_delivery_logs(job_id="j1")) == 1
