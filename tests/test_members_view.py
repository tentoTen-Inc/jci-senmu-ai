"""会員管理ビュー(招待状況・出欠履歴)のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.attendance import record_attendance
from app.deps import set_repo
from app.invite import issue_invite
from app.members_view import attendance_history, invite_status
from app.models import (
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 6, 20, 10, 0)


def build_repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="A", line_user_id="U1", committee="総務委員会"))
    r.upsert_member(Member(member_id="m2", name="B"))  # 未連携
    return r


def test_invite_status_flags():
    repo = build_repo()
    issue_invite(repo, "m2", now=NOW)  # m2に未使用コード発行
    rows = {r.member_id: r for r in invite_status(repo, now=NOW)}
    assert rows["m1"].linked is True
    assert rows["m1"].invite_issued is False
    assert rows["m2"].linked is False
    assert rows["m2"].invite_issued is True
    assert rows["m2"].invite_active is True
    assert rows["m2"].invite_used is False


def test_attendance_history_rate():
    repo = build_repo()
    for eid, dt in [("e1", datetime(2026, 5, 1, 19)), ("e2", datetime(2026, 6, 1, 19))]:
        repo.upsert_event(Event(
            event_id=eid, type=EventType.例会, title=eid,
            datetime_start=dt, target_scope=TargetScope(kind=TargetScopeKind.all),
            status=EventStatus.closed,
        ))
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=NOW)
    record_attendance(repo, "e2", "m1", AttendanceStatus.欠席, now=NOW)
    h = attendance_history(repo, "m1")
    assert h.counted == 2
    assert h.present == 1
    assert h.answered == 2
    assert h.attendance_rate == 0.5
    assert h.items[0].datetime_start >= h.items[1].datetime_start  # 新しい順


@pytest.fixture
def _wire():
    repo = build_repo()
    set_repo(repo)
    yield repo
    set_repo(None)


def test_endpoints(_wire):
    assert client.get("/api/members/invite-status").status_code == 200
    assert client.get("/api/members/m1/attendance-history").status_code == 200
    assert client.get("/api/members/zzz/attendance-history").status_code == 404
