"""日本JCの会員名簿の取込（app/member_profile_import.py）。架空の会員で確かめる。"""
import pytest

from app.member_profile_import import RosterRow, plan
from app.models import Contact, Member, MemberProfile, MemberStatus


def _row(**kw) -> RosterRow:
    base = {
        "member_no": "9999-00001", "name": "青木　一郎", "kana": "アオキ イチロウ",
        "joined_on": "2020-01-01", "birthday": "1990-04-01", "gender": "男",
        "home": {"postal_code": "〒１００－０００１", "address": "架空県架空市本町１－２",
                 "tel": "０１２－３４５－６７８９", "fax": None},
        "work": {"name": "(株)架空商事", "title": "代表取締役", "company_form": "1:株式会社",
                 "industry_code": "0401", "industry": "木造建築業",
                 "postal_code": "100-0002", "address": "架空県架空市駅前３", "tel": "012-000-0000",
                 "fax": "012-000-0001"},
        "mobile": "090ー0000ー0000", "email": "aoki@example.com",
        "education": "大学卒業", "school": "架空大学", "blood_type": "A型", "mail_to": "自宅",
    }
    return RosterRow.model_validate(base | kw)


def test_fills_profile_and_empty_contact_without_touching_line_link():
    member = Member(member_id="m1", name="青木 一郎", line_user_id="U1", nicknames=["イッチー"],
                    contact=Contact(email="aoki@example.com"))
    result = plan([member], [_row()], as_of="2026-10-13")
    change = result.changes[0]
    m = change.member
    assert m.member_id == "m1" and m.line_user_id == "U1" and m.nicknames == ["イッチー"]
    assert m.profile == MemberProfile(
        member_no="9999-00001", joined_on="2020-01-01", gender="男", work_postal_code="100-0002",
        work_address="架空県架空市駅前3", company_form="1:株式会社", industry_code="0401",
        industry="木造建築業", education="大学卒業", school="架空大学", blood_type="A型",
        mail_to="自宅", as_of="2026-10-13")
    # 全角の数字・ダッシュ・〒、数字の間の長音をそろえる
    assert m.contact.postal_code == "100-0001" and m.contact.home_tel == "012-345-6789"
    assert m.contact.mobile == "090-0000-0000" and m.contact.fax == "012-000-0001"
    assert m.birthday == "1990-04-01" and m.kana == "アオキ イチロウ"
    assert "contact.email" not in change.fields and not change.conflicts


def test_keeps_edited_contact_unless_overwrite():
    member = Member(member_id="m1", name="青木 一郎", kana="あおき いちろう",
                    contact=Contact(mobile="080-1111-1111"))
    change = plan([member], [_row()]).changes[0]
    assert change.member.contact.mobile == "080-1111-1111" and change.conflicts == ["mobile"]
    assert change.member.kana == "あおき いちろう"
    change = plan([member], [_row()], overwrite_contact=True).changes[0]
    assert change.member.contact.mobile == "090-0000-0000" and not change.conflicts


def test_matches_by_member_no_then_name_and_reports_the_rest():
    renamed = Member(member_id="m1", name="青木 壱郎",
                     profile=MemberProfile(member_no="9999-00001"))
    absent = Member(member_id="m2", name="江口 隆之")
    left = Member(member_id="m3", name="杉山 花子", status=MemberStatus.inactive)
    result = plan([renamed, absent, left],
                  [_row(), _row(member_no="9999-00002", name="田村 修")])
    assert result.changes[0].member.member_id == "m1"
    assert [r.name for r in result.unmatched] == ["田村 修"]
    assert [m.member_id for m in result.not_in_roster] == ["m2"]  # 退会した人は出さない


def test_unchanged_when_imported_twice():
    first = plan([Member(member_id="m1", name="青木 一郎")], [_row()], as_of="2026-10-13")
    again = plan([first.changes[0].member], [_row()], as_of="2026-10-13")
    assert again.changes[0].fields == [] and not again.changes[0].conflicts


def test_same_name_twice_is_refused():
    members = [Member(member_id="m1", name="青木 一郎"), Member(member_id="m2", name="青木一郎")]
    with pytest.raises(ValueError, match="同じ氏名"):
        plan(members, [_row(member_no=None)])
