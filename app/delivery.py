"""配信ジョブの実行（ガードレール適用＋監査ログ, docs/mvp-design.md §6）。

実際の LINE 送信は ``sender`` コールバックに委譲し、本モジュールはガードレール判定と
DeliveryLog（監査証跡）への記録に責任を持つ。冪等性はジョブ計画側（reminders）で担保。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .guardrails import decide_send, sanity_check
from .models import DeliveryJob, DeliveryLog, DeliveryResult, DeliveryStatus
from .repository import Repository

# sender(member_id) -> 送信成功なら True
Sender = Callable[[str], bool]


@dataclass
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    deferred: int = 0
    halted: bool = False
    problems: list[str] = field(default_factory=list)


def execute_delivery(
    repo: Repository,
    job: DeliveryJob,
    body: str,
    *,
    now: datetime,
    sender: Sender,
) -> DeliveryReport:
    settings = repo.get_settings()
    total_members = len(repo.list_members())

    # バッチ全体のサニティチェック（逸脱したら配信せず halt）
    problems = sanity_check(job.targets, body, total_members)
    if problems:
        job.status = DeliveryStatus.halted
        repo.save_delivery_job(job)
        return DeliveryReport(halted=True, problems=problems)

    report = DeliveryReport()
    for member_id in job.targets:
        decision = decide_send(repo, member_id, now=now, settings=settings)
        if not decision.send:
            _log(repo, job, member_id, body, DeliveryResult.blocked, decision.reason, now)
            if decision.reason == "quiet_hours":
                report.deferred += 1
            else:
                report.blocked += 1
            continue

        ok = sender(member_id)
        if ok:
            _log(repo, job, member_id, body, DeliveryResult.ok, None, now)
            report.sent += 1
        else:
            _log(repo, job, member_id, body, DeliveryResult.failed, "send_error", now)
            report.failed += 1

    job.status = DeliveryStatus.sent
    repo.save_delivery_job(job)
    return report


def _log(
    repo: Repository,
    job: DeliveryJob,
    member_id: str,
    body: str,
    result: DeliveryResult,
    reason: str | None,
    now: datetime,
) -> None:
    repo.save_delivery_log(
        DeliveryLog(
            log_id=f"{job.job_id}:{member_id}",
            job_id=job.job_id,
            member_id=member_id,
            content=body,
            result=result,
            reason=reason,
            sent_at=now,
        )
    )
