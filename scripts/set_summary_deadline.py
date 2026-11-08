#!/usr/bin/env python3
"""委員会サマリーの議案に、提出締切と上程先（日程調整中の会議）を設定する CLI。

既定は確認だけ（何も書き込まない）。--apply で書き込む（docs/proposal-ledger-design.md §10）。

使い方（リポジトリ直下。必要な環境変数（GCP_PROJECT_ID など）を設定した状態で
  例: set -a; source .env; set +a）:
  .venv/bin/python scripts/set_summary_deadline.py --deadline 2027-01-31T23:59 --poll <日程調整ID>
      # 対象と変更点の確認。書き込むときは --apply を付ける

- 対象: 台帳の open の議案のうち、種類が「年間計画（サマリー）」のもの（proposal_sync が作る）。
- 提出締切と上程先（日程調整 ID。管理画面の日程調整の URL にある）は毎回指定する。
- 認証は gcloud auth login でログインしたアカウントのアクセストークンを使う。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proposal_sync import SUMMARY_TYPE  # noqa: E402

PROJECT = os.environ.get("GCP_PROJECT_ID") or sys.exit("GCP_PROJECT_ID を設定してください")

def _repo():
    from google.cloud import firestore
    from google.oauth2.credentials import Credentials

    from app.firestore_repo import FirestoreRepository

    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return FirestoreRepository(client=firestore.Client(project=PROJECT,
                                                       credentials=Credentials(token)))


def plan(repo, deadline: datetime, poll_id: str, now: datetime | None = None) -> list:
    """設定する議案（変更後の値）の一覧。上程先の日程調整が無ければ止める。"""
    if repo.get_poll(poll_id) is None:
        raise SystemExit(f"日程調整 {poll_id} が見つかりません。")
    now = now or datetime.now()
    targets = [p for p in repo.list_proposals(status="open") if p.doc_type == SUMMARY_TYPE]
    return [p.model_copy(update={
        "deadlines": p.deadlines.model_copy(update={"submit": deadline}),
        "meeting_poll_id": poll_id,
        # 締切を変えたときだけ記録（未提出の知らせは、設定後に過ぎた締切だけが対象）
        "submit_deadline_set_at": (now if p.deadlines.submit != deadline
                                   else p.submit_deadline_set_at),
    }) for p in targets]


def main(argv: list[str] | None = None, repo=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--deadline", required=True, help="提出締切（日本時間。例: 2027-01-31T23:59）")
    ap.add_argument("--poll", required=True, help="上程先の会議の日程調整 ID")
    ap.add_argument("--apply", action="store_true", help="書き込む（無ければ確認だけ）")
    args = ap.parse_args(argv)
    repo = repo or _repo()
    deadline = datetime.fromisoformat(args.deadline)
    poll = repo.get_poll(args.poll)
    updates = plan(repo, deadline, args.poll)
    print(f"上程先: {poll.title}（{args.poll}）  提出締切: {deadline:%Y-%m-%d %H:%M}")
    if not updates:
        print("対象の議案（年間計画（サマリー））がありません。同期（tick）が済んでいるか確かめてください。")
        return 1
    for p in updates:
        before = repo.get_proposal(p.proposal_id)
        print(f"- {p.title}（{p.proposal_id}・{p.stage}）"
              f" 締切 {before.deadlines.submit} → {p.deadlines.submit}"
              f" / 上程先 {before.meeting_poll_id} → {p.meeting_poll_id}")
    if not args.apply:
        print("\n確認だけです。書き込むときは --apply を付けてください。")
        return 0
    for p in updates:
        repo.upsert_proposal(p)
    print(f"\n{len(updates)} 件に設定しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
