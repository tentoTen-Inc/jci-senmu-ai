"""専務の差し戻しの知らせ（app/proposal_return.py）。架空の会員・議案で確かめる。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import line_push, main, proposal_return
from app import proposals_api as api
from app.deps import set_repo
from app.models import (
    CommitteeSeat,
    Member,
    Proposal,
    ProposalHistory,
    ProposalStage,
    QuietHours,
    Settings,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
IAP = {"X-Goog-Authenticated-User-Email": "sed@example.org"}
NO_QUIET = QuietHours(start="00:00", end="00:00")


class _Clock:
    def __init__(self, at):
        self.at = at

    def now(self):
        return self.at


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(line_push, "push_messages", lambda to, msgs: out.append((to, msgs[0]))
                        or True)
    return out


@pytest.fixture
def repo(sent):
    r = InMemoryRepository()
    for m in [
        Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
               committees=[CommitteeSeat(committee="総務委員会", role="委員長")]),
        Member(member_id="m_vp", name="副理事長 次郎", line_user_id="U_vp",
               officer_role="副理事長", supervised_committees=["総務委員会"]),
        Member(member_id="m_pres", name="理事長 一郎", line_user_id="U_pres",
               officer_role="理事長"),
        Member(member_id="m_sed", name="専務 四郎", line_user_id="U_sed", officer_role="専務理事"),
    ]:
        r.upsert_member(m)
    r.save_settings(Settings(quiet_hours=NO_QUIET))
    r.upsert_proposal(Proposal(
        proposal_id="p1", title="総務委員会 事業計画", number="第3号議案", committee="総務委員会",
        stage=ProposalStage.submitted, storage_uri="https://docs.google.com/document/d/x/edit",
        history=[ProposalHistory(at=datetime(2026, 11, 1, 10), stage=ProposalStage.submitted,
                                 by="LINE: 副理事長 次郎")]))
    r.upsert_proposal(Proposal(
        proposal_id="p2", title="2027年度 予算（案）", number="第5号議案", owner_member_id="m_pres",
        stage=ProposalStage.submitted,
        history=[ProposalHistory(at=datetime(2026, 11, 1, 10), stage=ProposalStage.submitted,
                                 by="LINE: 理事長 一郎")]))
    set_repo(r)
    yield r
    set_repo(None)


def test_return_notifies_submitter_chair_and_vice_president(repo, sent):
    res = client.post("/api/proposals/p1/return", json={"comment": "根拠を補足してください"},
                      headers=IAP)
    assert res.status_code == 200, res.text
    # 提出した人（副理事長）・委員長。同じ人には1回だけ。専務には送らない
    assert sorted(to for to, _ in sent) == ["U_chair", "U_vp"]
    alt = sent[0][1].alt_text
    assert "【差し戻し】総務委員会「総務委員会 事業計画」" in alt
    assert "根拠を補足してください" in alt
    notice = res.json()["return_notice"]
    assert notice["state"] == "done" and not notice["failed"]
    assert notice["sent"] == ["副理事長 次郎", "委員長 太郎"]
    # もう一度 tick が回っても二度送らない
    assert proposal_return.send_pending(repo, datetime.now()) == 0 and len(sent) == 2


def test_outside_committee_goes_to_owner(repo, sent):
    client.post("/api/proposals/p2/return", json={}, headers=IAP)
    assert [to for to, _ in sent] == ["U_pres"]
    assert "【差し戻し】「2027年度 予算（案）」" in sent[0][1].alt_text


def test_quiet_hours_defer_to_next_tick(repo, sent, monkeypatch):
    repo.save_settings(Settings(quiet_hours=QuietHours(start="21:00", end="08:00")))
    night, morning = datetime(2026, 11, 7, 22), datetime(2026, 11, 8, 8, 30)
    monkeypatch.setattr(api, "datetime", _Clock(night))
    res = client.post("/api/proposals/p2/return", json={"comment": "数字を確認"}, headers=IAP)
    assert sent == [] and res.json()["return_notice"]["state"] == "pending"
    assert proposal_return.send_pending(repo, night) == 0
    assert proposal_return.send_pending(repo, morning) == 1
    assert [to for to, _ in sent] == ["U_pres"]


def test_unreachable_is_recorded(repo, monkeypatch):
    def fail(to, msgs):
        raise RuntimeError("not a friend")

    monkeypatch.setattr(line_push, "push_messages", fail)
    res = client.post("/api/proposals/p2/return", json={}, headers=IAP)
    assert res.json()["return_notice"] == {"state": "done", "sent": [], "failed": ["理事長 一郎"]}


def test_overdue_status_ignores_return_notices(repo):
    client.post("/api/proposals/p2/return", json={}, headers=IAP)
    from app.proposal_submit import alert_status
    assert alert_status(repo.get_proposal("p2")) == "未連絡"
