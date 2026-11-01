"""ランタイム結線（tick・Push送信）のユニットテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_push, main
from app.deps import set_repo
from app.models import (
    Event,
    EventStatus,
    EventType,
    Member,
    TargetScope,
    TargetScopeKind,
)
from app.reminders import EXAMPLE_MEETING_POLICY
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
DEADLINE = datetime(2026, 6, 25, 23, 59)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    for mid in ("m1", "m2"):
        r.upsert_member(Member(member_id=mid, name=mid, line_user_id=f"U_{mid}"))
    r.upsert_policy(EXAMPLE_MEETING_POLICY)
    set_repo(r)
    yield r
    set_repo(None)


def open_event(r):
    r.upsert_event(Event(
        event_id="e1", type=EventType.例会, title="6月例会",
        datetime_start=datetime(2026, 6, 25, 19, 0),
        attendance_deadline=DEADLINE,
        target_scope=TargetScope(kind=TargetScopeKind.all),
        reminder_policy_id="rp_例会_default",
        status=EventStatus.open,
    ))


def test_push_skips_without_token(monkeypatch):
    # トークン未設定（PLACEHOLDER）では False を返し例外を出さない
    monkeypatch.setattr("app.config.line_channel_access_token", lambda: "PLACEHOLDER_SET_ME")
    assert line_push.push_text("U_x", "hello") is False


def test_tick_pushes_to_targets(monkeypatch, repo):
    open_event(repo)
    pushed: list[str] = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda uid, msgs: pushed.append(uid) or True)

    # 締切7日前を過ぎた日時に固定
    monkeypatch.setattr(main, "datetime", _FixedDatetime(datetime(2026, 6, 19, 12, 0)))
    res = client.post("/tasks/tick")
    assert res.status_code == 200
    body = res.json()
    assert body["planned"] == 1
    assert body["results"][0]["sent"] == 2
    assert set(pushed) == {"U_m1", "U_m2"}


def test_tick_no_due_stage(monkeypatch, repo):
    open_event(repo)
    monkeypatch.setattr(line_push, "push_messages", lambda uid, msgs: True)
    # 締切より前すぎて発火なし
    monkeypatch.setattr(main, "datetime", _FixedDatetime(datetime(2026, 6, 1, 12, 0)))
    res = client.post("/tasks/tick")
    assert res.json()["planned"] == 0


class _FixedDatetime:
    """datetime.now() のみ固定するラッパ。"""

    def __init__(self, fixed):
        self._fixed = fixed

    def now(self):
        return self._fixed


def test_tick_runs_once_per_hour(monkeypatch, repo):
    """同じ時間帯の2回目の tick（手動実行・再試行）は何も送らない。"""
    open_event(repo)
    pushed: list[str] = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda uid, msgs: pushed.append(uid) or True)
    monkeypatch.setattr(main, "datetime", _FixedDatetime(datetime(2026, 6, 19, 12, 0)))
    assert client.post("/tasks/tick").json()["planned"] == 1
    monkeypatch.setattr(main, "datetime", _FixedDatetime(datetime(2026, 6, 19, 12, 40)))
    second = client.post("/tasks/tick").json()
    assert second == {"skipped": "already_ran", "slot": "tick:2026061912"}
    assert len(pushed) == 2
