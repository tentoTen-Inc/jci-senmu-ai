#!/usr/bin/env python3
"""会員名簿 xlsx をパースして Firestore に投入する CLI（プレビュー→upsert）。

使い方:
  python scripts/import_roster.py path/to/roster.xlsx            # プレビュー(投入しない)
  python scripts/import_roster.py path/to/roster.xlsx --sheet 2026会員名簿
  python scripts/import_roster.py path/to/roster.xlsx --upsert   # Firestore へ投入

個人情報保護: プレビュー表示は氏名と件数のみ。電話/住所等はマスクする。
実データファイルはリポジトリにコミットしないこと。
"""
import argparse
import os
import sys

sys.path.insert(0, ".")

from app.roster_import import load_roster  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--lom-id", default=os.environ.get("LOM_ID", "lom"))
    ap.add_argument("--upsert", action="store_true", help="Firestore へ投入")
    args = ap.parse_args()

    members = load_roster(args.path, sheet=args.sheet, lom_id=args.lom_id)
    print(f"パース結果: {len(members)} 名")
    for m in members:
        mob = "✓" if m.contact.mobile else "-"
        mail = "✓" if m.contact.email else "-"
        print(f"  {m.member_id:>5} | {m.name} | 携帯{mob} Mail{mail}")

    if args.upsert:
        from app.firestore_repo import FirestoreRepository

        repo = FirestoreRepository()
        for m in members:
            repo.upsert_member(m)
        print(f"Firestore へ {len(members)} 名を upsert しました。")
    else:
        print("（プレビューのみ。投入するには --upsert を付けてください）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
