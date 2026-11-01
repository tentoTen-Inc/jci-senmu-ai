"""招待コード発行・本人確認のユニットテスト。"""
from datetime import datetime, timedelta

from app.invite import (
    MAX_FAILED,
    generate_code,
    invite_url,
    issue_invite,
    normalize_code,
    verify_and_link,
)
from app.models import Member
from app.repository import InMemoryRepository

NOW = datetime(2026, 6, 20, 10, 0)


def setup_repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.upsert_member(Member(member_id="m1", name="猪苗代 太郎"))
    return repo


def test_generate_code_charset_and_length():
    code = generate_code(8)
    assert len(code) == 8
    # 紛らわしい文字を含まない
    assert not set(code) & set("01OI")


def test_normalize_code():
    assert normalize_code(" ab-cd 12 ") == "ABCD12"


def test_invite_url():
    url = invite_url("ABC123", "@782uyboi")
    assert "ABC123" in url
    assert "@782uyboi" in url


def test_issue_and_link_success():
    repo = setup_repo()
    invite = issue_invite(repo, "m1", now=NOW)
    res = verify_and_link(repo, "U_line_1", invite.code, now=NOW)
    assert res.ok is True
    assert res.member.member_id == "m1"
    # 紐付けが永続化されている
    assert repo.get_member_by_line_user_id("U_line_1").member_id == "m1"
    # コードは使用済み
    assert repo.get_invite_code(invite.code).used_at is not None


def test_link_lowercase_and_spaces_accepted():
    repo = setup_repo()
    invite = issue_invite(repo, "m1", now=NOW)
    spaced = f" {invite.code.lower()} "
    res = verify_and_link(repo, "U_line_1", spaced, now=NOW)
    assert res.ok is True


def test_used_code_rejected():
    repo = setup_repo()
    invite = issue_invite(repo, "m1", now=NOW)
    verify_and_link(repo, "U_line_1", invite.code, now=NOW)
    res = verify_and_link(repo, "U_line_2", invite.code, now=NOW)
    assert res.ok is False
    assert res.reason == "used"


def test_expired_code_rejected():
    repo = setup_repo()
    invite = issue_invite(repo, "m1", now=NOW, ttl_days=1)
    res = verify_and_link(repo, "U_line_1", invite.code, now=NOW + timedelta(days=2))
    assert res.ok is False
    assert res.reason == "expired"


def test_already_linked_returns_existing():
    repo = setup_repo()
    invite = issue_invite(repo, "m1", now=NOW)
    verify_and_link(repo, "U_line_1", invite.code, now=NOW)
    res = verify_and_link(repo, "U_line_1", "WHATEVER", now=NOW)
    assert res.ok is True
    assert res.reason == "already_linked"


def test_lock_after_max_failed_attempts():
    repo = setup_repo()
    for _ in range(MAX_FAILED):
        res = verify_and_link(repo, "U_line_x", "BADCODE0", now=NOW)
    assert res.ok is False
    assert res.reason == "locked"
    # ロック後は正しいコードでも拒否
    invite = issue_invite(repo, "m1", now=NOW)
    res2 = verify_and_link(repo, "U_line_x", invite.code, now=NOW)
    assert res2.ok is False
    assert res2.reason == "locked"
