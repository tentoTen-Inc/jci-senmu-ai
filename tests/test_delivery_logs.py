"""配信ログ検索APIのテスト。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.deps import set_repo
from app.models import DeliveryLog, DeliveryResult
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)


@pytest.fixture(autouse=True)
def repo():
    r = InMemoryRepository()
    r.save_delivery_log(DeliveryLog(log_id="l1", job_id="j", member_id="m1", content="x",
                                    result=DeliveryResult.ok, sent_at=datetime(2026, 6, 1, 9)))
    r.save_delivery_log(DeliveryLog(log_id="l2", job_id="j", member_id="m2", content="x",
                                    result=DeliveryResult.failed, sent_at=datetime(2026, 6, 2, 9)))
    r.save_delivery_log(DeliveryLog(log_id="l3", job_id="j", member_id="m3", content="x",
                                    result=DeliveryResult.blocked, sent_at=datetime(2026, 6, 3, 9)))
    set_repo(r)
    yield r
    set_repo(None)


def test_all_logs_desc():
    res = client.get("/api/delivery-logs")
    assert res.status_code == 200
    logs = res.json()
    assert len(logs) == 3
    # 新しい順
    assert logs[0]["log_id"] == "l3"


def test_filter_by_result():
    assert len(client.get("/api/delivery-logs?result=failed").json()) == 1
    assert client.get("/api/delivery-logs?result=ok").json()[0]["member_id"] == "m1"


def test_limit():
    assert len(client.get("/api/delivery-logs?limit=1").json()) == 1
