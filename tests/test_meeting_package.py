"""事前共有パッケージ生成（F6-7）のテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.meeting_package import build_package
from app.models import (
    Event,
    EventStatus,
    EventType,
    FormatCheckResult,
    LlmReview,
    Member,
    Proposal,
    ProposalDeadlines,
    ProposalSource,
    ProposalStage,
    SchedulePoll,
    SedApproval,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 7, 26, 9, 0)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="江口太郎"))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.理事会, title="8月理事会",
        datetime_start=datetime(2026, 8, 10, 19, 0), location="会館",
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    r.upsert_proposal(Proposal(
        proposal_id="p2", number="第2号議案", title="夏まつり事業計画",
        committee="コト創り委員会", owner_member_id="m1", event_id="ev1",
        stage=ProposalStage.board, storage_uri="https://drive.google.com/file/xyz",
        deadlines=ProposalDeadlines(submit=datetime(2026, 8, 5, 23, 59)),
        format_check=FormatCheckResult(passed=False, issues=["予算の記載がありません"]),
        llm_review=LlmReview(
            summary="地域交流を目的とした夏まつり。",
            points=["予算の妥当性"], concerns=["雨天時の対応が未記載"],
        ),
        sed_approval=SedApproval(status="approved", by="sed@example.org", comment="承認します"),
    ))
    r.upsert_proposal(Proposal(
        proposal_id="p1", number="第1号議案", title="規約改正の件",
        committee="総務委員会", event_id="ev1", stage=ProposalStage.goyaku,
        format_check=FormatCheckResult(passed=True),
    ))
    # 別会議の議案は含めない
    r.upsert_proposal(Proposal(proposal_id="p9", title="別会議の議案", event_id="ev9"))
    # 上程先未設定の議案も含めない
    r.upsert_proposal(Proposal(proposal_id="p8", title="上程先未定の議案"))
    set_repo(r)
    yield r
    set_repo(None)


def test_package_has_toc_in_number_order(repo):
    text = build_package(repo, repo.get_event("ev1"), now=NOW)
    assert "# 8月理事会 事前共有資料" in text
    assert "2026-08-10 19:00 / 会館" in text
    assert "議案数: 2件" in text
    toc = text.split("## 目次")[1].split("##")[0]
    # 議案番号順（第1号 → 第2号）
    assert toc.index("第1号議案") < toc.index("第2号議案")
    assert "総務委員会" in toc and "コト創り委員会" in toc


def test_package_includes_details_and_marks_ai_output(repo):
    text = build_package(repo, repo.get_event("ev1"), now=NOW)
    assert "## 議案2. 第2号議案 夏まつり事業計画" in text
    assert "担当: 江口太郎" in text  # 会員名に解決する
    assert "専務確認: 承認" in text
    assert "提出締切: 2026-08-05 23:59" in text
    assert "原本: https://drive.google.com/file/xyz" in text
    assert "形式チェック: 要修正（1件）" in text
    assert "予算の記載がありません" in text
    # AI生成物は「参考」と明示（責任分界・F6-8）
    assert "要約（AI・参考）: 地域交流を目的とした夏まつり。" in text
    assert "雨天時の対応が未記載" in text
    assert "専務コメント: 承認します" in text
    assert "最終判断は人が行います" in text
    assert "形式チェック: OK" in text  # p1 側


def test_package_excludes_other_meetings(repo):
    text = build_package(repo, repo.get_event("ev1"), now=NOW)
    assert "別会議の議案" not in text
    assert "上程先未定の議案" not in text


def test_package_includes_proposals_via_decided_poll(repo):
    """上程先が日程調整で決まった会議の議案も入れる（日程調整中・閉じた議案は入れない）。"""
    repo.save_poll(SchedulePoll(poll_id="poll1", title="8月理事会", event_type=EventType.理事会,
                                group_id="G", event_id="ev1"))
    repo.save_poll(SchedulePoll(poll_id="poll2", title="未定の会議", event_type=EventType.理事会,
                                group_id="G"))
    repo.upsert_proposal(Proposal(
        proposal_id="p3", title="2027年度 予算（案）", committee="執行部",
        meeting_poll_id="poll1", stage=ProposalStage.goyaku,
        storage_uri="https://drive.google.com/drive/folders/F1",
        sources=[ProposalSource(name="予算書_様式A", url="https://docs.google.com/spreadsheets/d/s1/edit")],
    ))
    repo.upsert_proposal(Proposal(proposal_id="p4", title="日程調整中の議案",
                                  meeting_poll_id="poll2"))
    repo.upsert_proposal(Proposal(proposal_id="p5", title="閉じた議案",
                                  meeting_poll_id="poll1", status="closed"))
    text = build_package(repo, repo.get_event("ev1"), now=NOW)
    assert "議案数: 3件" in text
    assert "2027年度 予算（案）（執行部）" in text
    assert "ステージ: 五役会" in text
    assert "    - 予算書_様式A: https://docs.google.com/spreadsheets/d/s1/edit" in text
    assert "日程調整中の議案" not in text and "閉じた議案" not in text


def test_package_without_proposals(repo):
    repo.upsert_event(Event(
        event_id="ev2", type=EventType.例会, title="9月例会",
        datetime_start=datetime(2026, 9, 20, 19, 0),
        target_scope=TargetScope(kind=TargetScopeKind.all), status=EventStatus.open,
    ))
    text = build_package(repo, repo.get_event("ev2"), now=NOW)
    assert "議案は登録されていません" in text
    assert "## 目次" not in text


def test_package_endpoint_returns_markdown():
    res = client.get("/api/events/ev1/package")
    assert res.status_code == 200
    assert "text/markdown" in res.headers["content-type"]
    assert "事前共有資料" in res.text
    assert "content-disposition" not in {k.lower() for k in res.headers}


def test_package_endpoint_download_headers():
    res = client.get("/api/events/ev1/package?download=true")
    assert 'filename="package_ev1.md"' in res.headers["content-disposition"]


def test_package_endpoint_404():
    assert client.get("/api/events/nope/package").status_code == 404
