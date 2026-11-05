#!/usr/bin/env python3
"""会員名簿 xlsx のメールアドレスを、登録済みの会員の連絡先（contact.email）に入れる CLI。

会議の案内メール（docs/meeting-notice-design.md 段階 B）の宛先に使う。
名前（空白を除いて）で突き合わせ、メールアドレスだけを書き換える
（LINE の連携・役職などは触らない）。
既に入っているアドレスは変えない（--overwrite で名簿の値に置き換える）。

使い方:
  python scripts/sync_member_emails.py path/to/roster.xlsx            # 確認だけ（書き込まない）
  python scripts/sync_member_emails.py path/to/roster.xlsx --apply    # Firestore に書き込む

個人情報保護: 表示は氏名と状態のみ（アドレスは出さない）。名簿のファイルはコミットしないこと。
名簿にいない人（監事・直前理事長など）は、管理画面の会員の「メール」に手で入れる。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, ".")

from app.roster_import import load_roster  # noqa: E402


def _key(name: str) -> str:
    return re.sub(r"\s", "", name)


def plan(registered, roster, *, overwrite: bool = False) -> list[tuple[object, str, str]]:
    """（会員, 名簿のアドレス, 状態）の並び。状態は 追加 / 変更 / 同じ / 既にあり。"""
    emails = {_key(m.name): (m.contact.email or "").strip() for m in roster}
    out = []
    for member in registered:
        email = emails.get(_key(member.name))
        if not email:
            continue
        current = (member.contact.email or "").strip()
        if not current:
            state = "追加"
        elif current == email:
            state = "同じ"
        else:
            state = "変更" if overwrite else "既にあり"
        out.append((member, email, state))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--apply", action="store_true", help="Firestore に書き込む")
    ap.add_argument("--overwrite", action="store_true", help="入っているアドレスも名簿の値にする")
    args = ap.parse_args()

    from app.firestore_repo import FirestoreRepository

    repo = FirestoreRepository()
    registered = [m for m in repo.list_members() if m.is_current]
    roster = load_roster(args.path, sheet=args.sheet, lom_id=os.environ.get("LOM_ID", "lom"))
    rows = plan(registered, roster, overwrite=args.overwrite)
    matched = {m.member_id for m, _, _ in rows}
    for member, _, state in rows:
        print(f"  {member.member_id:>6} | {member.name} | {state}")
    for member in registered:
        if member.member_id not in matched and not member.contact.email:
            print(f"  {member.member_id:>6} | {member.name} | 名簿にアドレスなし"
                  "（画面で入れてください）")

    changes = [(m, e) for m, e, s in rows if s in ("追加", "変更")]
    if not args.apply:
        print(f"\n書き込む予定: {len(changes)} 名（確認だけ。書き込むには --apply）")
        return 0
    for member, email in changes:
        repo.upsert_member(member.model_copy(
            update={"contact": member.contact.model_copy(update={"email": email})}))
    print(f"\n{len(changes)} 名のメールアドレスを入れました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
