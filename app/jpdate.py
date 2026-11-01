"""日本時間と日本語の日付表記（OS のタイムゾーン・言語設定に依存させない）。

アプリ内の日時は「日本時間の naive datetime」で扱う。外から来たタイムゾーン付きの日時
（Google API の RFC3339、epoch 秒など）は ``to_jst_naive`` で揃えてから使う。
`strftime("%a")` はサーバーのロケールで英語（Tue 等）になるため、曜日は自前で付ける。
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

WEEKDAYS = "月火水木金土日"


def to_jst_naive(dt: datetime) -> datetime:
    """タイムゾーン付きの日時を日本時間に直して tzinfo を外す（naive は日本時間とみなす）。"""
    return dt.astimezone(JST).replace(tzinfo=None) if dt.tzinfo else dt


def from_epoch_ms(ms: int) -> datetime:
    """epoch ミリ秒を日本時間の naive datetime にする（サーバーの TZ 設定に依存しない）。"""
    return to_jst_naive(datetime.fromtimestamp(ms / 1000, tz=UTC))


def weekday(dt: datetime) -> str:
    """「火」のような1文字の曜日。"""
    return WEEKDAYS[dt.weekday()]


def month_day_time(dt: datetime) -> str:
    """「10月20日(火) 19:00」。"""
    return f"{dt.month}月{dt.day}日({weekday(dt)}) {dt:%H:%M}"
