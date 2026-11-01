"""LOM（各地の青年会議所）ごとに変わる名前・URL・アカウント。

環境変数で設定する（docs/configuration.md「LOM の情報」）。コードに LOM 名・ドメイン・
メールアドレスを直書きせず、ここから読む。未設定のときは、どの LOM でもおかしくない
一般的な表記にする（例: ボット名「専務AI」）。
"""
from __future__ import annotations

import os

from . import config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def lom_id() -> str:
    """データに付ける LOM の識別子（英数字。例: inawashiro）。"""
    return _env("LOM_ID", "lom")


def lom_name() -> str:
    """LOM の名前（例: 猪苗代青年会議所）。AI への指示や画面の文言に使う。"""
    return _env("LOM_NAME", "青年会議所")


def lom_short_name() -> str:
    """LOM の短い呼び名（例: 猪苗代JC）。未設定なら LOM_NAME。"""
    return _env("LOM_SHORT_NAME", lom_name())


def bot_name() -> str:
    """LINE 公式アカウントの名前（例: 猪苗代専務AI）。"""
    return _env("BOT_NAME", "専務AI")


def friend_url() -> str | None:
    """LINE 公式アカウントを友だち追加するリンク（LINE_BASIC_ID が未設定なら None）。"""
    basic_id = _env("LINE_BASIC_ID")
    return f"https://line.me/R/ti/p/{basic_id}" if basic_id else None


def admin_url(path: str = "") -> str | None:
    """管理画面の URL（ADMIN_BASE_URL。例: https://admin.example.org）。未設定なら None。"""
    base = _env("ADMIN_BASE_URL").rstrip("/")
    return f"{base}{path}" if base else None


def google_account() -> str | None:
    """LOM 公式の Google アカウント（カレンダー・ドライブ・Gmail の持ち主）。"""
    return _env("LOM_GOOGLE_ACCOUNT") or None


def service_account(name: str) -> str:
    """このプロジェクトのサービスアカウントのメールアドレス（例: drive-reader）。"""
    return f"{name}@{config.PROJECT_ID}.iam.gserviceaccount.com"


def profile() -> dict:
    """管理画面・LIFF に渡す LOM の情報（秘密は含めない）。"""
    return {
        "lom_name": lom_name(),
        "lom_short_name": lom_short_name(),
        "bot_name": bot_name(),
        "friend_url": friend_url(),
        "drive_reader_sa": service_account("drive-reader"),
    }
