#!/usr/bin/env python3
"""日本JCの会員名簿（JSON）を会員情報に取り込む CLI（既定は確認だけ。--apply で書き込み）。

使い方（リポジトリ直下。必要な環境変数（GCP_PROJECT_ID など）を設定した状態で
  例: set -a; source .env; set +a）:
  .venv/bin/python \\
      scripts/import_member_profiles.py data/jc-roster-20261013.json             # 変更点の確認
  .venv/bin/python \\
      scripts/import_member_profiles.py data/jc-roster-20261013.json --apply     # 書き込み

- JSON は {"as_of": "YYYY-MM-DD", "members": [RosterRow, ...]}（app/member_profile_import.py）。
- 会員番号、なければ氏名で既存の会員に当てる。会員は作らない。LINE 連携・委員会・呼び名は触らない。
- 連絡先は空のところだけ入れる。今の値と違うものは項目名だけ出す。名簿の値にするなら
  --overwrite-contact を付ける。
- 画面に出すのは氏名と項目名だけ（電話・住所・メールなどの値は出さない）。
- 実データの JSON はリポジトリにコミットしない（data/ は .gitignore 済み）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit import write_audit  # noqa: E402
from app.member_profile_import import RosterRow, plan  # noqa: E402

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


def _actor() -> str:
    email = subprocess.run(["gcloud", "config", "get-value", "account"],
                           capture_output=True, text=True).stdout.strip()
    return email or "script"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="名簿の JSON")
    ap.add_argument("--apply", action="store_true", help="Firestore に書き込む")
    ap.add_argument("--overwrite-contact", action="store_true",
                    help="今の連絡先と違うときも名簿の値にする")
    args = ap.parse_args()

    data = json.loads(Path(args.path).read_text())
    rows = [RosterRow.model_validate(x) for x in data["members"]]
    repo = _repo()
    result = plan(repo.list_members(), rows, as_of=data.get("as_of"),
                  overwrite_contact=args.overwrite_contact)

    for c in result.changes:
        fields = ", ".join(c.fields) or "変更なし"
        print(f"[当てた] {c.member.member_id:>14} {c.member.name}: {fields}")
        if c.conflicts:
            print(f"         今の値と違う（そのまま）: {', '.join(c.conflicts)}")
    for row in result.unmatched:
        print(f"[会員にいない] {row.name}（会員番号 {row.member_no or '-'}）")
    for m in result.not_in_roster:
        print(f"[名簿にいない在籍会員] {m.member_id} {m.name}")
    changed = [c for c in result.changes if c.fields]
    print(f"合計: 当てた {len(result.changes)}（変わる {len(changed)}） / "
          f"会員にいない {len(result.unmatched)} / 名簿にいない {len(result.not_in_roster)}")

    if not args.apply:
        print("\n確認のみです。書き込むには --apply を付けて実行してください。")
        return 0
    actor = _actor()
    for c in changed:
        repo.upsert_member(c.member)
        write_audit(repo, actor=actor, action="member.profile_import", target=c.member.member_id,
                    detail=",".join(c.fields))
    print(f"\n{len(changed)}名を書き込みました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
