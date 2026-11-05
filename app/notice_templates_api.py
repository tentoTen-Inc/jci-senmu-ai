"""通知テンプレートと会議の配信物の管理 API（docs/meeting-notice-design.md §4・§7.1）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from . import meeting_dispatch as md
from . import notice_layout as nl
from . import notice_template as nt
from .audit import write_audit
from .deps import get_repo
from .models import Event, NoticeTemplate

router = APIRouter(tags=["notice-templates"])
CHANNELS = ("line", "email")
MENTIONS = ("none", "all")


def _actor(email: str | None) -> str:
    return email or "unknown"


class TemplateBody(BaseModel):
    name: str
    channel: str = "line"
    purpose: str = "agenda"
    event_types: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    mention: str = "none"
    is_default: bool = False
    archived: bool = False


def _checked(body: TemplateBody) -> None:
    if not body.name.strip():
        raise HTTPException(400, "テンプレートの名前を入れてください。")
    if body.channel not in CHANNELS or body.mention not in MENTIONS:
        raise HTTPException(400, "送り方（LINE／メール）かメンションの指定が正しくありません。")
    if not body.body.strip():
        raise HTTPException(400, "本文を入れてください。")
    errors = nt.validate(body.body, body.subject)
    if errors:
        raise HTTPException(400, "テンプレートの書き方に誤りがあります: " + "／".join(errors))


def _view(t: NoticeTemplate) -> dict:
    return t.model_dump(mode="json")


@router.get("/notice-templates")
def list_templates(include_archived: bool = False):
    return {
        "templates": [_view(t) for t in
                      nt.list_templates(get_repo(), include_archived=include_archived)],
        "fields": sorted(nt.FIELDS | nt.FLAGS),
        "lists": {k: sorted(v) for k, v in nt.LISTS.items()},
    }


def _save(template_id: str, body: TemplateBody, actor: str, *, builtin: bool) -> NoticeTemplate:
    _checked(body)
    repo = get_repo()
    template = NoticeTemplate(template_id=template_id, builtin=builtin,
                              updated_at=datetime.now(), updated_by=actor,
                              **body.model_dump())
    repo.save_notice_template(template)
    write_audit(repo, actor=actor, action="notice_template.save", target=template_id,
                detail=template.name)
    return template


@router.post("/notice-templates")
def create_template(body: TemplateBody,
                    x_goog_authenticated_user_email: str | None = Header(default=None)):
    template_id = f"tpl_{uuid.uuid4().hex[:10]}"
    return _view(_save(template_id, body, _actor(x_goog_authenticated_user_email),
                       builtin=False))


@router.put("/notice-templates/{template_id}")
def update_template(template_id: str, body: TemplateBody,
                    x_goog_authenticated_user_email: str | None = Header(default=None)):
    current = nt.get_template(get_repo(), template_id)
    if current is None:
        raise HTTPException(404, "テンプレートが見つかりません。")
    return _view(_save(template_id, body, _actor(x_goog_authenticated_user_email),
                       builtin=current.builtin))


@router.post("/notice-templates/{template_id}/reset")
def reset_template(template_id: str,
                   x_goog_authenticated_user_email: str | None = Header(default=None)):
    """既定のテンプレートを、リポジトリの文面に戻す。"""
    default = next((t for t in nt.defaults() if t.template_id == template_id), None)
    if default is None:
        raise HTTPException(404, "既定のテンプレートではありません。")
    repo = get_repo()
    actor = _actor(x_goog_authenticated_user_email)
    repo.save_notice_template(default.model_copy(update={"updated_at": datetime.now(),
                                                         "updated_by": actor}))
    write_audit(repo, actor=actor, action="notice_template.reset", target=template_id)
    return _view(default)


class PreviewBody(BaseModel):
    body: str
    subject: str = ""
    channel: str = "line"
    event_id: str | None = None


@router.post("/notice-templates/preview")
def preview(req: PreviewBody):
    """書きかけの本文を、選んだ会議（なければ例の値）で差し込んで見せる。メールは HTML も返す。"""
    errors = nt.validate(req.body, req.subject)
    if errors:
        return {"errors": errors, "text": None, "subject": None, "html": None}
    repo = get_repo()
    event = repo.get_event(req.event_id) if req.event_id else None
    now = datetime.now()
    if event:
        dispatch = md.get_dispatch(repo, event.event_id)
        ctx = md.email_context(repo, event, dispatch, now)[0] if req.channel == "email" \
            else md.context(repo, event, dispatch, now)
    else:
        ctx = nt.sample_context()
    text = nt.render(req.body, ctx)
    html = nl.to_html(text, repo.get_settings().email_style) if req.channel == "email" else None
    return {"errors": [], "text": text, "html": html,
            "subject": nt.render(req.subject, ctx) if req.subject else None}


# --------------------------------------------------------------------------- #
# 会議の「案内・配信」
# --------------------------------------------------------------------------- #
def _event(event_id: str) -> Event:
    event = get_repo().get_event(event_id)
    if event is None:
        raise HTTPException(404, "予定が見つかりません。")
    return event


def _error(exc: md.DispatchError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/events/{event_id}/dispatch")
def get_dispatch(event_id: str, template_id: str | None = None,
                 email_template_id: str | None = None):
    return md.view(get_repo(), _event(event_id), datetime.now(), template_id, email_template_id)


class DispatchBody(BaseModel):
    agenda_url: str | None = None
    opinion_url: str | None = None


@router.put("/events/{event_id}/dispatch")
def update_dispatch(event_id: str, body: DispatchBody,
                    x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo, event = get_repo(), _event(event_id)
    for url in (body.agenda_url, body.opinion_url):
        if url and not url.strip().startswith("https://"):
            raise HTTPException(400, "URL は https:// で始まるものを入れてください。")
    md.update(repo, event, agenda_url=body.agenda_url, opinion_url=body.opinion_url,
              actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    return md.view(repo, event, datetime.now())


class SendBody(BaseModel):
    template_id: str
    expected_group_id: str | None = None


@router.post("/events/{event_id}/dispatch/line")
def send_line(event_id: str, body: SendBody,
              x_goog_authenticated_user_email: str | None = Header(default=None)):
    repo, event = get_repo(), _event(event_id)
    try:
        md.send_line(repo, event, template_id=body.template_id,
                     expected_group_id=body.expected_group_id,
                     actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except md.DispatchError as exc:
        raise _error(exc) from exc
    except nt.TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc
    return md.view(repo, event, datetime.now(), body.template_id)


class DraftBody(BaseModel):
    template_id: str
    expected_count: int


@router.post("/events/{event_id}/dispatch/email-draft")
def make_email_draft(event_id: str, body: DraftBody,
                     x_goog_authenticated_user_email: str | None = Header(default=None)):
    """案内メールを LOM の Gmail の下書きに作る（送るのは専務が Gmail で）。"""
    repo, event = get_repo(), _event(event_id)
    try:
        md.make_draft(repo, event, template_id=body.template_id,
                      expected_count=body.expected_count,
                      actor=_actor(x_goog_authenticated_user_email), now=datetime.now())
    except md.DispatchError as exc:
        raise _error(exc) from exc
    except nt.TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc
    return md.view(repo, event, datetime.now(), None, body.template_id)
