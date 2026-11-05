"""通知テンプレート（LINE・メールの送信文）の差し込み（docs/meeting-notice-design.md §4）。

書き方は `{{名前}}`（値）、`{{#名前}}…{{/名前}}`（並びなら繰り返し、値があれば1回）、
`{{^名前}}…{{/名前}}`（値がないとき）、`{{?名前}}`（値がなければその行ごと消す）。式やコードは書けない。
保存のときに `validate` で知らない名前・閉じていないブロックを誤りにする。

既定のテンプレートは `app/notice_templates/*.md`（LOM に依らない文面）。
管理画面で直したものは台帳に保存する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import NoticeTemplate
from .repository import Repository

DEFAULTS_DIR = Path(__file__).parent / "notice_templates"
TOKEN = re.compile(r"\{\{\s*([#^/?]?)\s*([a-z_][a-z0-9_.]*)\s*\}\}")
#: LINE の1通の上限（文字）
LINE_LIMIT = 5000

#: 使える名前（§4.2）。段階 B 以降で増やす
FIELDS = {
    "lom.name", "lom.short_name", "lom.bot_name", "fiscal_year",
    "event.title", "event.date", "event.date_wareki", "event.time", "event.place",
    "event.online_url", "agenda.url", "opinion.url",
}
FLAGS = {"late", "online", "agenda", "opinion"}
LISTS = {"proposals": {"no", "title", "url", "presenter", "first", "index"}}


class TemplateError(ValueError):
    """テンプレートの書き方の誤り（画面にそのまま出す）。"""


@dataclass
class _Node:
    kind: str  # text | var | line_opt | section | inverted
    value: str
    children: list[_Node] | None = None


def _parse(body: str) -> list[_Node]:
    root: list[_Node] = []
    stack: list[tuple[str, list[_Node]]] = [("", root)]
    pos = 0
    for m in TOKEN.finditer(body):
        if m.start() > pos:
            stack[-1][1].append(_Node("text", body[pos:m.start()]))
        sigil, name = m.group(1), m.group(2)
        if sigil in ("#", "^"):
            node = _Node("section" if sigil == "#" else "inverted", name, [])
            stack[-1][1].append(node)
            stack.append((name, node.children))
        elif sigil == "/":
            if stack[-1][0] != name:
                opened = stack[-1][0]
                close, start = "{{/" + name + "}}", "{{#" + (opened or name) + "}}"
                if not opened:
                    raise TemplateError(f"「{close}」に対応する「{start}」がありません")
                raise TemplateError(f"「{start}」が閉じていません（{close} の前）")
            stack.pop()
        elif sigil == "?":
            stack[-1][1].append(_Node("line_opt", name))
        else:
            stack[-1][1].append(_Node("var", name))
        pos = m.end()
    if pos < len(body):
        stack[-1][1].append(_Node("text", body[pos:]))
    if len(stack) > 1:
        raise TemplateError("「{{#" + stack[-1][0] + "}}」が閉じていません")
    return root


def validate(body: str, subject: str = "") -> list[str]:
    """知らない名前・閉じていないブロック・使えない場所の名前を、画面に出す文で返す（なければ空）。"""
    errors: list[str] = []
    for text in (subject, body):
        try:
            nodes = _parse(text)
        except TemplateError as exc:
            errors.append(str(exc))
            # 形が崩れていても、知らない名前は一緒に知らせる
            known = FIELDS | FLAGS | set(LISTS) | set().union(*LISTS.values())
            errors += [f"「{m.group(2)}」という項目はありません" for m in TOKEN.finditer(text)
                       if m.group(2) not in known]
            continue
        _check(nodes, None, errors)
    if "{{" in re.sub(TOKEN, "", body + subject):
        errors.append("「{{」で始まる書き方に誤りがあります（名前は半角の英小文字・数字・._）")
    return list(dict.fromkeys(errors))


def _check(nodes: list[_Node], loop: str | None, errors: list[str]) -> None:
    for n in nodes:
        if n.kind == "text":
            continue
        if n.kind in ("section", "inverted"):
            if n.value in LISTS:
                if loop:
                    errors.append(f"「{n.value}」の繰り返しの中で、さらに繰り返すことはできません")
                _check(n.children or [], n.value, errors)
            else:
                if not _known(n.value, loop):
                    errors.append(f"「{n.value}」という項目はありません")
                _check(n.children or [], loop, errors)
        elif not _known(n.value, loop) or n.value in LISTS:
            errors.append(f"「{n.value}」という項目はありません")


def _known(name: str, loop: str | None) -> bool:
    if loop and name in LISTS[loop]:
        return True
    return name in FIELDS or name in FLAGS or name in LISTS


def _lookup(name: str, scopes: list[dict]):
    for scope in reversed(scopes):
        if name in scope:
            return scope[name]
    return None


def _text(value) -> str:
    return "" if value is None or value is False else str(value)


def _render(nodes: list[_Node], scopes: list[dict]) -> str:
    out = []
    for n in nodes:
        if n.kind == "text":
            out.append(n.value)
        elif n.kind == "var":
            out.append(_text(_lookup(n.value, scopes)))
        elif n.kind == "line_opt":
            value = _text(_lookup(n.value, scopes))
            out.append(value if value else "\x00DROP\x00")
        elif n.kind == "section":
            value = _lookup(n.value, scopes)
            if isinstance(value, list):
                for i, item in enumerate(value):
                    out.append(_render(n.children or [],
                                       scopes + [{**item, "first": i == 0, "index": i + 1}]))
            elif value:
                out.append(_render(n.children or [], scopes))
        elif n.kind == "inverted":
            if not _lookup(n.value, scopes):
                out.append(_render(n.children or [], scopes))
    return "".join(out)


def render(body: str, context: dict) -> str:
    """差し込んだ本文。`{{?名前}}` の値がない行は消し、続いた空行は1つにまとめる。"""
    text = _render(_parse(body), [context])
    lines = [line for line in text.split("\n") if "\x00DROP\x00" not in line]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip("\n")


# --------------------------------------------------------------------------- #
# 既定のテンプレートと台帳
# --------------------------------------------------------------------------- #
def _parse_default(path: Path) -> NoticeTemplate:
    """`---` で囲んだ見出し（key: value）＋本文の .md を読む。"""
    raw = path.read_text(encoding="utf-8")
    _, head, body = raw.split("---\n", 2)
    meta: dict = {}
    for line in head.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return NoticeTemplate(
        template_id=path.stem, name=meta["name"], channel=meta.get("channel", "line"),
        purpose=meta.get("purpose", "agenda"),
        event_types=[t.strip() for t in meta.get("event_types", "").split(",") if t.strip()],
        subject=meta.get("subject", ""), mention=meta.get("mention", "none"),
        is_default=meta.get("is_default", "false") == "true", builtin=True,
        body=body.strip("\n"),
    )


def defaults() -> list[NoticeTemplate]:
    return [_parse_default(p) for p in sorted(DEFAULTS_DIR.glob("*.md"))]


def list_templates(repo: Repository, *, include_archived: bool = False) -> list[NoticeTemplate]:
    """台帳のテンプレート＋まだ直していない既定のテンプレート（名前の順）。"""
    saved = {t.template_id: t for t in repo.list_notice_templates()}
    merged = {**{t.template_id: t for t in defaults()}, **saved}
    items = [t for t in merged.values() if include_archived or not t.archived]
    return sorted(items, key=lambda t: (t.channel, t.purpose, t.name))


def get_template(repo: Repository, template_id: str) -> NoticeTemplate | None:
    return next((t for t in list_templates(repo, include_archived=True)
                 if t.template_id == template_id), None)


def default_for(repo: Repository, channel: str, purpose: str,
                event_type: str | None) -> NoticeTemplate | None:
    """その会議で最初に選ぶテンプレート（種類が合う中で is_default を優先）。"""
    fits = [t for t in list_templates(repo) if t.channel == channel and t.purpose == purpose
            and (not t.event_types or (event_type in t.event_types))]
    fits.sort(key=lambda t: (not t.is_default, bool(not t.event_types), t.name))
    return fits[0] if fits else None


#: 資料配信の締切（既定: 開催日の2日前の24:00。docs/meeting-notice-design.md §7.3・§13）
DELIVER_DAYS_BEFORE = 2


def deliver_deadline(start: datetime, delivery_at: datetime | None = None) -> datetime:
    if delivery_at:
        return delivery_at
    return datetime.combine(start.date() - timedelta(days=DELIVER_DAYS_BEFORE - 1),
                            datetime.min.time())


def sample_context() -> dict:
    """会議を選ばずにプレビューするときの例の値（架空）。"""
    from . import lom

    return {
        "lom.name": lom.lom_name(), "lom.short_name": lom.lom_short_name(),
        "lom.bot_name": lom.bot_name(), "fiscal_year": 2027,
        "event.title": "第2回理事会", "event.date": "1月12日(火)",
        "event.date_wareki": "令和9年1月12日(火)", "event.time": "19:00〜21:00",
        "event.place": "会館 or オンライン", "event.online_url": "https://meet.google.com/xxx-xxxx-xxx",
        "agenda.url": "https://docs.google.com/document/d/（アジェンダ）",
        "opinion.url": "https://docs.google.com/spreadsheets/d/（意見書）",
        "online": True, "agenda": True, "opinion": True, "late": False,
        "proposals": [
            {"no": "第1号議案", "title": "年間計画（案）について", "url": "https://example.org/1",
             "presenter": "（担当者）"},
            {"no": "第2号議案", "title": "予算（案）について", "url": "https://example.org/2",
             "presenter": "（担当者）"},
        ],
    }
