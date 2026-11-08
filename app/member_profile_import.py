"""日本JCの会員名簿（在籍会員一覧表）の取込。

名簿を JSON にしたもの（`data/jc-roster-YYYYMMDD.json` 等・リポジトリには入れない）を読み、
既存の会員と**会員番号、なければ氏名で突き合わせて**会員情報に入れる。会員は作らない（突き合わせ
できなかった名簿の人は一覧に出すだけ）。member_id・LINE 連携・委員会・呼び名には触らない。

- 名簿にしかない項目（`Member.profile`）と生年月日は、名簿の値で入れる（名簿が正本）。
- ふりがなと連絡先（`Contact`）は、空のところだけ入れる。今の値と違うものは「違い」として出し、
  `overwrite_contact=True` のときだけ名簿の値にする（画面で直した値を消さないため）。
- 実際の書き込みは呼び出し側（scripts/import_member_profiles.py）。ここは差分の計算だけ。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .models import Contact, Member, MemberProfile, MemberType

# NFKC で正規化されないダッシュ類（電話・郵便番号で混ざりやすい）を ASCII のハイフンへ
_DASH_TABLE = {ord(c): "-" for c in "‐‑‒–—―−ー"}
# 長音（ー）は電話番号などの数字の間だけハイフンにする（名前・住所の長音は残す）
_DIGITS = set("0123456789")


def _norm(value: str | None) -> str | None:
    """全角→半角（NFKC）・前後の空白を除く。空は None。"""
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).strip()
    return s or None


def _norm_number(value: str | None) -> str | None:
    """電話・FAX・郵便番号。ダッシュ類と、数字の間の長音をハイフンにそろえる。"""
    s = _norm(value)
    if s is None:
        return None
    chars = list(s)
    for i, c in enumerate(chars):
        if ord(c) in _DASH_TABLE and (c != "ー" or (
                0 < i < len(chars) - 1 and chars[i - 1] in _DIGITS and chars[i + 1] in _DIGITS)):
            chars[i] = "-"
    return "".join(chars).removeprefix("〒").strip() or None


def name_key(name: str) -> str:
    """氏名の突き合わせ用（空白を除く）。"""
    return unicodedata.normalize("NFKC", name).replace(" ", "").replace("　", "")


class Place(BaseModel):
    postal_code: str | None = None
    address: str | None = None
    tel: str | None = None
    fax: str | None = None


class Work(Place):
    name: str | None = None  # 勤務先
    title: str | None = None  # 役職
    company_form: str | None = None
    industry_code: str | None = None
    industry: str | None = None


class RosterRow(BaseModel):
    """名簿の1人分（JSON の1件）。"""

    member_no: str | None = None
    name: str
    kana: str | None = None
    joined_on: str | None = None
    birthday: str | None = None
    gender: str | None = None
    home: Place = Field(default_factory=Place)
    work: Work = Field(default_factory=Work)
    mobile: str | None = None
    email: str | None = None
    education: str | None = None
    school: str | None = None
    industry_section: str | None = None
    blood_type: str | None = None
    mail_to: str | None = None


def to_contact(row: RosterRow) -> Contact:
    return Contact(
        mobile=_norm_number(row.mobile), email=_norm(row.email),
        postal_code=_norm_number(row.home.postal_code), home_address=_norm(row.home.address),
        home_tel=_norm_number(row.home.tel),
        work=_norm(row.work.name), work_title=_norm(row.work.title),
        work_tel=_norm_number(row.work.tel), fax=_norm_number(row.work.fax))


def to_profile(row: RosterRow, as_of: str | None) -> MemberProfile:
    return MemberProfile(
        member_no=_norm(row.member_no), joined_on=_norm(row.joined_on), gender=_norm(row.gender),
        home_fax=_norm_number(row.home.fax),
        work_postal_code=_norm_number(row.work.postal_code),
        work_address=_norm(row.work.address),
        company_form=_norm(row.work.company_form), industry_code=_norm(row.work.industry_code),
        industry=_norm(row.work.industry), industry_section=_norm(row.industry_section),
        education=_norm(row.education), school=_norm(row.school),
        blood_type=_norm(row.blood_type), mail_to=_norm(row.mail_to), as_of=as_of)


@dataclass
class Change:
    member: Member  # 取込後の会員
    fields: list[str] = field(default_factory=list)  # 変わる項目名
    #: 今の値と名簿の値が違う連絡先の項目（overwrite_contact でなければ今の値のまま）
    conflicts: list[str] = field(default_factory=list)


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    unmatched: list[RosterRow] = field(default_factory=list)  # 会員に当てられなかった名簿の人
    #: 名簿にいない在籍の正会員（賛助会員・外部監事・事務局は日本JCの名簿に載らないので出さない）
    not_in_roster: list[Member] = field(default_factory=list)


def _match(row: RosterRow, members: list[Member]) -> Member | None:
    no = _norm(row.member_no)
    if no:
        hit = next((m for m in members if m.profile.member_no == no), None)
        if hit:
            return hit
    hits = [m for m in members if name_key(m.name) == name_key(row.name)]
    if len(hits) > 1:
        raise ValueError(f"同じ氏名の会員が {len(hits)} 人います。会員番号を先に入れてください: "
                         f"{', '.join(m.member_id for m in hits)}")
    return hits[0] if hits else None


def plan(members: list[Member], rows: list[RosterRow], *, as_of: str | None = None,
         overwrite_contact: bool = False) -> Plan:
    """名簿を会員に当てたときの変更を計算する（書き込みはしない）。"""
    result = Plan()
    seen: set[str] = set()
    for row in rows:
        member = _match(row, members)
        if member is None:
            result.unmatched.append(row)
            continue
        if member.member_id in seen:
            raise ValueError(f"名簿の2人が同じ会員に当たりました: {row.name}")
        seen.add(member.member_id)
        change = Change(member=member)
        update: dict = {}
        profile = to_profile(row, as_of)
        if profile != member.profile:
            update["profile"] = profile
            change.fields += [k for k, v in profile.model_dump().items()
                              if v != getattr(member.profile, k)]
        birthday = _norm(row.birthday)
        if birthday and birthday != member.birthday:
            update["birthday"] = birthday
            change.fields.append("birthday")
        kana = _norm(row.kana)
        if kana and not member.kana:
            update["kana"] = kana
            change.fields.append("kana")
        contact = member.contact.model_copy()
        for key, value in to_contact(row).model_dump().items():
            now = getattr(contact, key)
            if value is None or value == now:
                continue
            if now and not overwrite_contact:
                change.conflicts.append(key)
                continue
            setattr(contact, key, value)
            change.fields.append(f"contact.{key}")
        if contact != member.contact:
            update["contact"] = contact
        if update:
            change.member = member.model_copy(update=update)
        result.changes.append(change)
    result.not_in_roster = [m for m in members if m.is_current and m.member_id not in seen
                            and m.member_type == MemberType.regular]
    return result
