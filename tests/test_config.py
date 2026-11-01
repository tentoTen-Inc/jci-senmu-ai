"""シークレットの読み取り（app/config.py）のテスト。"""
from types import SimpleNamespace

import pytest
from google.cloud import secretmanager

from app import config

# conftest が _from_secret_manager を差し替える前の本物（モジュール読み込み時に取っておく）
_read = config._from_secret_manager


class _FakeClient:
    calls: list[str] = []
    fail = True

    def access_secret_version(self, *, name, timeout):
        _FakeClient.calls.append(name)
        assert timeout == config.SECRET_TIMEOUT_SECONDS
        if _FakeClient.fail:
            raise RuntimeError("unavailable")
        return SimpleNamespace(payload=SimpleNamespace(data=b"secret-value"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", _FakeClient)
    monkeypatch.setattr(config, "_secret_cache", {})
    monkeypatch.setattr(config, "_secret_failed_at", {})
    _FakeClient.calls, _FakeClient.fail = [], True
    clock = [1000.0]
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    return clock


def test_failure_is_retried_after_a_while(client):
    """一度取れなくても、ずっと None のままにしない（5 分たてば取り直す）。"""
    assert _read("line-channel-access-token") is None
    assert _read("line-channel-access-token") is None  # すぐには取り直さない
    assert len(_FakeClient.calls) == 1
    client[0] += config.SECRET_RETRY_SECONDS + 1
    _FakeClient.fail = False
    assert _read("line-channel-access-token") == "secret-value"
    assert len(_FakeClient.calls) == 2


def test_success_is_cached(client):
    _FakeClient.fail = False
    assert _read("admin-api-secret") == "secret-value"
    assert _read("admin-api-secret") == "secret-value"
    assert len(_FakeClient.calls) == 1
