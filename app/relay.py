"""外部グループからの周知の転送（docs/line-relay-design.md）。

R-1: 取り込み。
- ワーカー（`line_worker.handle_envelope`）が、取込元に指定したグループの投稿を
  `RelayPost` として溜める
- `/tasks/relay`（5分おき）が、同じ送り主・同じグループで10分以内に続いた投稿を1件にまとめ、
  最後の投稿から10分たったものを `ExternalNotice(source="line_group")` として保存する

取込元はグループごとに管理画面で指定する（`LineGroup.relay_source`）。ボットがいるだけの
グループは取り込まない。グループ名・ID はコードに持たない（全国の LOM で使えるように）。

R-2: 判定・確認・転送。
- AI（Flash）が share / officer / other を判定し、確認用の要約を作る（本文は書き換えない）
- share は専務理事の個人 LINE に確認カード。［送る］→［送信する］の2段階で承認したときだけ、
  会員全員のグループ（設定の案内先 all）へ「@All【出所名より】＋原文」を予約投稿で送る
- officer は知らせるだけ。静音時間はカードを待たせ、7日押されなければ期限切れ
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta

from linebot.v3.messaging import (
    AllMentionTarget,
    FlexContainer,
    FlexMessage,
    MentionSubstitutionObject,
    Message,
    TextMessage,
    TextMessageV2,
)

from . import file_links, line_push, lom
from .audit import write_audit
from .delivery import execute_delivery
from .guardrails import is_quiet_hours, next_send_time
from .inference import record_inference
from .jpdate import JST
from .llm import judge_relay, model_for
from .models import (
    DeliveryJob,
    DeliveryStatus,
    ExternalNotice,
    InferenceUsage,
    Member,
    MemberStatus,
    NoticeAttachment,
    NoticeHistory,
    NoticeRelay,
    RelayPost,
    ScheduledPost,
)
from .repository import Repository
from .sed_notice import NOTIFY_ROLES, notify_sed

logger = logging.getLogger("jci-agent.relay")

MERGE_GAP = timedelta(minutes=10)  # これより間が空いたら別の連絡
#: 取込元の指定を読み直す間隔（Firestore の読み取りを減らす）
SOURCE_CACHE_TTL = timedelta(minutes=5)
SUBJECT_LIMIT = 40
APPROVAL_TTL = timedelta(days=7)  # 確認カードを送ってから承認を待つ期間
JUDGE_RETRY = timedelta(minutes=30)  # 判定に失敗したときに、やり直すまでの間
JUDGE_MAX_TRIES = 3  # これだけ失敗したら、判定なしで専務に確認してもらう
ACTION = "rly"  # postback の接頭辞
LINE_TEXT_LIMIT = 5000  # LINE のテキスト1通の上限
PREVIEW_LIMIT = 600  # 確認カードに出す文面の長さ
MENTION_KEY = "everyone"
KIND_LABELS = {"share": "会員への周知", "officer": "役員あて", "other": "対象外"}

#: プロセス内キャッシュ（グループID → (確認した時刻, 取込元か)）
_source_cache: dict[str, tuple[datetime, bool]] = {}


def _group_id(payload: dict) -> str | None:
    source = payload.get("source") or {}
    if source.get("type") != "group":
        return None
    return source.get("groupId")


def is_source(repo: Repository, payload: dict, now: datetime) -> bool:
    """投稿が取込元グループのものか。"""
    group_id = _group_id(payload)
    if not group_id:
        return False
    cached = _source_cache.get(group_id)
    if cached and now - cached[0] < SOURCE_CACHE_TTL:
        return cached[1]
    group = repo.get_line_group(group_id)
    flag = bool(group and group.relay_source and group.left_at is None)
    _source_cache[group_id] = (now, flag)
    return flag


def forget_source(group_id: str) -> None:
    """管理画面で取込元の指定を変えたとき、キャッシュを捨てる。"""
    _source_cache.pop(group_id, None)


def sent_at(payload: dict, now: datetime) -> datetime:
    """LINE の timestamp（ミリ秒）を JST の naive datetime に。無ければ受け取った時刻。"""
    ms = payload.get("timestamp")
    if not isinstance(ms, (int, float)):
        return now
    return datetime.fromtimestamp(ms / 1000, JST).replace(tzinfo=None)


def record_post(repo: Repository, payload: dict, now: datetime,
                attachment: NoticeAttachment | None = None) -> RelayPost | None:
    """取込元グループの投稿を溜める。テキストとファイル以外（スタンプ等）は溜めない。"""
    message = payload.get("message") or {}
    message_id = message.get("id")
    group_id = _group_id(payload)
    if not (message_id and group_id):
        return None
    text = message.get("text") if message.get("type") == "text" else None
    if not text and attachment is None:
        return None
    post = RelayPost(
        message_id=message_id, group_id=group_id,
        user_id=(payload.get("source") or {}).get("userId"),
        sent_at=sent_at(payload, now), text=text, attachment=attachment,
    )
    repo.save_relay_post(post)
    return post


def drop_unsent(repo: Repository, payload: dict) -> None:
    """送信取消された投稿は、まとめる前なら取り除く（取り消された文面を転送しない）。"""
    message_id = (payload.get("unsend") or {}).get("messageId")
    if message_id:
        repo.delete_relay_posts([message_id])


# --------------------------------------------------------------------------- #
# まとめて確定する
# --------------------------------------------------------------------------- #
def _clusters(posts: list[RelayPost]) -> list[list[RelayPost]]:
    """同じグループ・同じ送り主で、間が10分以内に続いた投稿の塊に分ける。"""
    by_sender: dict[tuple[str, str | None], list[RelayPost]] = {}
    for post in posts:
        by_sender.setdefault((post.group_id, post.user_id), []).append(post)
    clusters: list[list[RelayPost]] = []
    for items in by_sender.values():
        items.sort(key=lambda p: (p.sent_at, p.message_id))
        current = [items[0]]
        for post in items[1:]:
            if post.sent_at - current[-1].sent_at > MERGE_GAP:
                clusters.append(current)
                current = []
            current.append(post)
        clusters.append(current)
    return clusters


def notice_id_for(source_ref: str) -> str:
    """冪等キーから決まる ID（同時に2回確定しても同じ文書に書く）。"""
    return "lg_" + hashlib.sha256(source_ref.encode()).hexdigest()[:20]


def _subject(body: str, attachments: list[NoticeAttachment]) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line[:SUBJECT_LIMIT]
    if attachments:
        return attachments[0].name[:SUBJECT_LIMIT]
    return "（本文なし）"


def _fetch_sender_name(group_id: str, user_id: str | None) -> str | None:
    """送り主のグループでの表示名（取れなければ None）。"""
    if not user_id:
        return None
    from . import line_worker  # 循環 import を避ける

    try:
        status, _, raw = line_worker._line_get(
            f"{line_worker.LINE_API}/group/{group_id}/member/{user_id}")
    except Exception:  # noqa: BLE001 - 名前が無くても取り込みは続ける
        logger.warning("送り主の名前を取得できません: group=%s", group_id)
        return None
    if status != 200:
        return None
    try:
        return json.loads(raw).get("displayName")
    except ValueError:
        return None


def build_notice(cluster: list[RelayPost], now: datetime,
                 from_name: str | None = None) -> ExternalNotice:
    first = cluster[0]
    source_ref = f"{first.group_id}:{first.message_id}"
    body = "\n\n".join(p.text for p in cluster if p.text)
    attachments = [p.attachment for p in cluster if p.attachment is not None]
    return ExternalNotice(
        notice_id=notice_id_for(source_ref),
        source="line_group",
        source_ref=source_ref,
        received_at=first.sent_at,
        from_name=from_name,
        subject=_subject(body, attachments),
        body_text=body,
        attachments=attachments,
        history=[NoticeHistory(at=now, action="imported_line_group")],
        relay=NoticeRelay(group_id=first.group_id,
                          message_ids=[p.message_id for p in cluster]),
    )


def finalize(repo: Repository, now: datetime) -> list[ExternalNotice]:
    """最後の投稿から10分たった塊を対外連絡として保存し、溜めた投稿を消す。保存した連絡を返す。"""
    posts = repo.list_relay_posts()
    if not posts:
        return []
    created: list[ExternalNotice] = []
    for cluster in _clusters(posts):
        if now - cluster[-1].sent_at < MERGE_GAP:
            continue  # まだ続きが来るかもしれない
        first = cluster[0]
        source_ref = f"{first.group_id}:{first.message_id}"
        if repo.get_notice_by_source_ref(source_ref) is None:
            notice = build_notice(cluster, now,
                                  from_name=_fetch_sender_name(first.group_id, first.user_id))
            repo.upsert_notice(notice)
            created.append(notice)
            logger.info("外部グループの投稿を取り込みました: %s（%d件）",
                        notice.notice_id, len(cluster))
        repo.delete_relay_posts([p.message_id for p in cluster])
    return created


# --------------------------------------------------------------------------- #
# 判定（AI・助言。docs/line-relay-design.md §3）
# --------------------------------------------------------------------------- #
def _label(repo: Repository, group_id: str) -> str:
    group = repo.get_line_group(group_id)
    return (group.relay_label or group.group_name or "外部グループ") if group else "外部グループ"


def judge(repo: Repository, notice: ExternalNotice, now: datetime) -> ExternalNotice:
    """AI の判定を付ける。何度も失敗したら share として専務に見てもらう（取りこぼさない）。"""
    relay = notice.relay
    outcome = judge_relay(_label(repo, relay.group_id), notice.body_text,
                          [a.name for a in notice.attachments])
    if outcome is None:
        tries = relay.judge_tries + 1
        record_inference(repo, kind="relay_judge",
                         usage=InferenceUsage(model=model_for("relay_judge")), now=now,
                         target=notice.notice_id, ok=False, error="generation_failed")
        update = {"judge_tries": tries, "judge_failed_at": now}
        if tries >= JUDGE_MAX_TRIES:
            update |= {"kind": "share", "judged_at": now,
                       "summary": "（AI の判定に失敗しました。原文をご確認ください）"}
        relay = relay.model_copy(update=update)
    else:
        record_inference(repo, kind="relay_judge", usage=outcome.usage, now=now,
                         target=notice.notice_id)
        relay = relay.model_copy(update={"kind": outcome.kind, "summary": outcome.summary,
                                         "deadline": outcome.deadline, "judged_at": now})
    notice = notice.model_copy(update={"relay": relay})
    repo.upsert_notice(notice)
    return notice


# --------------------------------------------------------------------------- #
# 転送する文面（§4.2）
# --------------------------------------------------------------------------- #
def relay_text(repo: Repository, notice: ExternalNotice) -> str:
    """転送する文面（メンションを除く）。見出し＋原文そのまま。"""
    head = f"【{_label(repo, notice.relay.group_id)}より】"
    return f"{head}\n{notice.body_text}" if notice.body_text else head


def _attachment_lines(notice: ExternalNotice) -> list[str]:
    """添付ファイルを開くリンク（署名付き・30日）。リンクを作れないものは名前だけ。"""
    base = file_links.public_base_url()
    lines = []
    for att in notice.attachments:
        message_id = None
        if att.storage_uri and "/line/content/" in att.storage_uri:
            message_id = att.storage_uri.split("/line/content/", 1)[1].split("/", 1)[0]
        token = (file_links.make_token(message_id, notice.relay.group_id)
                 if message_id and base else None)
        lines.append(f"📎 {att.name}" + (f"\n{base}/files/{token}" if token else ""))
    return lines


def _chunks(text: str, limit: int) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def relay_messages(repo: Repository, notice: ExternalNotice) -> list[Message]:
    """グループへ送るメッセージ（@All＋見出し＋原文。長ければ分ける。LINE は1回5通まで）。"""
    text = relay_text(repo, notice)
    files = _attachment_lines(notice)
    if files:
        text += "\n\n" + "\n".join(files)
    parts = _chunks(text, LINE_TEXT_LIMIT - 20)
    if len(parts) > 5:
        raise ValueError("文面が長すぎて LINE の1回の送信（5通）に収まりません。")
    escaped = parts[0].replace("{", "{{").replace("}", "}}")
    first = TextMessageV2(
        text=f"{{{MENTION_KEY}}}\n{escaped}",
        substitution={MENTION_KEY: MentionSubstitutionObject(mentionee=AllMentionTarget())})
    return [first, *(TextMessage(text=p) for p in parts[1:])]


def target_group(repo: Repository) -> tuple[str | None, str]:
    """転送先（設定の案内先「会員全員」）の ID と名前。"""
    group_id = repo.get_settings().line_groups.get("all")
    if not group_id:
        return None, "（未設定）"
    group = repo.get_line_group(group_id)
    return group_id, (group.group_name if group and group.group_name else "会員全員のグループ")


# --------------------------------------------------------------------------- #
# 専務への確認カード（§4.1）
# --------------------------------------------------------------------------- #
def _approvers(repo: Repository) -> list[Member]:
    return [m for m in repo.list_members()
            if m.officer_role in NOTIFY_ROLES and m.line_user_id
            and m.status == MemberStatus.active]


def _text(text: str, **kw) -> dict:
    return {"type": "text", "text": text, "wrap": True, "size": "sm", **kw}


def _button(label: str, data: str, style: str = "secondary") -> dict:
    return {"type": "button", "style": style, "height": "sm",
            "action": {"type": "postback", "label": label, "data": data,
                       "displayText": label}}


def card_message(repo: Repository, notice: ExternalNotice) -> FlexMessage:
    relay = notice.relay
    label = _label(repo, relay.group_id)
    _, group_name = target_group(repo)
    preview = relay_text(repo, notice)
    if len(preview) > PREVIEW_LIMIT:
        preview = preview[:PREVIEW_LIMIT] + "…（続きは管理画面）"
    if notice.attachments:
        preview += "\n" + "\n".join(f"📎 {a.name}" for a in notice.attachments)
    contents = [
        _text(f"【転送の確認】{label}のグループから", weight="bold"),
        _text(f"要約: {relay.summary or '—'}", margin="md"),
    ]
    if relay.deadline:
        contents.append(_text(f"期限: {relay.deadline:%-m月%-d日}"))
    contents += [
        _text(f"送信先: {group_name}（@All）"),
        {"type": "separator", "margin": "md"},
        _text(preview, margin="md", color="#555555"),
    ]
    buttons = [_button("送る", f"{ACTION}|send|{notice.notice_id}", "primary"),
               _button("送らない", f"{ACTION}|no|{notice.notice_id}")]
    url = lom.admin_url("/app/notices")
    if url:
        buttons.append({"type": "button", "style": "link", "height": "sm",
                        "action": {"type": "uri", "label": "管理画面で直す", "uri": url}})
    bubble = {
        "type": "bubble", "size": "giga",
        "body": {"type": "box", "layout": "vertical", "contents": contents},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons},
    }
    alt = f"【転送の確認】{label}: {relay.summary or notice.subject}"[:400]
    return FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(bubble))


def send_card(repo: Repository, notice: ExternalNotice, now: datetime) -> bool | None:
    """専務理事に確認カードを送る。送れた True、送る相手がいない False、静音時間など保留 None。"""
    approvers = _approvers(repo)
    if not approvers:
        return False
    message = card_message(repo, notice)
    job = DeliveryJob(job_id=f"relay_card_{now:%Y%m%d%H%M%S}_{uuid.uuid4().hex[:4]}",
                      type="relay_card", targets=[m.member_id for m in approvers],
                      scheduled_at=now, status=DeliveryStatus.queued)
    repo.save_delivery_job(job)
    report = execute_delivery(repo, job, message.alt_text, now=now,
                              sender=line_push.member_messages_sender(repo, [message]))
    if report.halted or report.deferred:
        return None
    return report.sent > 0


def _officer_text(repo: Repository, notice: ExternalNotice) -> str:
    relay = notice.relay
    body = notice.body_text if len(notice.body_text) <= PREVIEW_LIMIT \
        else notice.body_text[:PREVIEW_LIMIT] + "…"
    return (f"【役員あての連絡】{_label(repo, relay.group_id)}のグループに届きました"
            f"（会員への転送はしません）。\n要約: {relay.summary or '—'}\n\n{body}")


# --------------------------------------------------------------------------- #
# 5分おきの処理（/tasks/relay）
# --------------------------------------------------------------------------- #
def _pending(repo: Repository) -> list[ExternalNotice]:
    return [n for n in repo.list_notices(status="new")
            if n.source == "line_group" and n.relay is not None]


def process(repo: Repository, now: datetime) -> dict:
    """まとめて確定 → 判定 → 確認カード・知らせ → 期限切れ。"""
    created = finalize(repo, now)
    result = {"created": [n.notice_id for n in created], "judged": 0, "cards": 0,
              "officer": 0, "expired": 0}
    quiet = is_quiet_hours(now, repo.get_settings())
    for notice in _pending(repo):
        relay = notice.relay
        try:
            if relay.kind is None:
                if relay.judge_failed_at and now - relay.judge_failed_at < JUDGE_RETRY:
                    continue
                notice = judge(repo, notice, now)
                relay = notice.relay
                result["judged"] += 1
            if relay.expired_at or relay.approved_at:
                continue
            if relay.card_sent_at is None and relay.kind in ("share", "officer") and not quiet:
                if relay.kind == "share":
                    sent = send_card(repo, notice, now)
                    action = "notice.relay.card"
                else:
                    sent = True if notify_sed(repo, _officer_text(repo, notice), now,
                                              "relay_officer") else None
                    action = "notice.relay.officer"
                if sent is None:
                    continue  # 静音時間・停止中。次の回に送る
                if not sent:
                    # 専務理事が LINE 連携していない・届かなかった。記録せず次の回に送り直す
                    logger.warning("確認カードを専務理事に届けられません: %s", notice.notice_id)
                    continue
                notice = notice.model_copy(update={
                    "relay": relay.model_copy(update={"card_sent_at": now}),
                    "history": [*notice.history, NoticeHistory(at=now, action=action)]})
                repo.upsert_notice(notice)
                write_audit(repo, actor="system", action=action, target=notice.notice_id,
                            detail=relay.summary)
                result["cards" if relay.kind == "share" else "officer"] += 1
            elif (relay.kind == "share" and relay.card_sent_at
                  and now - relay.card_sent_at >= APPROVAL_TTL):
                notice = notice.model_copy(update={
                    "relay": relay.model_copy(update={"expired_at": now}),
                    "history": [*notice.history, NoticeHistory(at=now, action="relay_expired")]})
                repo.upsert_notice(notice)
                write_audit(repo, actor="system", action="notice.relay.expire",
                            target=notice.notice_id)
                result["expired"] += 1
        except Exception:  # noqa: BLE001 - 1件の失敗で他を止めない
            logger.exception("周知の転送の処理に失敗: %s", notice.notice_id)
    return result


# --------------------------------------------------------------------------- #
# ボタン（postback。§4.1）
# --------------------------------------------------------------------------- #
def _reply(text: str) -> list[Message]:
    return [TextMessage(text=text)]


def confirm_message(repo: Repository, notice: ExternalNotice) -> FlexMessage:
    _, group_name = target_group(repo)
    text = f"「{group_name}」に @All で送ります。グループへの送信は取り消せません。"
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical",
                 "contents": [_text(text), _text(f"件名: {notice.subject}", color="#555555",
                                                  margin="md")]},
        "footer": {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
            _button("送信する", f"{ACTION}|go|{notice.notice_id}", "primary"),
            _button("やめる", f"{ACTION}|cancel|{notice.notice_id}")]},
    }
    return FlexMessage(alt_text=text, contents=FlexContainer.from_dict(bubble))


def handle_postback(repo: Repository, member: Member, data: str,
                    now: datetime) -> list[Message]:
    """確認カードのボタン。押せるのは専務理事だけ。"""
    parts = data.split("|")
    if len(parts) != 3:
        return _reply("ボタンの内容を読み取れませんでした。")
    _, action, notice_id = parts
    if member.officer_role not in NOTIFY_ROLES:
        return _reply("この確認は専務理事だけが操作できます。")
    notice = repo.get_notice(notice_id)
    if notice is None or notice.relay is None:
        return _reply("連絡が見つかりません。")
    relay = notice.relay
    if relay.post_id or relay.sent_at:
        return _reply("この連絡はすでに転送を受け付けています。")
    if notice.status == "archived":
        return _reply("この連絡は「送らない」にしています。送るときは管理画面からどうぞ。")
    if relay.expired_at:
        return _reply("確認の期限（7日）が過ぎました。送るときは管理画面からどうぞ。")
    actor = f"line:{member.member_id}"
    if action == "cancel":
        return _reply("やめました。転送はしていません。")
    if action == "no":
        notice = notice.model_copy(update={
            "status": "archived",
            "history": [*notice.history, NoticeHistory(at=now, action="relay_rejected",
                                                       by=actor)]})
        repo.upsert_notice(notice)
        write_audit(repo, actor=actor, action="notice.relay.reject", target=notice_id)
        return _reply("送らないことにしました。管理画面の「対外連絡」には残っています。")
    if action == "send":
        if target_group(repo)[0] is None:
            return _reply("転送先（設定の案内先「会員全員」）が決まっていません。"
                          "管理画面の設定で選んでください。")
        return [confirm_message(repo, notice)]
    if action == "go":
        return approve(repo, notice, actor, member.member_id, now)
    return _reply("ボタンの内容を読み取れませんでした。")


def approve(repo: Repository, notice: ExternalNotice, actor: str, member_id: str | None,
            now: datetime) -> list[Message]:
    """承認して予約投稿を作り、静音時間でなければその場で送る。"""
    from . import line_link  # 循環 import を避ける

    group_id, group_name = target_group(repo)
    if group_id is None:
        return _reply("転送先（設定の案内先「会員全員」）が決まっていません。")
    try:
        relay_messages(repo, notice)  # 長すぎるなどで送れないものは、ここで止める
    except ValueError as exc:
        return _reply(f"{exc} 管理画面から分けて送ってください。")
    # 二度押し・同時押しで二重に送らない
    if not repo.claim_once(f"relay_approve:{notice.notice_id}"):
        return _reply("この連絡はすでに転送を受け付けています。")
    send_at = next_send_time(now, repo.get_settings())
    post = ScheduledPost(post_id=f"relay_{notice.notice_id}", group_id=group_id, kind="relay",
                         notice_id=notice.notice_id, send_at=send_at, created_at=now,
                         created_by=actor)
    repo.save_scheduled_post(post)
    notice = notice.model_copy(update={
        "relay": notice.relay.model_copy(update={
            "approved_by": member_id, "approved_at": now, "target_group_id": group_id,
            "post_id": post.post_id}),
        "history": [*notice.history, NoticeHistory(at=now, action="relay_approved", by=actor)]})
    repo.upsert_notice(notice)
    write_audit(repo, actor=actor, action="notice.relay.approve", target=notice.notice_id,
                detail=f"→ {group_id}")
    if send_at > now:
        return _reply(f"静音時間のため、{send_at:%-m月%-d日 %H:%M}に「{group_name}」へ送ります。")
    [result] = line_link.send_posts(repo, [post], now)
    if result["result"] == "held":
        return _reply("緊急停止中のため、送信を保留しています。"
                      "停止を解くと次の定期処理で送ります。")
    if result["result"] != "sent":
        return _reply("送信に失敗しました。管理画面の「送信の記録」をご確認ください。")
    return _reply(f"「{group_name}」に転送しました。")


def send_relay(repo: Repository, post: ScheduledPost, now: datetime) -> None:
    """予約投稿（kind=relay）の送信本体。失敗は例外。"""
    notice = repo.get_notice(post.notice_id or "")
    if notice is None or notice.relay is None:
        raise ValueError("連絡が見つかりません。")
    if not line_push.push_messages(post.group_id, relay_messages(repo, notice)):
        raise RuntimeError("LINE に送れませんでした（トークン未設定・緊急停止中など）。")
    notice = notice.model_copy(update={
        "status": "delivered",
        "relay": notice.relay.model_copy(update={"sent_at": now}),
        "history": [*notice.history, NoticeHistory(at=now, action="relay_sent")]})
    repo.upsert_notice(notice)
    write_audit(repo, actor="system", action="notice.relay.sent", target=notice.notice_id,
                detail=f"→ {post.group_id}")
