"""会議の配信物（docs/meeting-notice-design.md）。通知テンプレートで LINE と案内メールを作る。

会議（Event）と上程議案（台帳）から差し込みの値を作り、テンプレートで本文を作る。
送るのは管理画面のボタンから（人の確認の後。自動では送らない）。
- LINE（段階 A）: 会議体のグループ（設定の line_groups・委員会はグループ名）。
  なければ連携済みの対象者に個別に送る。
- メール（段階 B）: LOM の Gmail に下書きを作る。専務が Gmail で確かめて送る（§13）。
  宛名・差出人・To/BCC は会議体ごとの設定、文書番号は下書きを作るときに決める。
"""
from __future__ import annotations

import logging
from datetime import datetime

from linebot.v3.messaging import (
    AllMentionTarget,
    MentionSubstitutionObject,
    TextMessage,
    TextMessageV2,
)

from . import gmail, line_push, lom
from . import notice_layout as nl
from . import notice_template as nt
from . import schedule_poll as sp
from .audit import write_audit
from .events import resolve_targets
from .jpdate import month_day, wareki_date
from .meeting_package import proposals_for
from .models import (
    EmailDispatch,
    Event,
    EventType,
    LineDispatch,
    MeetingDispatch,
    MeetingNoticeSetting,
    Member,
    NoticeTemplate,
)
from .repository import Repository

logger = logging.getLogger("jci-agent.dispatch")

#: 会議の種類 → 設定 line_groups の鍵（委員会はグループ名で探す）
GROUP_KEYS = {EventType.五役会: "goyaku", EventType.理事会: "riji",
              EventType.例会: "all", EventType.総会: "all"}
MENTION_KEY = "everyone"


class DispatchError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def get_dispatch(repo: Repository, event_id: str) -> MeetingDispatch:
    return repo.get_meeting_dispatch(event_id) or MeetingDispatch(event_id=event_id)


# --------------------------------------------------------------------------- #
# 差し込みの値
# --------------------------------------------------------------------------- #
def _doc_url(p) -> str | None:
    if p.doc_id:
        return f"https://docs.google.com/document/d/{p.doc_id}/edit"
    return p.storage_uri


def context(repo: Repository, event: Event, dispatch: MeetingDispatch, now: datetime) -> dict:
    """テンプレートに差し込む値（§4.2）。"""
    start = event.datetime_start
    end = event.datetime_end
    names = {m.member_id: m.name for m in repo.list_members()}
    proposals = [
        {"no": p.number or f"第{i}号議案", "title": p.title, "url": _doc_url(p) or "",
         "presenter": names.get(p.owner_member_id or "", "")}
        for i, p in enumerate(proposals_for(repo, event), start=1)
    ]
    return {
        "lom.name": lom.lom_name(), "lom.short_name": lom.lom_short_name(),
        "lom.bot_name": lom.bot_name(),
        "fiscal_year": event.fiscal_year or start.year,
        "event.title": event.title,
        "event.date": month_day(start), "event.date_wareki": wareki_date(start),
        "event.time": f"{start:%H:%M}〜" + (f"{end:%H:%M}" if end else ""),
        "event.place": event.location or "",
        "event.online_url": event.meet_url or "",
        "agenda.url": dispatch.agenda_url or "", "opinion.url": dispatch.opinion_url or "",
        "online": bool(event.meet_url), "agenda": bool(dispatch.agenda_url),
        "opinion": bool(dispatch.opinion_url),
        "late": now > nt.deliver_deadline(start, event.delivery_at),
        "proposals": proposals,
    }


# --------------------------------------------------------------------------- #
# 送り先
# --------------------------------------------------------------------------- #
def _group(repo: Repository, event: Event):
    key = GROUP_KEYS.get(event.type)
    if key:
        return sp.preset_group(repo, key)
    if event.type == EventType.委員会 and event.target_scope.value:
        return sp.committee_group(repo, event.target_scope.value[0])
    return None


def destination(repo: Repository, event: Event) -> dict:
    """確認画面に出す送り先（グループ名、または個別に送る人数）。"""
    targets = resolve_targets(repo, event)
    linked = [m for m in targets if m.line_user_id]
    group = _group(repo, event)
    if group:
        return {"mode": "group", "group_id": group.group_id, "group_name": group.group_name,
                "targets": len(targets), "linked": len(linked)}
    return {"mode": "individual", "group_id": None, "group_name": None,
            "targets": len(targets), "linked": len(linked)}


# --------------------------------------------------------------------------- #
# 本文と送信
# --------------------------------------------------------------------------- #
def render_line(template: NoticeTemplate, ctx: dict) -> str:
    text = nt.render(template.body, ctx)
    if len(text) > nt.LINE_LIMIT:
        raise DispatchError(400, f"本文が LINE の上限（{nt.LINE_LIMIT}字）を超えています。")
    return text


def line_message(template: NoticeTemplate, text: str):
    """全員へのメンションは textV2（本文の { } は {{ }} に直す。LINE の決まり）。"""
    if template.mention == "all":
        escaped = text.replace("{", "{{").replace("}", "}}")
        return TextMessageV2(
            text=f"{{{MENTION_KEY}}}\n{escaped}",
            substitution={MENTION_KEY: MentionSubstitutionObject(mentionee=AllMentionTarget())})
    return TextMessage(text=text)


def view(repo: Repository, event: Event, now: datetime,
         template_id: str | None = None, email_template_id: str | None = None) -> dict:
    """管理画面「案内・配信」の内容（テンプレートの候補・プレビュー・送り先・送った記録）。"""
    dispatch = get_dispatch(repo, event.event_id)
    choices = [t for t in nt.list_templates(repo) if t.channel == "line"
               and (not t.event_types or event.type.value in t.event_types)]
    chosen = (nt.get_template(repo, template_id) if template_id else None) or \
        (nt.get_template(repo, dispatch.line.template_id) if dispatch.line.template_id else None) \
        or nt.default_for(repo, "line", "agenda", event.type.value)
    ctx = context(repo, event, dispatch, now)
    preview, error = None, None
    if chosen:
        try:
            preview = render_line(chosen, ctx)
        except (DispatchError, nt.TemplateError) as exc:
            error = getattr(exc, "message", str(exc))
    missing = []
    if not dispatch.agenda_url:
        missing.append("アジェンダの URL が入っていません（アジェンダの行は出ません）。")
    if not event.meet_url and event.location and "オンライン" in event.location:
        missing.append("オンラインの URL（Meet）が予定にありません。")
    return {
        "dispatch": dispatch.model_dump(mode="json"),
        "templates": [{"template_id": t.template_id, "name": t.name, "purpose": t.purpose,
                       "mention": t.mention} for t in choices],
        "template_id": chosen.template_id if chosen else None,
        "mention": chosen.mention if chosen else "none",
        "preview": preview, "error": error, "missing": missing,
        "late": ctx["late"],
        "deliver_deadline": nt.deliver_deadline(event.datetime_start,
                                                event.delivery_at).isoformat(),
        "proposals": ctx["proposals"],
        "destination": destination(repo, event),
        "email": email_view(repo, event, now, email_template_id),
    }


def update(repo: Repository, event: Event, *, agenda_url: str | None,
           opinion_url: str | None, actor: str, now: datetime) -> MeetingDispatch:
    dispatch = get_dispatch(repo, event.event_id).model_copy(update={
        "agenda_url": (agenda_url or "").strip() or None,
        "opinion_url": (opinion_url or "").strip() or None,
        "updated_at": now, "updated_by": actor,
    })
    repo.save_meeting_dispatch(dispatch)
    return dispatch


def send_line(repo: Repository, event: Event, *, template_id: str, expected_group_id: str | None,
              actor: str, now: datetime) -> MeetingDispatch:
    """LINE に送る。画面で確かめた送り先（expected_group_id）と今の送り先が違えば送らない。"""
    template = nt.get_template(repo, template_id)
    if template is None or template.channel != "line":
        raise DispatchError(404, "LINE のテンプレートが見つかりません。")
    dispatch = get_dispatch(repo, event.event_id)
    text = render_line(template, context(repo, event, dispatch, now))
    dest = destination(repo, event)
    if dest["group_id"] != expected_group_id:
        raise DispatchError(409, "送り先が変わりました。画面を開き直して確かめてください。")
    message = line_message(template, text)
    if dest["mode"] == "group":
        if not line_push.push_messages(dest["group_id"], [message]):
            raise DispatchError(503, sp.SEND_FAILED)
        recipients = 0
    else:
        linked = [m for m in resolve_targets(repo, event) if m.line_user_id]
        if not linked:
            raise DispatchError(409, "送り先のグループがなく、LINE 連携済みの対象者もいません。")
        recipients = 0
        for m in linked:
            try:
                if line_push.push_messages(m.line_user_id, [message]):
                    recipients += 1
            except Exception:  # noqa: BLE001 - 友だち未追加など。他の人には送る
                logger.warning("会議の案内を個別に送れませんでした: %s", m.member_id)
        if recipients == 0:
            raise DispatchError(503, "LINE で送れませんでした（友だち未追加など）。")
    sent = LineDispatch(template_id=template.template_id, text=text, group_id=dest["group_id"],
                        group_name=dest["group_name"], recipients=recipients, sent_at=now,
                        sent_by=actor)
    dispatch = dispatch.model_copy(update={"line": sent, "history": [*dispatch.history, sent],
                                           "updated_at": now, "updated_by": actor})
    repo.save_meeting_dispatch(dispatch)
    write_audit(repo, actor=actor, action="dispatch.line", target=event.event_id,
                detail=f"{template.name} → {dest['group_name'] or f'個別 {recipients}人'}")
    return dispatch


# --------------------------------------------------------------------------- #
# 案内メール（段階 B）
# --------------------------------------------------------------------------- #
def notice_setting(repo: Repository, event: Event) -> MeetingNoticeSetting:
    return repo.get_settings().meeting_notices.get(event.type.value) or MeetingNoticeSetting()


def _sender(repo: Repository, setting: MeetingNoticeSetting) -> Member | None:
    if not setting.sender_role:
        return None
    return next((m for m in repo.list_members()
                 if m.is_current and m.officer_role == setting.sender_role), None)


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def recipients(repo: Repository, event: Event, setting: MeetingNoticeSetting) -> dict:
    """宛先（To/CC/BCC）と、メールアドレスのない対象者。LINE の連携は問わない。"""
    targets = resolve_targets(repo, event, require_line=False)
    cc = _unique([m.contact.email.strip() for m in repo.list_members()
                  if m.is_current and m.officer_role in setting.cc_roles and m.contact.email])
    main = _unique([m.contact.email.strip() for m in targets
                    if m.contact.email and m.contact.email.strip() not in cc])
    missing = [{"member_id": m.member_id, "name": m.name} for m in targets
               if not (m.contact.email or "").strip()]
    if setting.to_mode == "to":
        to, bcc = main, []
    else:
        own = lom.google_account()
        to, bcc = ([own] if own else []), main
    return {"mode": "to" if setting.to_mode == "to" else "bcc", "to": to, "cc": cc, "bcc": bcc,
            "count": len(main) + len(cc), "targets": len(targets), "missing": missing}


def _format_doc_number(seq: int, cfg) -> str:
    return (cfg.format.replace("{prefix}", cfg.prefix).replace("{term}", cfg.term)
            .replace("{seq}", str(seq).zfill(max(cfg.digits, 1))))


def doc_number(repo: Repository, event: Event, dispatch: MeetingDispatch,
               fiscal_year: int) -> tuple[int | None, str | None]:
    """この会議の文書番号（使わない設定なら None）。前に下書きを作っていれば同じ番号を使う。

    新しい番号は、その年度で使った番号の続き（設定の最初の番号より小さくしない）。
    """
    cfg = repo.get_settings().doc_number
    if not cfg.enabled:
        return None, None
    if dispatch.email.doc_seq and dispatch.email.fiscal_year == fiscal_year:
        return dispatch.email.doc_seq, _format_doc_number(dispatch.email.doc_seq, cfg)
    used = [d.email.doc_seq for d in repo.list_meeting_dispatches()
            if d.event_id != event.event_id and d.email.fiscal_year == fiscal_year
            and d.email.doc_seq]
    seq = max([cfg.start_seq.get(str(fiscal_year), 1), *[u + 1 for u in used]])
    return seq, _format_doc_number(seq, cfg)


def email_context(repo: Repository, event: Event, dispatch: MeetingDispatch,
                  now: datetime) -> tuple[dict, int | None]:
    """メールの差し込みの値（LINE と同じ値＋宛名・差出人・文書番号・発信日）と文書番号の連番。"""
    ctx = context(repo, event, dispatch, now)
    setting = notice_setting(repo, event)
    sender = _sender(repo, setting)
    seq, number = doc_number(repo, event, dispatch, ctx["fiscal_year"])
    ctx |= {
        "audience": setting.audience,
        "sender.title": setting.sender_title or setting.sender_role,
        "sender.name": sender.name if sender else "",
        "doc_number": number or "",
        "issued_on": month_day(now),
        "issued_on_wareki": wareki_date(now, weekday=False),
    }
    return ctx, seq


def render_email(template: NoticeTemplate, ctx: dict, style) -> dict:
    text = nt.render(template.body, ctx)
    return {"subject": nt.render(template.subject, ctx).strip() if template.subject else "",
            "body": text, "html": nl.to_html(text, style), "text": nl.to_text(text)}


def _email_template(repo: Repository, event: Event, dispatch: MeetingDispatch,
                    template_id: str | None) -> NoticeTemplate | None:
    if template_id:
        return nt.get_template(repo, template_id)
    if dispatch.email.template_id:
        return nt.get_template(repo, dispatch.email.template_id)
    return nt.default_for(repo, "email", "meeting_notice", event.type.value) or \
        nt.default_for(repo, "email", "agenda", event.type.value)


def email_view(repo: Repository, event: Event, now: datetime,
               template_id: str | None = None) -> dict:
    """「案内・配信」のメールの欄（テンプレートの候補・プレビュー・宛先・文書番号・下書きの記録）。"""
    dispatch = get_dispatch(repo, event.event_id)
    choices = [t for t in nt.list_templates(repo) if t.channel == "email"
               and (not t.event_types or event.type.value in t.event_types)]
    chosen = _email_template(repo, event, dispatch, template_id)
    setting = notice_setting(repo, event)
    ctx, _ = email_context(repo, event, dispatch, now)
    preview, error = None, None
    if chosen:
        try:
            preview = render_email(chosen, ctx, repo.get_settings().email_style)
        except nt.TemplateError as exc:
            error = str(exc)
    hints = []
    if not setting.audience:
        hints.append(f"「{event.type.value}」の宛名が設定にありません（設定 → 案内メール）。")
    if setting.sender_role and not ctx["sender.name"]:
        hints.append(f"差出人の役職「{setting.sender_role}」の会員が見つかりません。")
    if not setting.sender_role:
        hints.append(f"「{event.type.value}」の差出人の役職が設定にありません"
                     "（設定 → 案内メール）。")
    rcpt = recipients(repo, event, setting)
    if rcpt["mode"] == "bcc" and not rcpt["to"]:
        hints.append("BCC で送るときの To（LOM のアドレス）が設定されていません。")
    return {
        "templates": [{"template_id": t.template_id, "name": t.name, "purpose": t.purpose}
                      for t in choices],
        "template_id": chosen.template_id if chosen else None,
        "preview": preview, "error": error, "hints": hints,
        "recipients": rcpt,
        "doc_number": ctx["doc_number"] or None,
        "account": lom.google_account(),
    }


def make_draft(repo: Repository, event: Event, *, template_id: str, expected_count: int,
               actor: str, now: datetime) -> MeetingDispatch:
    """LOM の Gmail に案内メールの下書きを作る（前に作った下書きがあれば置き換える）。

    画面で確かめた宛先の人数（expected_count）と今の人数が違えば作らない。
    """
    template = nt.get_template(repo, template_id)
    if template is None or template.channel != "email":
        raise DispatchError(404, "メールのテンプレートが見つかりません。")
    dispatch = get_dispatch(repo, event.event_id)
    setting = notice_setting(repo, event)
    rcpt = recipients(repo, event, setting)
    if rcpt["count"] == 0:
        raise DispatchError(409, "メールアドレスのある宛先がいません"
                                 "（会員の情報にメールを入れてください）。")
    if rcpt["count"] != expected_count:
        raise DispatchError(409, "宛先が変わりました。画面を開き直して確かめてください。")
    ctx, seq = email_context(repo, event, dispatch, now)
    mail = render_email(template, ctx, repo.get_settings().email_style)
    if not mail["subject"]:
        raise DispatchError(400, "件名が空です。テンプレートに件名を入れてください。")
    try:
        draft_id = gmail.save_draft(to=rcpt["to"], cc=rcpt["cc"], bcc=rcpt["bcc"],
                                    subject=mail["subject"], html=mail["html"], text=mail["text"],
                                    draft_id=dispatch.email.draft_id)
    except gmail.DraftError as exc:
        raise DispatchError(503, str(exc)) from exc
    except RuntimeError as exc:  # OAuth の情報がない
        raise DispatchError(503, f"Gmail を使う設定がありません: {exc}") from exc
    email = EmailDispatch(
        template_id=template.template_id, subject=mail["subject"], text=mail["body"],
        to=rcpt["to"], cc=rcpt["cc"], bcc=rcpt["bcc"], fiscal_year=ctx["fiscal_year"],
        doc_seq=seq, doc_number=ctx["doc_number"] or None, draft_id=draft_id,
        drafted_at=now, drafted_by=actor)
    dispatch = dispatch.model_copy(update={"email": email, "updated_at": now,
                                           "updated_by": actor})
    repo.save_meeting_dispatch(dispatch)
    write_audit(repo, actor=actor, action="dispatch.email_draft", target=event.event_id,
                detail=f"{template.name} → {rcpt['count']}人"
                + (f"（{email.doc_number}）" if email.doc_number else ""))
    return dispatch
