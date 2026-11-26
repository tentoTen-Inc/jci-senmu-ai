"""外部グループからの周知の転送（docs/line-relay-design.md）。

R-1: 取り込み。
- ワーカー（`line_worker.handle_envelope`）が、取込元に指定したグループの投稿を
  `RelayPost` として溜める
- `/tasks/relay`（5分おき）が、同じ送り主・同じグループで10分以内に続いた投稿を1件にまとめ、
  最後の投稿から10分たったものを `ExternalNotice(source="line_group")` として保存する

取込元はグループごとに管理画面で指定する（`LineGroup.relay_source`）。ボットがいるだけの
グループは取り込まない。グループ名・ID はコードに持たない（全国の LOM で使えるように）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from .jpdate import JST
from .models import ExternalNotice, NoticeAttachment, NoticeHistory, NoticeRelay, RelayPost
from .repository import Repository

logger = logging.getLogger("jci-agent.relay")

MERGE_GAP = timedelta(minutes=10)  # これより間が空いたら別の連絡
#: 取込元の指定を読み直す間隔（Firestore の読み取りを減らす）
SOURCE_CACHE_TTL = timedelta(minutes=5)
SUBJECT_LIMIT = 40

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
