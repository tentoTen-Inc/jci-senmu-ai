"""監査ログ（docs/dashboard-design.md §4.2）。

全自動配信・設定変更・LLM判断・専務操作などを Firestore(auditLogs) と
Cloud Logging に記録する。IAP 配下では actor に操作者メールが入る。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from .models import AuditLog
from .repository import Repository

logger = logging.getLogger("jci-agent.audit")


def write_audit(
    repo: Repository,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> AuditLog:
    entry = AuditLog(
        audit_id=f"aud_{uuid.uuid4().hex[:12]}",
        at=now or datetime.now(),
        actor=actor,
        action=action,
        target=target,
        detail=detail,
    )
    try:
        repo.save_audit(entry)
    except Exception:  # noqa: BLE001 - 監査保存失敗で本処理は止めない
        logger.exception("audit 保存に失敗: %s", action)
    logger.info("AUDIT actor=%s action=%s target=%s", actor, action, target)
    return entry
