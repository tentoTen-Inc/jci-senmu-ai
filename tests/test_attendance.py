"""イベント対象者解決・出欠記録・集計のユニットテスト。"""
from datetime import datetime

from app.attendance import aggregate, record_attendance
from app.events import resolve_targets
from app.models import (
    AttendanceStatus,
    Event,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository

NOW = datetime(2026, 6, 20, 10, 0)


def make_member(mid, committee=None, officer=None, line=True):
    return Member(
        member_id=mid,
        name=mid,
        line_user_id=f"U_{mid}" if line else None,
        committee=committee,
        officer_role=officer,
    )


def base_event(scope: TargetScope) -> Event:
    return Event(
        event_id="e1",
        type=EventType.例会,
        title="6月例会",
        datetime_start=datetime(2026, 6, 25, 19, 0),
        location="会館",
        target_scope=scope,
    )


def setup_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.upsert_member(make_member("m1", committee="総務委員会"))
    repo.upsert_member(make_member("m2", committee="コト創り委員会", officer="副理事長"))
    repo.upsert_member(make_member("m3", committee="総務委員会", line=False))  # 未連携→対象外
    return repo


def test_resolve_targets_all_excludes_unlinked():
    repo = setup_repo()
    repo.upsert_event(base_event(TargetScope(kind=TargetScopeKind.all)))
    targets = resolve_targets(repo, repo.get_event("e1"))
    assert {m.member_id for m in targets} == {"m1", "m2"}


def test_resolve_targets_by_committee():
    repo = setup_repo()
    ev = base_event(TargetScope(kind=TargetScopeKind.committee, value=["総務委員会"]))
    repo.upsert_event(ev)
    targets = resolve_targets(repo, ev)
    assert {m.member_id for m in targets} == {"m1"}


def test_resolve_targets_by_officer():
    repo = setup_repo()
    ev = base_event(TargetScope(kind=TargetScopeKind.officer, value=["副理事長"]))
    repo.upsert_event(ev)
    assert {m.member_id for m in resolve_targets(repo, ev)} == {"m2"}


def test_record_attendance_history_on_change():
    repo = setup_repo()
    repo.upsert_event(base_event(TargetScope(kind=TargetScopeKind.all)))
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=NOW)
    att = record_attendance(repo, "e1", "m1", AttendanceStatus.欠席, now=NOW)
    assert att.status == AttendanceStatus.欠席
    assert len(att.history) == 2


def test_aggregate_counts_and_rate():
    repo = setup_repo()
    repo.upsert_event(base_event(TargetScope(kind=TargetScopeKind.all)))  # 対象 m1,m2
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=NOW)
    # m2 未回答のまま
    summary = aggregate(repo, "e1")
    assert summary.total_targets == 2
    assert summary.answered == 1
    assert summary.unanswered == 1
    assert summary.unanswered_member_ids == ["m2"]
    assert summary.counts["出席"] == 1
    assert summary.counts["未回答"] == 1
    assert summary.attendance_rate == 0.5


def test_aggregate_web_attendance_counts_as_present():
    repo = setup_repo()
    repo.upsert_event(base_event(TargetScope(kind=TargetScopeKind.all)))
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=NOW)
    record_attendance(repo, "e1", "m2", AttendanceStatus.WEB出席, now=NOW)
    summary = aggregate(repo, "e1")
    assert summary.attendance_rate == 1.0
