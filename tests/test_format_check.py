"""議案 形式チェックのテスト。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.format_check import check_fullwidth, check_required_sections, run_format_check
from app.models import Proposal
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)

COMPLETE = (
    "事業名: 桜まつり\n事業実施に至る背景: ...\n事業の対象者: 地域住民\n"
    "事業目的: まちづくり\nKPI: 来場100名\n実施日時: 2026-07-15\n"
    "実施場所: 会館\n予算: 10万円\n事業内容: ブース運営\n討議のポイント: 安全管理\n"
)


def test_required_sections_detects_missing():
    issues = check_required_sections("事業名だけ", "事業計画書")
    assert any("KPI" in i for i in issues)
    assert any("予算" in i for i in issues)


def test_required_sections_complete():
    assert check_required_sections(COMPLETE, "事業計画書") == []


def test_fullwidth_detection():
    issues = check_fullwidth("予算は１０万円、ＫＰＩは１００名（重要）")
    assert any("全角英数字" in i for i in issues)
    assert any("全角記号" in i for i in issues)


def test_fullwidth_clean():
    assert check_fullwidth("予算は10万円、KPIは100名(重要)") == []


def test_run_format_check_pass():
    p = Proposal(proposal_id="p1", title="A", content=COMPLETE)
    r = run_format_check(p)
    assert r.passed is True
    assert r.issues == []


def test_run_format_check_empty():
    p = Proposal(proposal_id="p1", title="A", content="")
    r = run_format_check(p)
    assert r.passed is False
    assert any("空" in i for i in r.issues)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def test_endpoint_saves_result(repo):
    p = Proposal(proposal_id="p1", title="A", content=COMPLETE)
    repo.upsert_proposal(p)
    res = client.post("/api/proposals/p1/format-check")
    assert res.status_code == 200
    assert res.json()["passed"] is True
    # 保存される
    assert repo.get_proposal("p1").format_check.passed is True


def test_endpoint_404():
    assert client.post("/api/proposals/nope/format-check").status_code == 404
