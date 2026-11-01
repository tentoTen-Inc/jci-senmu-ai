"""依頼→回答→催促→集計→サマリ の一連を結合テスト（InMemory + フェイク送信）。"""
from datetime import datetime, timedelta

from app.attendance import record_attendance
from app.delivery import execute_delivery
from app.models import (
    AttendanceStatus,
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.reminders import EXAMPLE_MEETING_POLICY, plan_reminders
from app.repository import InMemoryRepository
from app.summary import build_summary_text, close_event, officer_member_ids

DEADLINE = datetime(2026, 6, 25, 23, 59)


def setup_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="理事長太郎", line_user_id="U1",
                             officer_role="理事長"))
    repo.upsert_member(Member(member_id="m2", name="委員次郎", line_user_id="U2",
                             committee="総務委員会"))
    repo.upsert_member(Member(member_id="m3", name="委員三郎", line_user_id="U3",
                             committee="総務委員会"))
    repo.upsert_policy(EXAMPLE_MEETING_POLICY)
    repo.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="6月例会",
        datetime_start=datetime(2026, 6, 25, 19, 0),
        attendance_deadline=DEADLINE,
        target_scope=TargetScope(kind=TargetScopeKind.all),
        reminder_policy_id="rp_例会_default",
        status=EventStatus.open,
    ))
    return repo


def test_full_attendance_flow():
    repo = setup_repo()
    sent_log: list[tuple[str, str]] = []

    def sender(mid):
        sent_log.append(("send", mid))
        return True

    # 1) 7日前: 依頼を全員へ（締切は6/25 23:59なので7日前発火は6/18 23:59、翌昼で確実に過ぎる）
    t_req = datetime(2026, 6, 19, 12, 0)
    jobs = plan_reminders(repo, t_req)
    assert [j.stage for j in jobs] == ["依頼"]
    report = execute_delivery(repo, jobs[0], "出欠をご回答ください", now=t_req, sender=sender)
    assert report.sent == 3

    # 2) m1, m2 が回答（m3 は未回答のまま）
    record_attendance(repo, "e1", "m1", AttendanceStatus.出席, now=t_req + timedelta(hours=1))
    record_attendance(repo, "e1", "m2", AttendanceStatus.欠席, now=t_req + timedelta(hours=2))

    # 3) 3日前: 未回答(m3)のみへリマインド（リマインド発火は6/22 23:59、翌昼で過ぎる）
    t_remind = datetime(2026, 6, 23, 12, 0)
    jobs2 = plan_reminders(repo, t_remind)
    assert [j.stage for j in jobs2] == ["リマインド"]
    assert jobs2[0].targets == ["m3"]

    # 4) サマリ
    text = build_summary_text(repo, "e1")
    assert "6月例会" in text
    assert "委員三郎" in text  # 未回答者名
    assert "出席率 33%" in text  # 出席1/対象3

    # 5) クローズ → 五役通知先は理事長のみ
    assert close_event(repo, "e1", now=t_remind) is True
    assert repo.get_event("e1").status == EventStatus.closed
    assert officer_member_ids(repo) == ["m1"]
