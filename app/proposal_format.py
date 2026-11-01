"""議案の形式チェックと、体裁の自動修正・委員長への知らせ（docs/proposal-ledger-design.md §11）。

作業文書を持たず資料フォルダに紐づく議案（予算書など）は、フォルダの資料を読んで調べる
（app/folder_docs.py、docs/folder-proposal-check-design.md。自動では直さない）。

管理画面の［形式チェック］から動く（ボタンからだけ。チャットからは動かさない）。

1. 議案書（Google ドキュメント）があれば、フォント・文字の大きさ・全角の英数字と記号を直す
   （app/doc_format.py。内容は変えない）
2. 直した後の文書を台帳に写し直し、必須項目と表記を調べる（app/format_check.py）。
   残った点は「委員会で直す点」として画面に出す
3. 直したら、その委員会の委員長に LINE で知らせる。静音時間なら、明けた tick で送る
"""
from __future__ import annotations

import logging
from datetime import datetime

from linebot.v3.messaging import TextMessage

from . import committee_docs, doc_format, folder_docs, lom
from .format_check import check_fullwidth, run_format_check
from .guardrails import is_quiet_hours
from .models import FormatCheckResult, Member, MemberStatus, Proposal, ProposalSource
from .proposal_submit import _push_one, doc_label
from .proposal_sync import apply_document
from .repository import Repository

logger = logging.getLogger("jci-agent.proposal_format")

CHAIR = committee_docs.CHAIR
KIND = "proposal_format_fix"


def chairs(repo: Repository, committee: str) -> list[Member]:
    """その委員会の委員長（兼務の席を含む。在籍の会員だけ）。"""
    out = []
    for m in repo.list_members():
        if m.status != MemberStatus.active:
            continue
        seats = any(s.committee == committee and s.role == CHAIR for s in m.committees)
        if seats or (m.committee == committee and m.committee_role == CHAIR):
            out.append(m)
    return out


def _recipients(repo: Repository, p: Proposal) -> list[Member]:
    """知らせる相手。委員長 → 議案の担当者（予算など委員会の外の議案）。"""
    found = chairs(repo, p.committee or "")
    if found:
        return found
    owner = repo.get_member(p.owner_member_id) if p.owner_member_id else None
    return [owner] if owner and owner.status == MemberStatus.active else []


def notice_text(p: Proposal) -> str:
    fc = p.format_check
    lines = [f"【体裁を直しました】{doc_label(p)}の体裁を、{lom.bot_name()}が直しました。"]
    lines += [f"・{x}" for x in (fc.fixed if fc else [])]
    lines.append("書かれている内容は変えていません。")
    if fc and fc.issues:
        lines.append("委員会で直していただきたい点:")
        lines += [f"・{x}" for x in fc.issues]
    if p.storage_uri:
        lines.append(p.storage_uri)
    return "\n".join(lines)


def send_notice(repo: Repository, p: Proposal, now: datetime) -> bool:
    """直したことを委員長に知らせる。送った（相手がいない場合も含む）なら True。"""
    fc = p.format_check
    if fc is None or not fc.fixed or fc.notified_at is not None:
        return False
    if is_quiet_hours(now, repo.get_settings()):
        return False
    text = notice_text(p)
    sent: list[str] = []
    for member in _recipients(repo, p):
        ok = _push_one(repo, member, TextMessage(text=text), text, now, kind=KIND)
        if ok is None:  # 送信の一時停止中など。次の tick で送り直す
            return False
        if ok:
            sent.append(member.name)
    repo.upsert_proposal(p.model_copy(update={"format_check": fc.model_copy(update={
        "notified_at": now, "notified_to": sent})}))
    return True


def send_pending(repo: Repository, now: datetime) -> int:
    """tick: まだ知らせていない「直しました」を送る。"""
    count = 0
    for p in repo.list_proposals(status="open"):
        try:
            count += send_notice(repo, p, now)
        except Exception:  # noqa: BLE001 - 1件の失敗で他を止めない
            logger.exception("体裁の知らせに失敗: %s", p.proposal_id)
    return count


def _fiscal_year(repo: Repository, p: Proposal, now: datetime) -> int:
    """議案の年度（議案名 → 台帳の年度 → 運用開始日の年 → 今年）。"""
    start = repo.get_settings().operation_start
    fallback = p.fiscal_year or (start.year if start else now.year)
    return folder_docs.fiscal_year(p.title, fallback)


def read_sources(repo: Repository, p: Proposal, now: datetime
                 ) -> tuple[Proposal, list[folder_docs.SourceFile] | None]:
    """資料フォルダを読み、台帳の本文と読んだファイルの一覧に写す。読めなければ files は None。"""
    read = folder_docs.read(p.storage_uri)
    if read.error:
        p = p.model_copy(update={"source_error": read.error, "sources_read_at": now})
        repo.upsert_proposal(p)
        return p, None
    p = p.model_copy(update={
        "content": folder_docs.to_content(read.files),
        "sources": [ProposalSource(name=f.name, url=f.url) for f in read.files],
        "source_error": None, "sources_read_at": now,
    })
    repo.upsert_proposal(p)
    return p, read.files


def check(repo: Repository, p: Proposal, now: datetime) -> Proposal:
    """形式チェック。議案書があれば体裁を直してから調べ、直したら委員長に知らせる。"""
    fixed: list[str] = []
    issues: list[str] = []
    if p.doc_id and not committee_docs.is_enabled():
        # 設定がないと文書を直せないまま「問題なし」に見えてしまうので、画面に出す
        issues.append("議案書の体裁を直す設定（COMMITTEE_DOCS_FOLDER）がこのサービスにないため、"
                      "体裁は直していません。")
    elif p.doc_id:
        try:
            token = committee_docs._token()
            fix, doc = doc_format.fix_document(p.doc_id, token)
            fixed = fix.summary(doc_format.doc_font())
            p = apply_document(p, doc)
        except Exception as exc:  # noqa: BLE001 - 直せなくても、本文のチェックは続ける
            logger.warning("議案書の体裁を直せませんでした: %s %s", p.proposal_id, exc)
            issues.append("議案書の体裁を自動で直せませんでした（文書を開けない、または"
                          "チェック中に編集されました）。少し待ってからやり直してください。")
    if not p.doc_id and folder_docs.parse_location(p.storage_uri):
        # 資料フォルダに紐づく議案（予算書など）。読んだ資料を調べる（自動では直さない）
        p, files = read_sources(repo, p, now)
        if files is None:
            result = FormatCheckResult(passed=False, issues=[p.source_error or ""])
        else:
            found = folder_docs.check(files, _fiscal_year(repo, p, now))
            found += check_fullwidth(folder_docs.doc_text(files))
            result = FormatCheckResult(passed=not found, issues=found)
    else:
        result = run_format_check(p)
    previous = p.format_check
    # まだ知らせていない前回の直しは引き継ぐ（知らせが消えないように）
    carried = (previous.fixed if previous and previous.fixed and previous.notified_at is None
               else [])
    result = result.model_copy(update={
        "issues": issues + result.issues, "passed": not issues and result.passed,
        "checked_at": now, "fixed": carried + fixed,
        "fixed_at": now if fixed else (previous.fixed_at if carried else None),
    })
    p = p.model_copy(update={"format_check": result})
    repo.upsert_proposal(p)
    if result.fixed:
        send_notice(repo, p, now)
        p = repo.get_proposal(p.proposal_id) or p
    return p
