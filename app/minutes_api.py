"""議事録の管理 API（docs/meeting-minutes-design.md §7）。

作成・送信は管理画面のボタンからだけ（チャットは検索のみの原則）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from . import minutes as mn
from . import minutes_send as ms
from . import schedule_poll as sp
from .audit import write_audit
from .deps import get_repo
from .models import (
    Event,
    MeetingMinutes,
    MinutesSection,
    MinutesStatus,
)
from .repository import Repository

router = APIRouter(tags=["minutes"])


def _actor(email: str | None) -> str:
    return email or "unknown"


def _event(event_id: str) -> Event:
    event = get_repo().get_event(event_id)
    if event is None:
        raise HTTPException(404, "予定が見つかりません。")
    return event


def _error(exc: mn.MinutesError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


def view(repo: Repository, event: Event) -> dict:
    """管理画面「議事録」の内容。"""
    minutes = repo.get_minutes(event.event_id)
    present, absent = mn.attendees(repo, event)
    base = {
        "configured": mn.is_configured(),
        "attendees": [{"member_id": m.member_id, "name": m.name, "absent": m in absent,
                       "nicknames": m.nicknames} for m in present + absent],
        "minutes": None,
    }
    if minutes is None:
        return base
    names = {m.member_id: m.name for m in repo.list_members()}
    ready = minutes.status == MinutesStatus.ready
    preview, group = None, None
    if ready:
        text, keys = ms.group_body(repo, event, minutes)
        preview = ms.preview_text(text, keys, ms._names(repo))
        group = ms.group_destination(repo, event)
    return base | {
        "minutes": minutes.model_dump(mode="json"),
        "names": names,
        "progress": {"chunk": minutes.chunk,
                     "chunks": len(mn.chunks(minutes.duration_sec or 0))
                     if minutes.duration_sec else None},
        "recording_url": (f"https://drive.google.com/file/d/{minutes.recording_file_id}/view"
                          if minutes.recording_file_id else None),
        "doc_url": ms.doc_url(minutes.doc_id),
        "group_preview": preview,
        "group": group,
        "committees": [{"committee": c, **ms.committee_destinations(repo, c)}
                       for c in sp.committee_names(repo)] if ready else [],
    }


@router.get("/events/{event_id}/minutes")
def get_minutes(event_id: str):
    return view(get_repo(), _event(event_id))


class StartBody(BaseModel):
    recording: str


@router.post("/events/{event_id}/minutes")
def start_minutes(event_id: str, body: StartBody,
                  x_goog_authenticated_user_email: str | None = Header(default=None)):
    """［議事録を作る］録画の Google ドライブの URL（ファイルかフォルダ）を指定する。"""
    repo, event = get_repo(), _event(event_id)
    try:
        mn.start(repo, event, recording=body.recording,
                 actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    return view(repo, event)


@router.post("/events/{event_id}/minutes/resume")
def resume_minutes(event_id: str,
                   x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo, event = get_repo(), _event(event_id)
    try:
        mn.resume(repo, event, actor=_actor(x_goog_authenticated_user_email),
                  now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    return view(repo, event)


class EditBody(BaseModel):
    sections: list[MinutesSection]
    confirmations: list[str] = Field(default_factory=list)
    #: 内々の話のうち、グループ向けに入れるもの
    include_sensitive: list[str] = Field(default_factory=list)


def _ready(repo: Repository, event_id: str) -> MeetingMinutes:
    minutes = repo.get_minutes(event_id)
    if minutes is None or minutes.status != MinutesStatus.ready:
        raise HTTPException(409, "議事録の案がまだできていません。")
    return minutes


@router.put("/events/{event_id}/minutes")
def edit_minutes(event_id: str, body: EditBody,
                 x_goog_authenticated_user_email: str | None = Header(default=None)):
    """議事録の案を直す（議案ごとのまとめ・確認事項・内々の話の扱い）。"""
    repo, event = get_repo(), _event(event_id)
    minutes = _ready(repo, event_id)
    allowed = mn._member_ids(repo, event)
    for s in body.sections:  # 会員は出席予定者から選ぶ
        for o in s.opinions:
            o.by = mn._clean_member(o.by, allowed)
            o.response_by = mn._clean_member(o.response_by, allowed)
        for a in s.action_items:
            a.owner = mn._clean_member(a.owner, allowed)
    minutes.sections = body.sections
    minutes.confirmations = [c for c in body.confirmations if c.strip()]
    for x in minutes.sensitive:
        x.include = x.sensitive_id in body.include_sensitive
    actor = _actor(x_goog_authenticated_user_email)
    minutes.updated_at, minutes.edited_by = datetime.now(), actor
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action="minutes.edit", target=event_id,
                detail=f"{len(minutes.sections)}件")
    return view(repo, event)


class UnclearBody(BaseModel):
    status: str = "resolved"  # resolved（直した）| skipped（このままにする）
    speaker: str | None = None  # 話者を直す（会員ID）
    text: str | None = None  # 発言を直す
    #: 書き起こしの呼ばれ方を、この会員の呼び名として覚える
    remember_nickname: str | None = None


@router.post("/events/{event_id}/minutes/unclear/{unclear_id}")
def resolve_unclear(event_id: str, unclear_id: str, body: UnclearBody,
                    x_goog_authenticated_user_email: str | None = Header(default=None)):
    """不明点を人が直す。話者を直したら、その呼び名を覚えさせることもできる。"""
    repo, event = get_repo(), _event(event_id)
    minutes = _ready(repo, event_id)
    item = next((u for u in minutes.unclear if u.unclear_id == unclear_id), None)
    if item is None:
        raise HTTPException(404, "不明点が見つかりません。")
    if body.status not in ("resolved", "skipped"):
        raise HTTPException(400, "状態の指定が正しくありません。")
    actor = _actor(x_goog_authenticated_user_email)
    seg = mn._segment_at(minutes, item.at)
    speaker = mn._clean_member(body.speaker, mn._member_ids(repo, event))
    if body.speaker and speaker is None:
        raise HTTPException(400, "話者は出席予定者から選んでください。")
    if seg and speaker:
        seg.speaker, seg.speaker_by, seg.evidence = speaker, "human", f"人が確認（{actor}）"
    if seg and body.text and body.text.strip():
        seg.text = body.text.strip()
    item.status = body.status
    item.answer = item.answer or ("人が確認" if body.status == "resolved" else None)
    nickname = (body.remember_nickname or "").strip()
    if nickname and speaker:
        member = repo.get_member(speaker)
        if member and nickname not in member.nicknames:
            repo.upsert_member(member.model_copy(update={"nicknames": [*member.nicknames,
                                                                       nickname]}))
            write_audit(repo, actor=actor, action="member.nickname", target=speaker,
                        detail=nickname)
    minutes.updated_at, minutes.edited_by = datetime.now(), actor
    repo.save_minutes(minutes)
    return view(repo, event)


@router.post("/events/{event_id}/minutes/doc")
def save_minutes_doc(event_id: str,
                     x_goog_authenticated_user_email: str | None = Header(default=None)):
    """［ドキュメントに保存］"""
    repo, event = get_repo(), _event(event_id)
    try:
        ms.save_doc(repo, event, actor=_actor(x_goog_authenticated_user_email),
                    now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    return view(repo, event)


class GroupSendBody(BaseModel):
    expected_group_id: str | None = None


@router.post("/events/{event_id}/minutes/send-group")
def send_minutes_group(event_id: str, body: GroupSendBody,
                       x_goog_authenticated_user_email: str | None = Header(default=None)):
    """［グループに送る］確認ダイアログで見せた送り先と同じときだけ送る。"""
    repo, event = get_repo(), _event(event_id)
    try:
        ms.send_group(repo, event, expected_group_id=body.expected_group_id,
                      actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    return view(repo, event)


class CommitteeBody(BaseModel):
    committee: str


@router.post("/events/{event_id}/minutes/committee")
def make_committee_minutes(event_id: str, body: CommitteeBody,
                           x_goog_authenticated_user_email: str | None = Header(default=None)):
    """［委員会向けを作る］"""
    repo, event = get_repo(), _event(event_id)
    try:
        ms.make_committee(repo, event, body.committee,
                          actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    except RuntimeError as exc:  # AI の呼び出しの失敗
        raise HTTPException(502, f"委員会向けの議事録を作れませんでした: {exc}") from exc
    return view(repo, event)


class CommitteeSendBody(BaseModel):
    committee: str
    channel: str  # user（委員長への個別の LINE）| group（委員会のグループ）
    expected_to_id: str | None = None
    text: str | None = None  # 画面で直した文面


@router.post("/events/{event_id}/minutes/committee/send")
def send_committee_minutes(event_id: str, body: CommitteeSendBody,
                           x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo, event = get_repo(), _event(event_id)
    try:
        ms.send_committee(repo, event, body.committee, channel=body.channel,
                          expected_to_id=body.expected_to_id, text=body.text,
                          actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except mn.MinutesError as exc:
        raise _error(exc) from exc
    return view(repo, event)
