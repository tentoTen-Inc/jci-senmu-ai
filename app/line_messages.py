"""LINE 出欠メッセージのビルダーと postback 処理（docs/mvp-design.md §4.2）。"""
from __future__ import annotations

from datetime import datetime

from linebot.v3.messaging import (
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .attendance import add_absence_reason, record_attendance, set_late_leave
from .models import (
    AbsenceReason,
    AttendanceStatus,
    Event,
    LateLeave,
    Member,
)
from .repository import Repository

# postback データ: "<action>|<event_id>|<value>"
SEP = "|"
ACTION_ATT = "att"
ACTION_LATE = "att_ll"
ACTION_REASON = "att_reason"


def make_postback(action: str, event_id: str, value: str) -> str:
    return SEP.join([action, event_id, value])


def parse_postback(data: str) -> tuple[str, str, str]:
    parts = data.split(SEP)
    action = parts[0] if parts else ""
    event_id = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    return action, event_id, value


def build_attendance_request(event: Event) -> TextMessage:
    """出欠依頼メッセージ（出席/Web出席/欠席のクイックリプライ）。"""
    when = event.datetime_start.strftime("%-m月%-d日 %H:%M")
    lines = [f"【出欠確認】{event.title}", f"日時: {when}"]
    if event.location:
        lines.append(f"場所: {event.location}")
    lines.append("出欠をお選びください。")
    qr = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label=label,
                    data=make_postback(ACTION_ATT, event.event_id, label),
                    display_text=label,
                )
            )
            for label in ("出席", "Web出席", "欠席")
        ]
    )
    return TextMessage(text="\n".join(lines), quick_reply=qr)


def _question(event_id: str, action: str, options: list[str], prompt: str) -> TextMessage:
    qr = QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label=opt,
                    data=make_postback(action, event_id, opt),
                    display_text=opt,
                )
            )
            for opt in options
        ]
    )
    return TextMessage(text=prompt, quick_reply=qr)


def build_lateleave_question(event_id: str) -> TextMessage:
    return _question(
        event_id, ACTION_LATE,
        [e.value for e in LateLeave],
        "遅刻・早退の予定はありますか？",
    )


def build_reason_question(event_id: str) -> TextMessage:
    return _question(
        event_id, ACTION_REASON,
        [e.value for e in AbsenceReason],
        "差し支えなければ欠席理由を教えてください。",
    )


def apply_postback(
    repo: Repository,
    member: Member,
    data: str,
    *,
    now: datetime,
) -> list[TextMessage]:
    """postback を処理し、返信メッセージ列を返す。"""
    action, event_id, value = parse_postback(data)

    if action == ACTION_ATT:
        status = AttendanceStatus(value)
        record_attendance(repo, event_id, member.member_id, status, now=now)
        if status in (AttendanceStatus.出席, AttendanceStatus.WEB出席):
            return [build_lateleave_question(event_id)]
        return [build_reason_question(event_id)]

    if action == ACTION_LATE:
        set_late_leave(repo, event_id, member.member_id, LateLeave(value), now=now)
        return [TextMessage(text="出席で受け付けました。ありがとうございます！")]

    if action == ACTION_REASON:
        add_absence_reason(repo, event_id, member.member_id, AbsenceReason(value), now=now)
        return [TextMessage(text="欠席で承りました。ご連絡ありがとうございます。")]

    return [TextMessage(text="ご回答ありがとうございます。")]
