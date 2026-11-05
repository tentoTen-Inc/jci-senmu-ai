"""チャットで届いた出欠の連絡を受け付ける（F4-7、docs/nl-assistant-design.md §7）。

「本日の五役は欠席になりそうです」「13日の理事会は出張で欠席で」のような連絡を読み取り、
**本人が［この内容で登録］を押したときだけ**登録する（チャットの文だけでは書き込まない。
docs/doc-session-design.md §1）。1通に複数の会議の連絡があれば、まとめて確認する。

グループでは、ほかの人も確認のボタンを押せてしまうため、ボタンに本人の会員IDを入れ、
押した人が本人でなければ登録しない。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from linebot.v3.messaging import (
    Message,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    TextMessage,
)

from .attendance import record_attendance
from .events import resolve_targets
from .inference import record_inference
from .jpdate import WEEKDAYS
from .llm import AttendanceAnswer, model_for, parse_attendance_intent
from .models import AttendanceStatus, EventStatus, InferenceUsage, LateLeave, Member
from .repository import Repository

logger = logging.getLogger("jci-agent.attendance_intent")

MAX_CANDIDATES = 8
#: 始まった後の連絡（「やっぱり参加難しいです」）も受けるため、開始から少しの間は候補に残す
LOOKBACK = timedelta(hours=6)
#: 出欠の連絡らしい言葉。含むときだけ読み取りにかける（ほかの文で AI を呼ばない）
HINT_WORDS = (
    "出席", "欠席", "参加", "出欠", "休み", "休ませ", "行けな", "行けま", "伺え", "遅れ",
    "遅刻", "早退", "オンライン", "リモート", "Web", "WEB", "ウェブ",
)
#: 確認の［この内容で登録］の postback（"att_nl|<member_id>|<event_id>=<status>=<late>=<note>;..."）
ACTION_CONFIRM = "att_nl"
CANCEL_POSTBACK = "menu|出欠を回答"
NOTE_MAX = 30
POSTBACK_MAX = 300  # LINE の postback data の上限（文字）
_UNSAFE = re.compile(r"[|;=\n]")


def looks_like_attendance(text: str) -> bool:
    return any(word in text for word in HINT_WORDS)


def _is_target(repo: Repository, event, member: Member) -> bool:
    return any(m.member_id == member.member_id for m in resolve_targets(repo, event))


def _candidates(repo: Repository, member: Member, now: datetime) -> list:
    """本人が対象の、受付中の会議（開催順）。始まって間もない会議も含める。"""
    events = sorted(
        (e for e in repo.list_events(status=EventStatus.open)
         if e.datetime_start >= now - LOOKBACK),
        key=lambda e: e.datetime_start,
    )
    return [e for e in events if _is_target(repo, e, member)][:MAX_CANDIDATES]


def _events_text(events: list) -> str:
    return "\n".join(
        f"- id={e.event_id} / {e.type} / {e.title} / "
        f"{e.datetime_start.strftime('%Y-%m-%d')}({WEEKDAYS[e.datetime_start.weekday()]}) "
        f"{e.datetime_start.strftime('%H:%M')}"
        for e in events
    )


def _status_label(status: str, late_leave: str | None) -> str:
    return f"{status}（{late_leave}）" if late_leave else status


def _clean_note(note: str | None) -> str:
    return _UNSAFE.sub(" ", note or "").strip()[:NOTE_MAX]


def encode_answers(member_id: str, answers: list[AttendanceAnswer]) -> str:
    """確認ボタンの postback。長すぎるときはメモを外す。"""
    def build(with_note: bool) -> str:
        items = [
            "=".join([a.event_id, a.status, a.late_leave or "",
                      _clean_note(a.note) if with_note else ""])
            for a in answers
        ]
        return "|".join([ACTION_CONFIRM, member_id, ";".join(items)])

    data = build(True)
    return data if len(data) <= POSTBACK_MAX else build(False)


def decode_answers(data: str) -> tuple[str, list[AttendanceAnswer]]:
    _, member_id, payload = (data.split("|", 2) + ["", ""])[:3]
    answers = []
    for item in payload.split(";"):
        parts = (item.split("=") + ["", "", "", ""])[:4]
        if parts[0] and parts[1]:
            answers.append(AttendanceAnswer(event_id=parts[0], status=parts[1],
                                            late_leave=parts[2] or None, note=parts[3] or None))
    return member_id, answers


def _valid(answer: AttendanceAnswer, events: dict) -> AttendanceAnswer | None:
    """候補の会議・正しい出欠の値だけを残す（遅刻・早退は出席のときだけ）。"""
    if answer.event_id not in events:
        logger.info("読み取った会議が候補外でした: %s", answer.event_id)
        return None
    try:
        status = AttendanceStatus(answer.status)
    except ValueError:
        logger.info("読み取った出欠が不正でした: %s", answer.status)
        return None
    if status == AttendanceStatus.未回答:
        return None
    late = answer.late_leave
    if late not in (LateLeave.遅刻, LateLeave.早退, LateLeave.遅刻早退) or status not in (
            AttendanceStatus.出席, AttendanceStatus.WEB出席):
        late = None
    return AttendanceAnswer(event_id=answer.event_id, status=status.value, late_leave=late,
                            note=_clean_note(answer.note) or None)


def _line(event, answer: AttendanceAnswer) -> str:
    when = event.datetime_start.strftime("%-m月%-d日")
    note = f"（{answer.note}）" if answer.note else ""
    return f"・{when} {event.title}：{_status_label(answer.status, answer.late_leave)}{note}"


def build_confirmation(member: Member, pairs: list, *, in_group: bool) -> Message:
    """「この内容で登録しますか？」の確認（押すのは本人）。"""
    answers = [a for _, a in pairs]
    head = f"{member.name}さん、" if in_group else ""
    lines = [f"{head}出欠の連絡を次の内容で登録します。よろしいですか？",
             *(_line(e, a) for e, a in pairs)]
    qr = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label="この内容で登録", data=encode_answers(member.member_id, answers),
            display_text="この内容で登録")),
        QuickReplyItem(action=PostbackAction(
            label="ちがう", data=CANCEL_POSTBACK, display_text="ちがう（ボタンで回答する）")),
    ])
    return TextMessage(text="\n".join(lines), quick_reply=qr)


def try_attendance_intent(
    repo: Repository, member: Member, text: str, *, now: datetime, in_group: bool = False,
) -> list[Message] | None:
    """出欠の連絡として読み取れたら確認メッセージを返す。読み取れなければ None。"""
    events = _candidates(repo, member, now)
    if not events:
        return None

    today = f"{now:%Y-%m-%d}({WEEKDAYS[now.weekday()]}) {now:%H:%M}"
    outcome = parse_attendance_intent(text, _events_text(events), today)
    if outcome is None:
        record_inference(
            repo, kind="attendance_intent",
            usage=InferenceUsage(model=model_for("attendance_intent")),
            now=now, target=member.member_id, ok=False, error="generation_failed",
        )
        return None
    record_inference(
        repo, kind="attendance_intent", usage=outcome.usage, now=now, target=member.member_id
    )

    intent = outcome.intent
    if not intent.confident:
        return None
    by_id = {e.event_id: e for e in events}
    pairs, seen = [], set()
    for answer in intent.answers:
        valid = _valid(answer, by_id)
        if valid is not None and valid.event_id not in seen:
            seen.add(valid.event_id)
            pairs.append((by_id[valid.event_id], valid))
    if not pairs:
        return None
    return [build_confirmation(member, pairs, in_group=in_group)]


def handle_confirm_postback(
    repo: Repository, member: Member, data: str, *, now: datetime,
) -> list[Message]:
    """［この内容で登録］が押されたら登録する（押した人が本人のときだけ）。"""
    member_id, answers = decode_answers(data)
    if member_id != member.member_id:
        owner = repo.get_member(member_id)
        name = f"{owner.name}さん" if owner else "連絡したご本人"
        return [TextMessage(text=f"この確認は{name}だけが押せます。")]
    done = []
    for answer in answers:
        event = repo.get_event(answer.event_id)
        if event is None or event.status != EventStatus.open or not _is_target(
                repo, event, member):
            continue
        record_attendance(
            repo, event.event_id, member.member_id, AttendanceStatus(answer.status), now=now,
            late_leave=(LateLeave(answer.late_leave) if answer.late_leave else
                        LateLeave.全日程参加 if answer.status in (
                            AttendanceStatus.出席, AttendanceStatus.WEB出席) else None),
            free_text=answer.note or "",  # 前のメモを残さない
        )
        done.append(_line(event, answer))
    if not done:
        return [TextMessage(text="受付中の会議が見つからなかったため、登録しませんでした。"
                                 "メニューの［予定と出欠］から回答してください。")]
    thanks = "出欠を登録しました。ご連絡ありがとうございます。"
    return [TextMessage(text="\n".join([thanks, *done]))]
