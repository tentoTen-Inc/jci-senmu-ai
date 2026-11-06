"""専務が議案を差し戻したときの知らせ（docs/dashboard-design.md の承認/差戻し）。

管理画面の［差し戻す］で、次の人の個別トークに「【差し戻し】…」と専務のコメントを送る。
- 議案を［提出する］で出した人（履歴の最後の資料提出）
- 委員会の議案はその委員会の委員長、委員会の外の議案（予算など）は議案の担当者
- 担当副理事長
同じ人には1回だけ。専務理事には送らない（差し戻した本人）。
静音時間・送信の一時停止のときは送らず、次の tick（`send_pending`）で送る。
送った知らせは `Proposal.notices`（`returned:<差し戻した時刻>`）と `Proposal.alerts` に記録する。
"""
from __future__ import annotations

import logging
from datetime import datetime

from linebot.v3.messaging import FlexContainer, FlexMessage

from .guardrails import is_quiet_hours
from .models import Member, MemberStatus, Proposal, ProposalAlert, ProposalStage
from .proposal_format import chairs
from .proposal_submit import LIFF_BY_PREFIX, _naive, _push_one, doc_label, supervisors
from .repository import Repository
from .sed_notice import NOTIFY_ROLES

logger = logging.getLogger("jci-agent.proposal_return")

KIND = "proposal_return"
KEY_PREFIX = "returned:"


def return_key(p: Proposal) -> str | None:
    """まだ送っていない差し戻しの知らせのキー。送るものが無ければ None。"""
    a = p.sed_approval
    if p.status != "open" or a.status != "returned" or a.at is None \
            or p.stage != ProposalStage.entry:
        return None
    key = f"{KEY_PREFIX}{_naive(a.at).isoformat()}"
    return None if key in p.notices else key


def _submitter(repo: Repository, p: Proposal) -> Member | None:
    last = next((h for h in reversed(p.history) if h.stage == ProposalStage.submitted), None)
    if last is None or not last.by.startswith(LIFF_BY_PREFIX):
        return None
    name = last.by.removeprefix(LIFF_BY_PREFIX)
    return next((m for m in repo.list_members() if m.name == name), None)


def targets(repo: Repository, p: Proposal) -> list[Member]:
    """知らせる相手。提出した人 → 委員長（委員会の外の議案は担当者）→ 担当副理事長。"""
    owner = repo.get_member(p.owner_member_id) if p.owner_member_id else None
    people = [_submitter(repo, p),
              *(chairs(repo, p.committee) if p.committee else [owner]),
              *(supervisors(repo, p.committee) if p.committee else [])]
    out: dict[str, Member] = {}
    for m in people:
        if m is not None and m.status == MemberStatus.active \
                and m.officer_role not in NOTIFY_ROLES:
            out.setdefault(m.member_id, m)
    return list(out.values())


def message(p: Proposal) -> FlexMessage:
    text = f"【差し戻し】{doc_label(p)}を、専務理事が差し戻しました。"
    comment = (p.sed_approval.comment or "").strip()
    contents = [{"type": "text", "text": text, "wrap": True, "size": "sm"}]
    if comment:
        contents.append({"type": "text", "text": f"専務のコメント:\n{comment}", "wrap": True,
                         "size": "sm", "margin": "md"})
    contents.append({"type": "text", "wrap": True, "size": "xs", "color": "#888888",
                     "margin": "md",
                     "text": "直したら、LINE のメニュー［委員会の議案］の［提出する］から"
                             "出し直してください。"})
    bubble: dict = {"type": "bubble",
                    "body": {"type": "box", "layout": "vertical", "contents": contents}}
    if p.storage_uri:
        bubble["footer"] = {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "action": {
                "type": "uri", "label": "議案書を開く", "uri": p.storage_uri}}]}
    alt = f"{text}\n{comment}" if comment else text
    return FlexMessage(alt_text=alt[:400], contents=FlexContainer.from_dict(bubble))


def notify(repo: Repository, p: Proposal, now: datetime) -> Proposal:
    """差し戻しを知らせる。静音時間・一時停止なら送らずに返す（次の tick で送る）。"""
    key = return_key(p)
    if key is None or is_quiet_hours(now, repo.get_settings()):
        return p
    msg = message(p)
    alerts = []
    for member in targets(repo, p):
        ok = _push_one(repo, member, msg, msg.alt_text, now, kind=KIND)
        if ok is None:  # 停止中など。次の tick でまとめて送り直す
            return p
        alerts.append(ProposalAlert(key=key, at=now, member_id=member.member_id,
                                    name=member.name, ok=ok))
    p = p.model_copy(update={"notices": {**p.notices, key: now},
                             "alerts": [*p.alerts, *alerts]})
    repo.upsert_proposal(p)
    return p


def send_pending(repo: Repository, now: datetime) -> int:
    """tick: 静音時間などで送れなかった差し戻しの知らせを送る。送った議案の数を返す。"""
    sent = 0
    for p in repo.list_proposals(status="open"):
        if return_key(p) is None:
            continue
        try:
            sent += notify(repo, p, now) is not p
        except Exception:  # noqa: BLE001 - 1件の失敗で他を止めない
            logger.exception("差し戻しの知らせに失敗: %s", p.proposal_id)
    return sent


def status(p: Proposal) -> dict | None:
    """管理画面に出す、最後の差し戻しの知らせの状況。差し戻していなければ None。"""
    a = p.sed_approval
    if a.status != "returned" or a.at is None:
        return None
    key = f"{KEY_PREFIX}{_naive(a.at).isoformat()}"
    if key not in p.notices:
        return {"state": "pending", "sent": [], "failed": []}
    sent = [x.name for x in p.alerts if x.key == key and x.ok]
    failed = [x.name for x in p.alerts if x.key == key and not x.ok]
    return {"state": "done", "sent": sent, "failed": failed}
