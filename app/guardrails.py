"""全自動配信のガードレール（docs/mvp-design.md §6）。

- 静音時間（クワイエットアワー）
- レート制限（会員あたり/日）
- サニティチェック（対象人数・空文・未置換プレースホルダ）
- キルスイッチ
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .models import DeliveryResult, Settings
from .repository import Repository


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_quiet_hours(now: datetime, settings: Settings) -> bool:
    start = _parse_hhmm(settings.quiet_hours.start)
    end = _parse_hhmm(settings.quiet_hours.end)
    t = now.time()
    if start <= end:
        return start <= t < end
    # 日付をまたぐ静音時間（例 21:00-08:00）
    return t >= start or t < end


def next_send_time(now: datetime, settings: Settings) -> datetime:
    """静音時間中なら次に送信可能な時刻（=終了時刻）を返す。"""
    if not is_quiet_hours(now, settings):
        return now
    end = _parse_hhmm(settings.quiet_hours.end)
    candidate = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def count_sends_today(repo: Repository, member_id: str, now: datetime) -> int:
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(
        1
        for log in repo.list_delivery_logs()
        if log.member_id == member_id
        and log.result == DeliveryResult.ok
        and log.sent_at >= start_of_day
    )


def sanity_check(targets: list[str], body: str, total_members: int) -> list[str]:
    """配信前の健全性チェック。問題のリスト（空ならOK）を返す。"""
    problems: list[str] = []
    if not body or not body.strip():
        problems.append("empty_body")
    if "{{" in body or "}}" in body:
        problems.append("unresolved_placeholder")
    if total_members >= 0 and len(targets) > total_members:
        problems.append("too_many_targets")
    if len(targets) == 0:
        problems.append("no_targets")
    return problems


@dataclass
class SendDecision:
    send: bool
    reason: str = "ok"  # ok | kill_switch | quiet_hours | rate_limited
    defer_to: datetime | None = None


def decide_send(
    repo: Repository,
    member_id: str,
    *,
    now: datetime,
    settings: Settings,
) -> SendDecision:
    if settings.kill_switch:
        return SendDecision(send=False, reason="kill_switch")
    if is_quiet_hours(now, settings):
        return SendDecision(
            send=False, reason="quiet_hours", defer_to=next_send_time(now, settings)
        )
    if count_sends_today(repo, member_id, now) >= settings.rate_limit.per_member_per_day:
        return SendDecision(send=False, reason="rate_limited")
    return SendDecision(send=True)


@dataclass
class BatchHalt:
    halted: bool
    problems: list[str] = field(default_factory=list)
