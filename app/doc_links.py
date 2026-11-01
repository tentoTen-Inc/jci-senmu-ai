"""LINE で送る資料リンクを、LINE の中ではなくスマホのアプリ・ブラウザで開かせる。

LINE のトーク内ブラウザは Google にログインしていないため、Google ドキュメントや
ドライブのリンクを開くたびにログインを求められる。URL に ``openExternalBrowser=1``
を付けると LINE は外部で開く（LINE の仕様）。スマホに Google ドキュメント等のアプリが
あればアプリで、なければ普段のブラウザ（ログイン済み）で開く。

送信の出口（返信・Push）でまとめて書き換えるので、議案書・RAG の出典・リマインドなど
資料リンクを作る側は気にしなくてよい。Google フォーム（アンケート）はログイン不要で
トーク内で答えやすいので対象外。
"""
from __future__ import annotations

import re
from typing import Any

from linebot.v3.messaging import Message

PARAM = "openExternalBrowser=1"
DOC_URL = re.compile(r"https://(?:docs|drive)\.google\.com/[\w\-./?=&%#~+:]*", re.ASCII)


def _external(url: str) -> str:
    if PARAM in url or url.startswith("https://docs.google.com/forms/"):
        return url
    base, sep, fragment = url.partition("#")
    joined = f"{base}{'&' if '?' in base else '?'}{PARAM}"
    return f"{joined}{sep}{fragment}"


def externalize_text(text: str) -> str:
    """文中の Google ドキュメント・ドライブの URL に openExternalBrowser=1 を付ける。"""
    return DOC_URL.sub(lambda m: _external(m.group(0)), text)


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return externalize_text(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value


def externalize(messages: list[Message]) -> list[Message]:
    """メッセージ列（テキスト・Flex のボタン等）の資料リンクを外部で開くものにする。"""
    out = []
    for message in messages:
        data = message.to_dict()
        changed = _walk(data)
        out.append(message if changed == data else type(message).from_dict(changed))
    return out
