"""Drive 取込のテスト（fetch境界をモック）。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, proposals_api
from app.deps import set_repo
from app.drive_import import import_drive_folder
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 6, 20, 10, 0)

DOCS = [
    {"id": "f1", "name": "7月例会事業計画(案)", "text": "事業名: 桜まつり\n予算: 10万円"},
    {"id": "f2", "name": "8月例会事業計画(案)", "text": "事業名: 夏祭り"},
]


def fake_fetch(folder_id):
    return DOCS


def test_import_creates_proposals():
    repo = InMemoryRepository()
    s = import_drive_folder(repo, "FOLDER", now=NOW, fetch=fake_fetch)
    assert s.created == 2 and s.updated == 0 and s.total == 2
    props = repo.list_proposals()
    assert len(props) == 2
    titles = {p.title for p in props}
    assert "7月例会事業計画(案)" in titles
    # storage_uri にファイルIDが入る
    assert {p.storage_uri for p in props} == {"f1", "f2"}


def test_import_dedup_updates():
    repo = InMemoryRepository()
    import_drive_folder(repo, "FOLDER", now=NOW, fetch=fake_fetch)
    # 同じファイルIDで本文が変わったら更新
    updated_docs = [
        {"id": "f1", "name": "7月例会事業計画(改)", "text": "事業名: 桜まつり\n予算: 12万円"}
    ]
    s = import_drive_folder(repo, "FOLDER", now=NOW, fetch=lambda fid: updated_docs)
    assert s.created == 0 and s.updated == 1
    p = next(p for p in repo.list_proposals() if p.storage_uri == "f1")
    assert "12万円" in p.content
    assert p.title == "7月例会事業計画(改)"
    # 重複作成されていない（f1, f2 のまま）
    assert len(repo.list_proposals()) == 2


def test_dry_run_does_not_write():
    repo = InMemoryRepository()
    s = import_drive_folder(repo, "FOLDER", now=NOW, dry_run=True, fetch=fake_fetch)
    assert s.dry_run is True and s.created == 2
    assert repo.list_proposals() == []  # 書き込まない


@pytest.fixture
def _wire(monkeypatch):
    repo = InMemoryRepository()
    set_repo(repo)

    def patched(repo_, folder_id, **kw):
        kw.setdefault("fetch", fake_fetch)
        return import_drive_folder(repo_, folder_id, **kw)

    monkeypatch.setattr(proposals_api, "import_drive_folder", patched)
    yield repo
    set_repo(None)


def test_endpoint(_wire):
    res = client.post("/api/proposals/import-drive", json={"folder_id": "FOLDER", "dry_run": True})
    assert res.status_code == 200
    assert res.json()["created"] == 2
    assert res.json()["dry_run"] is True
