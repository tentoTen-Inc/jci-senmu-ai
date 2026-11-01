"""日本時間の扱い（app/jpdate.py）のテスト。"""
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app import calendar_intent, gmail
from app.jpdate import from_epoch_ms, to_jst_naive


@pytest.fixture
def server_tz_utc(monkeypatch):
    """サーバーの TZ が UTC でも日本時間になることを確かめる。"""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_to_jst_naive():
    utc = datetime(2026, 5, 10, 9, 51, tzinfo=UTC)
    assert to_jst_naive(utc) == datetime(2026, 5, 10, 18, 51)
    jst = datetime(2026, 5, 10, 18, 51, tzinfo=timezone(timedelta(hours=9)))
    assert to_jst_naive(jst) == datetime(2026, 5, 10, 18, 51)
    assert to_jst_naive(datetime(2026, 5, 10, 18, 51)) == datetime(2026, 5, 10, 18, 51)


def test_epoch_ms_does_not_depend_on_server_tz(server_tz_utc):
    assert from_epoch_ms(1785000000000) == datetime(2026, 7, 26, 2, 20)


def test_gmail_received_at_is_jst(server_tz_utc):
    assert gmail._parse_internal_date("1785000000000") == datetime(2026, 7, 26, 2, 20)


def test_calendar_intent_converts_offsets_to_jst():
    assert calendar_intent._parse_dt("2026-11-06T10:00:00Z") == datetime(2026, 11, 6, 19, 0)
    assert calendar_intent._parse_dt("2026-11-06T19:00:00+09:00") == datetime(2026, 11, 6, 19, 0)
    assert calendar_intent._parse_dt("2026-11-06T19:00:00") == datetime(2026, 11, 6, 19, 0)
