"""ガードレールのユニットテスト。"""
from datetime import datetime

from app.guardrails import (
    count_sends_today,
    decide_send,
    is_quiet_hours,
    next_send_time,
    sanity_check,
)
from app.models import DeliveryLog, DeliveryResult, QuietHours, Settings
from app.repository import InMemoryRepository


def settings(**kw) -> Settings:
    return Settings(**kw)


def test_quiet_hours_overnight_window():
    s = Settings(quiet_hours=QuietHours(start="21:00", end="08:00"))
    assert is_quiet_hours(datetime(2026, 6, 20, 23, 0), s) is True
    assert is_quiet_hours(datetime(2026, 6, 20, 7, 0), s) is True
    assert is_quiet_hours(datetime(2026, 6, 20, 12, 0), s) is False


def test_next_send_time_defers_to_morning():
    s = Settings(quiet_hours=QuietHours(start="21:00", end="08:00"))
    # 23時 → 翌朝8時
    nxt = next_send_time(datetime(2026, 6, 20, 23, 0), s)
    assert nxt == datetime(2026, 6, 21, 8, 0)
    # 早朝7時 → 当日8時
    nxt2 = next_send_time(datetime(2026, 6, 20, 7, 0), s)
    assert nxt2 == datetime(2026, 6, 20, 8, 0)
    # 日中はそのまま
    assert next_send_time(datetime(2026, 6, 20, 12, 0), s) == datetime(2026, 6, 20, 12, 0)


def test_sanity_check():
    assert sanity_check(["m1"], "本文", 10) == []
    assert "empty_body" in sanity_check(["m1"], "  ", 10)
    assert "unresolved_placeholder" in sanity_check(["m1"], "こんにちは {{name}}", 10)
    assert "too_many_targets" in sanity_check(["m1", "m2", "m3"], "x", 2)
    assert "no_targets" in sanity_check([], "x", 10)


def _log(lid, mid, result, at):
    return DeliveryLog(
        log_id=lid, job_id="j", member_id=mid, content="x", result=result, sent_at=at
    )


def test_count_sends_today():
    repo = InMemoryRepository()
    now = datetime(2026, 6, 20, 15, 0)
    repo.save_delivery_log(_log("l1", "m1", DeliveryResult.ok, datetime(2026, 6, 20, 9, 0)))
    repo.save_delivery_log(_log("l2", "m1", DeliveryResult.ok, datetime(2026, 6, 19, 9, 0)))
    repo.save_delivery_log(_log("l3", "m1", DeliveryResult.blocked, now))
    assert count_sends_today(repo, "m1", now) == 1  # 当日のok のみ


def test_decide_send_kill_switch():
    repo = InMemoryRepository()
    repo.save_settings(Settings(kill_switch=True))
    d = decide_send(repo, "m1", now=datetime(2026, 6, 20, 12, 0), settings=repo.get_settings())
    assert d.send is False
    assert d.reason == "kill_switch"


def test_decide_send_quiet_hours_defers():
    repo = InMemoryRepository()
    s = Settings(quiet_hours=QuietHours(start="21:00", end="08:00"))
    d = decide_send(repo, "m1", now=datetime(2026, 6, 20, 23, 0), settings=s)
    assert d.send is False
    assert d.reason == "quiet_hours"
    assert d.defer_to == datetime(2026, 6, 21, 8, 0)


def test_decide_send_rate_limited():
    repo = InMemoryRepository()
    now = datetime(2026, 6, 20, 12, 0)
    s = Settings()  # per_member_per_day=3
    for i in range(3):
        repo.save_delivery_log(_log(f"l{i}", "m1", DeliveryResult.ok, now))
    d = decide_send(repo, "m1", now=now, settings=s)
    assert d.send is False
    assert d.reason == "rate_limited"


def test_decide_send_ok():
    repo = InMemoryRepository()
    d = decide_send(repo, "m1", now=datetime(2026, 6, 20, 12, 0), settings=Settings())
    assert d.send is True
