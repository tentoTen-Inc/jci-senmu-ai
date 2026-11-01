"""KPI(トレンド・overview)のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.attendance import record_attendance
from app.deps import set_repo
from app.kpi import attendance_trends, kpi_overview
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
    r.upsert_member(Member(member_id="m1", name="A", line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="B", line_user_id="U2"))
    for eid, dt in [("e1", datetime(2026, 5, 1, 19)), ("e2", datetime(2026, 6, 1, 19))]:
        r.upsert_event(Event(
            event_id=eid, type=EventType.例会, title=eid,
            datetime_start=dt, target_scope=TargetScope(kind=TargetScopeKind.all),
            status=EventStatus.closed,
        ))
    record_attendance(r, "e1", "m1", AttendanceStatus.出席, now=NOW)
    record_attendance(r, "e2", "m1", AttendanceStatus.出席, now=NOW)
    record_attendance(r, "e2", "m2", AttendanceStatus.WEB出席, now=NOW)
    return r


def test_trends_chronological():
    repo = build_repo()
    pts = attendance_trends(repo)
    assert [p.event_id for p in pts] == ["e1", "e2"]  # 古い順
    assert pts[0].attendance_rate == 0.5  # m1のみ出席/2名
    assert pts[1].attendance_rate == 1.0  # 2名とも出席


def test_overview():
    repo = build_repo()
    o = kpi_overview(repo, now=NOW)
    assert 0 <= o.avg_attendance_rate <= 1
    assert o.avg_attendance_rate == round((0.5 + 1.0) / 2, 3)


@pytest.fixture
def _wire():
    set_repo(build_repo())
    yield
    set_repo(None)


def test_kpi_endpoints(_wire):
    assert client.get("/api/kpi/trends").status_code == 200
    assert client.get("/api/kpi/overview").status_code == 200
