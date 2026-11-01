"""Drive フォルダから議案(Proposal)を取り込む（docs/dashboard-design.md §4.1）。

Drive のファイルIDを storage_uri に保存して冪等化（既存なら content を更新）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from .drive import fetch_folder_docs
from .models import Proposal, ProposalHistory, ProposalStage
from .repository import Repository


class ImportItem(BaseModel):
    file_id: str
    name: str
    action: str  # created | updated | skipped
    proposal_id: str | None = None


class ImportSummary(BaseModel):
    folder_id: str
    dry_run: bool
    total: int
    created: int
    updated: int
    items: list[ImportItem]


def _title_from_name(name: str) -> str:
    # 拡張子は基本付かない（Google Docs）が、念のため除去
    return name.rsplit(".", 1)[0] if "." in name else name


def import_drive_folder(
    repo: Repository,
    folder_id: str,
    *,
    now: datetime,
    dry_run: bool = False,
    actor: str = "system",
    fetch=fetch_folder_docs,
) -> ImportSummary:
    docs = fetch(folder_id)
    existing = {p.storage_uri: p for p in repo.list_proposals() if p.storage_uri}

    items: list[ImportItem] = []
    created = updated = 0
    for d in docs:
        fid, name, text = d["id"], d["name"], d.get("text", "")
        prev = existing.get(fid)
        if prev is not None:
            action = "updated"
            updated += 1
            if not dry_run:
                prev.content = text
                prev.title = _title_from_name(name)
                repo.upsert_proposal(prev)
            items.append(
                ImportItem(file_id=fid, name=name, action=action, proposal_id=prev.proposal_id)
            )
        else:
            action = "created"
            created += 1
            pid = f"prop_{uuid.uuid4().hex[:10]}"
            if not dry_run:
                proposal = Proposal(
                    proposal_id=pid,
                    title=_title_from_name(name),
                    content=text,
                    storage_uri=fid,
                    stage=ProposalStage.submitted,
                    history=[ProposalHistory(at=now, stage=ProposalStage.submitted, by=actor)],
                )
                repo.upsert_proposal(proposal)
            items.append(ImportItem(file_id=fid, name=name, action=action, proposal_id=pid))

    return ImportSummary(
        folder_id=folder_id,
        dry_run=dry_run,
        total=len(docs),
        created=created,
        updated=updated,
        items=items,
    )
