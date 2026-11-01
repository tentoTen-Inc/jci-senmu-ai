"""共有依存（リポジトリの取得）。

本番は Firestore。テストでは set_repo() で InMemoryRepository を注入する。
循環 import を避けるため main/admin_api から本モジュールを参照する。
"""
from __future__ import annotations

from . import config
from .repository import Repository

_repo: Repository | None = None


def set_repo(repo: Repository | None) -> None:
    global _repo
    _repo = repo


def get_repo() -> Repository:
    global _repo
    if _repo is None:
        from .firestore_repo import FirestoreRepository

        _repo = FirestoreRepository(project=config.PROJECT_ID)
    return _repo
