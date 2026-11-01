"""Drive 参照（鍵レス impersonation, docs/dashboard-design.md §10）。

Cloud Run の実行SA(app-runtime)が drive-reader を権限借用して Drive API を読む。
drive-reader には対象フォルダが共有済み。fetch_folder_docs がテストのモック境界。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

from . import lom

logger = logging.getLogger("jci-agent.drive")

DRIVE_READER_SA = os.environ.get("DRIVE_READER_SA") or lom.service_account("drive-reader")
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DOC = "application/vnd.google-apps.document"


def _token() -> str:
    """drive-reader を impersonate したアクセストークンを取得する。"""
    from google.auth import default, impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = default()
    target = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=DRIVE_READER_SA,
        target_scopes=[SCOPE],
    )
    target.refresh(Request())
    return target.token


def _get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def list_docs(folder_id: str, token: str) -> list[dict]:
    """フォルダ直下の Google ドキュメント一覧。"""
    import json

    q = f"'{folder_id}' in parents and mimeType='{GOOGLE_DOC}' and trashed=false"
    params = {
        "q": q,
        "pageSize": 1000,
        "fields": "files(id,name,modifiedTime)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
    return json.loads(_get(url, token)).get("files", [])


def export_text(file_id: str, token: str) -> str:
    """Google ドキュメントをプレーンテキストにエクスポート。"""
    params = {"mimeType": "text/plain"}
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}/export?"
        + urllib.parse.urlencode(params)
    )
    return _get(url, token).decode("utf-8", "replace")


def fetch_folder_docs(folder_id: str) -> list[dict]:
    """フォルダ内 Google ドキュメントを [{id,name,text}] で返す（モック境界）。"""
    token = _token()
    out = []
    for f in list_docs(folder_id, token):
        out.append({"id": f["id"], "name": f["name"], "text": export_text(f["id"], token)})
    return out
