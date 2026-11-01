"""LINE Push の出口（app/line_push.py）のテスト。"""
import pytest
from linebot.v3.messaging import TextMessage

from app import config, lake, line_push
from app.deps import set_repo
from app.models import Settings
from app.repository import InMemoryRepository


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class _FakeApi:
        def __init__(self, _client):
            pass

        def push_message(self, req):
            calls.append(req)

    monkeypatch.setattr(config, "line_channel_access_token", lambda: "real-token")
    monkeypatch.setattr(line_push, "MessagingApi", _FakeApi)
    monkeypatch.setattr(lake, "record_outbound", lambda *a, **k: None)
    return calls


@pytest.fixture
def repo():
    r = InMemoryRepository()
    set_repo(r)
    yield r
    set_repo(None)


def test_push_sends_normally(repo, sent):
    assert line_push.push_text("U1", "お知らせです") is True
    assert len(sent) == 1 and sent[0].to == "U1"


def test_push_stops_while_kill_switch(repo, sent):
    """緊急停止中は、催促以外の Push（案内・リマインド等）も送らない。"""
    repo.save_settings(Settings(kill_switch=True))
    assert line_push.push_messages("C1", [TextMessage(text="案内です")]) is False
    assert sent == []
