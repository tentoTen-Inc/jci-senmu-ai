"""管理画面から出す CSV を、表計算ソフトで開いても数式として動かないようにする。

`=`・`+`・`-`・`@`（とタブ・改行）で始まるセルは、Excel やスプレッドシートが数式として
実行することがある（CSV インジェクション）。AI への質問など会員が書いた文が入るので、
先頭に `'` を付けて文字列として扱わせる（OWASP の推奨どおり）。
"""
from __future__ import annotations

from typing import Any

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def row(values: list[Any]) -> list[Any]:
    return [cell(v) for v in values]


def record(values: dict[str, Any]) -> dict[str, Any]:
    return {k: cell(v) for k, v in values.items()}
