"""議事録の確認・保存・送信（docs/meeting-minutes-design.md §6〜§7）。

管理画面「会議 › 議事録」のボタンから呼ぶ。送るのはいつも人の確認の後で、確認ダイアログに出した
送り先（グループ・委員長）と今の送り先が違えば送らない。グループへの投稿は取り消せない。
- 会議体のグループ: 議案ごとの要点と宿題。内々の話（sensitive）は既定で入れない。
  欠席者と宿題の担当者（LINE 連携済み）にメンションする。
- 委員会向け: 一つの委員会に関わる所だけを AI で抜き出し、委員長への個別の LINE か
  委員会のグループに送る（文面をコピーして専務が送ってもよい）。
"""
from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from linebot.v3.messaging import (
    AllMentionTarget,
    MentionSubstitutionObject,
    TextMessage,
    TextMessageV2,
    UserMentionTarget,
)

from . import committee_docs, line_push, lom_drive
from . import minutes as mn
from . import schedule_poll as sp
from .audit import write_audit
from .guardrails import is_quiet_hours
from .inference import record_inference
from .jpdate import month_day
from .meeting_dispatch import _group
from .models import (
    CommitteeMinutes,
    Event,
    MeetingMinutes,
    Member,
    MinutesSend,
    MinutesStatus,
)
from .repository import Repository

logger = logging.getLogger("jci-agent.minutes")

LINE_LIMIT = 5000
NOTE = "録画から AI でまとめたものです。違うところがあればご指摘ください。"
GOOGLE_DOC = "application/vnd.google-apps.document"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
CHAIR = "委員長"


def _require(repo: Repository, event: Event) -> MeetingMinutes:
    minutes = repo.get_minutes(event.event_id)
    if minutes is None or minutes.status != MinutesStatus.ready:
        raise mn.MinutesError(409, "議事録の案がまだできていません。")
    return minutes


def _names(repo: Repository) -> dict[str, Member]:
    return {m.member_id: m for m in repo.list_members()}


def _title(m: Member | None, fallback: str = "") -> str:
    """役職＋名字（例: 理事長 青木 → 「青木理事長」）。役職がなければ名字＋さん。"""
    if m is None:
        return fallback
    family = m.name.split()[0] if m.name.split() else m.name
    role = m.officer_role or (f"{m.committee}{m.committee_role}" if m.committee_role else "")
    return f"{family}{role}" if role else f"{family}さん"


def _time_range(event: Event) -> str:
    start = event.datetime_start
    end = f"{event.datetime_end:%H:%M}" if event.datetime_end else ""
    return f"{month_day(start)} {start:%H:%M}〜{end}"


# --------------------------------------------------------------------------- #
# 会議体のグループ向けの要点
# --------------------------------------------------------------------------- #
def _mention(key: str) -> str:
    return "{" + key + "}"


def group_body(repo: Repository, event: Event, minutes: MeetingMinutes) -> tuple[str, dict]:
    """グループ向けの要点の本文（メンションは {キー}）と、キー → 会員ID。

    LINE の textV2 の決まりで、本文の { } は {{ }} に直す。
    """
    members = _names(repo)
    present, absent = mn.attendees(repo, event)
    keys: dict[str, str] = {}

    def who(member_id: str | None) -> str:
        m = members.get(member_id or "")
        if m is None:
            return ""
        if not m.line_user_id:
            return _title(m)
        key = next((k for k, v in keys.items() if v == member_id), None) or f"m{len(keys) + 1}"
        keys[key] = member_id
        return _mention(key)

    def esc(text: str) -> str:
        return text.replace("{", "{{").replace("}", "}}")

    lines = [_mention("everyone"),
             esc(f"{event.title}の議事録（要点）を共有します。{NOTE}")]
    absent_linked = [m for m in absent if m.line_user_id]
    if absent_linked:
        lines.append(" ".join(who(m.member_id) for m in absent_linked)
                     + " 欠席でしたので、ご確認ください。")
    place = f" {event.location}" if event.location else ""
    lines += ["", esc(f"【{event.title}】"), esc(_time_range(event) + place)]
    if present:
        lines.append(esc("出席：" + "、".join(_title(m) for m in present)))
    if absent:
        lines.append(esc("欠席：" + "、".join(_title(m) for m in absent)))
    for s in minutes.sections:
        lines += ["", esc(f"■{s.title}")]
        lines += [esc(f"・{x}") for x in s.explained]
        for o in s.opinions:
            text = f"・{o.target + '：' if o.target else ''}{o.point}"
            if o.response:
                text += f"（→{o.response}）"
            lines.append(esc(text))
        lines += [esc(f"・決定：{x}") for x in s.decided]
        for a in s.action_items:
            owner = who(a.owner) if a.owner else ""
            due = f"（{a.due}）" if a.due else ""
            lines.append(esc(f"・{a.what}{due} →") + (owner or "担当未定"))
    included = [x for x in minutes.sensitive if x.include]
    if included:
        lines += ["", "■その他"] + [esc(f"・{x.summary}") for x in included]
    if minutes.confirmations:
        lines += ["", "■確認事項"] + [esc(f"・{x}") for x in minutes.confirmations]
    return "\n".join(lines), keys


def _split(text: str, limit: int = LINE_LIMIT) -> list[str]:
    """5,000字を超えたら、議案の区切り（■）で分ける。"""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    out = []
    for p in parts:  # 1つの議案だけで上限を超えるときは行で切る
        while len(p) > limit:
            cut = p.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(p[:cut])
            p = p[cut:].lstrip("\n")
        out.append(p)
    return out


def _v2(text: str, keys: dict[str, str], members: dict[str, Member]):
    used = {k: v for k, v in keys.items() if _mention(k) in text}
    subs = {k: MentionSubstitutionObject(
        mentionee=UserMentionTarget(userId=members[v].line_user_id)) for k, v in used.items()}
    if _mention("everyone") in text:
        subs["everyone"] = MentionSubstitutionObject(mentionee=AllMentionTarget())
    if not subs:
        return TextMessage(text=text.replace("{{", "{").replace("}}", "}"))
    return TextMessageV2(text=text, substitution=subs)


def group_messages(repo: Repository, event: Event, minutes: MeetingMinutes) -> list:
    text, keys = group_body(repo, event, minutes)
    members = _names(repo)
    return [_v2(part, keys, members) for part in _split(text)]


def preview_text(text: str, keys: dict[str, str], members: dict[str, Member]) -> str:
    """画面に出す文面（メンションは @名前）。"""
    out = text.replace(_mention("everyone"), "@All")
    for k, v in keys.items():
        out = out.replace(_mention(k), f"@{members[v].name}")
    return out.replace("{{", "{").replace("}}", "}")


def group_destination(repo: Repository, event: Event) -> dict:
    group = _group(repo, event)
    return {"group_id": group.group_id if group else None,
            "group_name": group.group_name if group else None}


def _guard(repo: Repository, now: datetime) -> None:
    settings = repo.get_settings()
    if settings.kill_switch:
        raise mn.MinutesError(409, "送信を一時停止しています（設定）。")
    if is_quiet_hours(now, settings):
        raise mn.MinutesError(409, "静音時間のため送れません。時間をおいて送ってください。")


def _record(repo: Repository, event: Event, minutes: MeetingMinutes, send: MinutesSend,
            actor: str) -> None:
    minutes.sends.append(send)
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action=f"minutes.send_{send.kind}", target=event.event_id,
                detail=f"{send.committee + ' ' if send.committee else ''}→ {send.to_name}")


def send_group(repo: Repository, event: Event, *, expected_group_id: str | None, actor: str,
               now: datetime) -> MeetingMinutes:
    minutes = _require(repo, event)
    dest = group_destination(repo, event)
    if not dest["group_id"]:
        raise mn.MinutesError(409, "この会議体の LINE グループが設定にありません"
                                   "（設定 → LINE グループ）。")
    if dest["group_id"] != expected_group_id:
        raise mn.MinutesError(409, "送り先が変わりました。画面を開き直して確かめてください。")
    _guard(repo, now)
    text, _ = group_body(repo, event, minutes)
    if not line_push.push_messages(dest["group_id"], group_messages(repo, event, minutes)):
        raise mn.MinutesError(503, sp.SEND_FAILED)
    _record(repo, event, minutes, MinutesSend(
        kind="group", channel="group", to_id=dest["group_id"], to_name=dest["group_name"],
        text=text, sent_at=now, sent_by=actor), actor)
    return minutes


# --------------------------------------------------------------------------- #
# 委員会向け（§6）
# --------------------------------------------------------------------------- #
_COMMITTEE_PROMPT = """あなたは青年会議所の会議の書記です。会議の書き起こしと議事録の案から、
「{committee}」に関わるところだけを抜き出し、委員長に送る議事録を作ってください。

{materials}

# 議事録の案（JSON）
{draft}

# 書き起こし（[時刻] 話者: 発言）
{transcript}

# 作り方
- その委員会の議案の協議だけでなく、所信・基本計画・予算・年間スケジュールなど他の議題の中で
  その委員会に向けられた意見・決まったこと・宿題も拾う。
- 見出しは「■年間計画への意見」のように、話題ごとにまとめる。意見は誰の意見か（役職＋名字）を書く。
  話者が（話者不明）の発言は人を書かない。
- 意見の理由・背景も短く書く（委員長が委員会で相談できるように）。
- 人事・入会候補者の名前・個人の事情は書かない。書き起こしにないことを足さない。
- 1行目は「【{title}　{committee}に関する議事録】」、2行目に出席・欠席、3行目に
  「※録画の書き起こしから、当委員会に関わる部分を抜き出したものです。」
- 最後に「■今後」として、次の会議などを書く。

# 出力（JSONのみ）
{{"text": "LINE で送る本文（5,000字以内）"}}
"""


def committee_destinations(repo: Repository, committee: str) -> dict:
    """委員会向けの送り先の候補（委員長への個別の LINE・委員会のグループ）。"""
    chair = next((m for m in repo.list_members() if m.is_current and any(
        seat.committee == committee and seat.role == CHAIR for seat in m.committees)
        or (m.is_current and m.committee == committee and m.committee_role == CHAIR)), None)
    group = sp.committee_group(repo, committee)
    return {
        "chair": ({"member_id": chair.member_id, "name": chair.name,
                   "linked": bool(chair.line_user_id)} if chair else None),
        "group": ({"group_id": group.group_id, "group_name": group.group_name}
                  if group else None),
    }


def make_committee(repo: Repository, event: Event, committee: str, *, actor: str,
                   now: datetime) -> MeetingMinutes:
    """委員会向けの議事録を AI で作る（作り直すと置き換える）。"""
    minutes = _require(repo, event)
    if committee not in sp.committee_names(repo):
        raise mn.MinutesError(404, "委員会が見つかりません。")
    present, absent = mn.attendees(repo, event)
    members = _names(repo)
    attend = ("出席：" + "、".join(_title(m) for m in present)
              + ("／欠席：" + "、".join(_title(m) for m in absent) if absent else ""))
    draft = minutes.model_dump(mode="json", include={"sections", "confirmations"})
    for s in draft["sections"]:  # 会員IDを役職＋名字に直して渡す
        for o in s["opinions"]:
            o["by"] = _title(members.get(o["by"] or ""), "（話者不明）")
            o["response_by"] = _title(members.get(o["response_by"] or ""), "") or None
        for a in s["action_items"]:
            a["owner"] = _title(members.get(a["owner"] or ""), "担当未定")
    prompt = _COMMITTEE_PROMPT.format(
        committee=committee, title=event.title,
        materials=mn.materials(repo, event) + f"\n# 出席・欠席\n{attend}",
        draft=json.dumps(draft, ensure_ascii=False), transcript=mn.transcript_text(repo, minutes))
    data, usage = mn.generate("minutes_committee", [{"text": prompt}])
    record_inference(repo, kind="minutes_committee", usage=usage, now=now,
                     target=event.event_id)
    text = str(data.get("text") or "").strip()
    if not text:
        raise mn.MinutesError(502, "委員会向けの議事録を作れませんでした。もう一度お試しください。")
    minutes.committees = [c for c in minutes.committees if c.committee != committee] + [
        CommitteeMinutes(committee=committee, text=text, created_at=now)]
    minutes.updated_at, minutes.edited_by = now, actor
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action="minutes.committee", target=event.event_id,
                detail=committee)
    return minutes


def send_committee(repo: Repository, event: Event, committee: str, *, channel: str,
                   expected_to_id: str | None, text: str | None, actor: str,
                   now: datetime) -> MeetingMinutes:
    """委員会向けの議事録を、委員長への個別の LINE（user）か委員会のグループ（group）へ送る。"""
    minutes = _require(repo, event)
    item = next((c for c in minutes.committees if c.committee == committee), None)
    if item is None:
        raise mn.MinutesError(404, "先に委員会向けの議事録を作ってください。")
    if text is not None and text.strip():  # 画面で直した文面
        item.text = text.strip()
    if len(item.text) > LINE_LIMIT:
        raise mn.MinutesError(400, f"本文が LINE の上限（{LINE_LIMIT}字）を超えています。")
    dest = committee_destinations(repo, committee)
    if channel == "user":
        chair = dest["chair"]
        if not chair or not chair["linked"]:
            raise mn.MinutesError(409, "委員長が LINE と連携していません。")
        to_id, to_name = chair["member_id"], f"{chair['name']}（{committee}{CHAIR}）"
        line_to = repo.get_member(to_id).line_user_id
    elif channel == "group":
        group = dest["group"]
        if not group:
            raise mn.MinutesError(409, f"「{committee}」の LINE グループが見つかりません。")
        to_id, to_name, line_to = group["group_id"], group["group_name"], group["group_id"]
    else:
        raise mn.MinutesError(400, "送り先の指定が正しくありません。")
    if to_id != expected_to_id:
        raise mn.MinutesError(409, "送り先が変わりました。画面を開き直して確かめてください。")
    _guard(repo, now)
    if not line_push.push_messages(line_to, [TextMessage(text=item.text)]):
        raise mn.MinutesError(503, sp.SEND_FAILED)
    _record(repo, event, minutes, MinutesSend(
        kind="committee", channel=channel, to_id=to_id, to_name=to_name, committee=committee,
        text=item.text, sent_at=now, sent_by=actor), actor)
    return minutes


# --------------------------------------------------------------------------- #
# ドキュメントに保存（§6）
# --------------------------------------------------------------------------- #
def doc_html(repo: Repository, event: Event, minutes: MeetingMinutes) -> str:
    """議事録のドキュメントの中身（HTML。ドライブが Google ドキュメントに変換する）。"""
    members = _names(repo)
    present, absent = mn.attendees(repo, event)
    e = html.escape

    def name(member_id: str | None, fallback: str = "") -> str:
        return e(_title(members.get(member_id or ""), fallback))

    out = [f"<h1>{e(event.title)} 議事録</h1>",
           f"<p>日時：{e(_time_range(event))}"
           + (f"<br>場所：{e(event.location)}" if event.location else "") + "<br>"
           f"出席：{e('、'.join(_title(m) for m in present))}<br>"
           f"欠席：{e('、'.join(_title(m) for m in absent) or 'なし')}</p>",
           f"<p><i>{e(NOTE)}</i></p>"]
    for s in minutes.sections:
        out.append(f"<h2>{e(s.title)}</h2>")
        if s.explained:
            out.append("<h3>説明</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in s.explained)
                       + "</ul>")
        if s.opinions:
            out.append("<h3>意見</h3><ul>" + "".join(
                f"<li>{name(o.by, '（話者不明）')}：{e(o.target + '　' if o.target else '')}"
                f"{e(o.point)}" + (f"<br>→ {name(o.response_by)} {e(o.response)}"
                                   if o.response else "") + "</li>" for o in s.opinions)
                + "</ul>")
        if s.decided:
            out.append("<h3>決まったこと</h3><ul>"
                       + "".join(f"<li>{e(x)}</li>" for x in s.decided) + "</ul>")
        if s.action_items:
            out.append("<h3>宿題</h3><ul>" + "".join(
                f"<li>{e(a.what)}（{name(a.owner, '担当未定')}"
                + (f"・{e(a.due)}" if a.due else "") + "）</li>" for a in s.action_items)
                + "</ul>")
    if minutes.confirmations:
        out.append("<h2>確認事項</h2><ul>"
                   + "".join(f"<li>{e(x)}</li>" for x in minutes.confirmations) + "</ul>")
    out.append("<h2>付録：書き起こし</h2>")
    for seg in minutes.segments:
        who = name(seg.speaker, "（話者不明）")
        out.append(f"<p>[{e(seg.start)}] {who}：{e(seg.text)}</p>")
    return "<html><head><meta charset='utf-8'></head><body>" + "".join(out) + "</body></html>"


def _upload(doc_id: str | None, name: str, folder: str | None, body: str) -> str:
    """HTML を Google ドキュメントとして作る（doc_id があれば同じファイルを置き換える）。"""
    boundary = f"minutes{uuid.uuid4().hex}"
    meta: dict = {"name": name}
    if doc_id is None:
        meta["mimeType"] = GOOGLE_DOC
        if folder:
            meta["parents"] = [folder]
    payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
               f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\n"
               f"Content-Type: text/html; charset=UTF-8\r\n\r\n{body}\r\n--{boundary}--")
    url = (f"{DRIVE_UPLOAD}/{doc_id}" if doc_id else DRIVE_UPLOAD) \
        + "?uploadType=multipart&supportsAllDrives=true&fields=id"
    req = urllib.request.Request(url, data=payload.encode(), method="PATCH" if doc_id else "POST")
    req.add_header("Authorization", f"Bearer {lom_drive._token()}")
    req.add_header("Content-Type", f"multipart/related; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["id"]


def save_doc(repo: Repository, event: Event, *, actor: str, now: datetime) -> MeetingMinutes:
    """LOM のドライブに保存する（作り直しは同じファイルを更新し、URL を変えない）。"""
    minutes = _require(repo, event)
    folder = repo.get_settings().minutes.folder_id or minutes.recording_folder_id
    name = f"{event.title} 議事録"
    try:
        minutes.doc_id = _upload(minutes.doc_id, name, folder, doc_html(repo, event, minutes))
    except urllib.error.HTTPError as exc:
        raise mn.MinutesError(503, f"ドキュメントに保存できませんでした（{exc.code}）。") from exc
    except RuntimeError as exc:  # OAuth の情報がない
        raise mn.MinutesError(503, f"LOM のドライブを使う設定がありません: {exc}") from exc
    minutes.updated_at = now
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action="minutes.save_doc", target=event.event_id,
                detail=minutes.doc_id)
    return minutes


def doc_url(doc_id: str | None) -> str | None:
    return committee_docs.doc_url(doc_id) if doc_id else None
