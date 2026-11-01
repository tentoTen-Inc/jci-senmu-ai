"""資料フォルダに紐づく議案（予算書など）の読み取りとチェック（docs/folder-proposal-check-design.md）。"""
import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import drive, folder_docs, llm, main, proposal_format
from app.deps import set_repo
from app.folder_docs import Sheet, SourceFile
from app.models import Proposal
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 11, 1, 10, 0)
FOLDER = "https://drive.google.com/drive/folders/FOLDER1"

# 予算書の様式を模したシート（値と数式）
BUDGET_VALUES = [
    ["予算書（案）"],
    ["2027年1月1日から2027年12月31日まで"],
    ["科目", "", "当年度予算額", "前年度予算額"],
    ["", "正会員年会費", "1,040,000", "880,000"],
    ["", "賛助会員会費", "２２０，０００", "300,000"],
    ["事業活動収入計", "", "1,260,000", "1,180,000"],
    ["", "一般会計からの繰入金", "0", "0"],
    ["一般会計収支決算書（案）"],
    ["2023年1月1日から2023年12月31日まで"],
    ["", "参照", "#REF!"],
]
BUDGET_FORMULAS = [
    ["予算書（案）"],
    ['="2027年1月1日から2027年12月31日まで"'],
    ["科目", "", "当年度予算額", "前年度予算額"],
    ["", "正会員年会費", 1040000, 880000],
    ["", "賛助会員会費", "２２０，０００", 300000],
    ["事業活動収入計", "", "=SUM(C4:C5)", 1180000],  # 前年度の合計は手入力
    ["", "一般会計からの繰入金", 0, 0],
    ["一般会計収支決算書（案）"],
    ["2023年1月1日から2023年12月31日まで"],
    ["", "参照", "=Sheet9!A1"],
]


def _budget():
    return SourceFile("s1", "予算書_様式A", "sheet", "https://docs.google.com/spreadsheets/d/s1/edit",
                      sheets=[Sheet("予算書（案）", BUDGET_VALUES, BUDGET_FORMULAS)])


def test_parse_location():
    assert folder_docs.parse_location(FOLDER) == ("folder", "FOLDER1")
    assert folder_docs.parse_location(
        "https://drive.google.com/drive/u/4/folders/F2?usp=x") == ("folder", "F2")
    assert folder_docs.parse_location(
        "https://docs.google.com/spreadsheets/d/S1/edit") == ("file", "S1")
    assert folder_docs.parse_location(None) is None
    assert folder_docs.parse_location("https://example.com/a") is None


def test_check_finds_errors_years_totals_and_text_numbers():
    issues = folder_docs.check([_budget()], 2027)
    by_kind = {i.split(":")[0]: i for i in issues}
    assert "C10" in by_kind["数式のエラー（#REF! など）があります"]
    year = next(i for i in issues if i.startswith("古い年の表記"))
    assert "A9" in year and "A2" not in year and "2027年度" in year
    total = next(i for i in issues if i.startswith("合計の行に"))
    assert total.endswith("D6")  # 当年度の合計は数式なので挙げない
    text = next(i for i in issues if i.startswith("数字が文字として"))
    assert text.endswith("C5")
    # 「一般会計からの…」は「会計」なので合計の行ではない。0 の手入力も挙げない
    assert all("D7" not in i and "C7" not in i for i in issues)
    assert all("予算書_様式A／予算書（案）" in i for i in issues)


def test_previous_year_is_allowed_and_many_cells_are_summarized():
    sheet = Sheet("s", [["2026年度予算期首"]] + [["#REF!"]] * 8,
                  [["2026年度予算期首"]] + [["=X"]] * 8)
    issues = folder_docs.check([SourceFile("f", "F", "sheet", "u", sheets=[sheet])], 2027)
    assert len(issues) == 1 and "ほか3か所" in issues[0]


def test_fiscal_year():
    assert folder_docs.fiscal_year("2027年度 予算（案）", 2026) == 2027
    assert folder_docs.fiscal_year("予算（案）", 2026) == 2026


def test_to_content_and_limit():
    doc = SourceFile("d1", "所信", "doc", "u", text="本文です\n")
    content = folder_docs.to_content([doc, _budget()])
    assert content.startswith("【所信】\n本文です")
    assert "【予算書_様式A／予算書（案）】" in content
    assert " | 正会員年会費 | 1,040,000 | 880,000" not in content  # 空のセルは詰める
    assert "正会員年会費 | 1,040,000 | 880,000" in content
    cut = folder_docs.to_content([doc, _budget()], limit=20)
    assert cut.endswith("省略しました）")


# --------------------------------------------------------------------------- #
# Drive・Sheets からの読み取り
# --------------------------------------------------------------------------- #
@pytest.fixture
def api(monkeypatch):
    """Drive と Sheets の代わり。呼ばれた URL を記録する。"""
    calls = []
    files = [
        {"id": "s1", "name": "予算書_様式A", "mimeType": folder_docs.GOOGLE_SHEET},
        {"id": "d1", "name": "説明", "mimeType": folder_docs.GOOGLE_DOC},
    ]

    def get(url, token):
        calls.append(url)
        u = urlparse(url)
        q = parse_qs(u.query)
        if u.path.endswith("/drive/v3/files"):
            assert "'FOLDER1' in parents" in q["q"][0]
            return {"files": files}
        if u.path.endswith("/s1"):
            return {"sheets": [{"properties": {"title": "予算書（案）"}}]}
        if u.path.endswith("values:batchGet"):
            grid = BUDGET_VALUES if q["valueRenderOption"] == ["FORMATTED_VALUE"] \
                else BUDGET_FORMULAS
            return {"valueRanges": [{"values": grid}]}
        raise AssertionError(url)
    monkeypatch.setattr(folder_docs, "_get_json", get)
    monkeypatch.setattr(drive, "_token", lambda: "tok")
    monkeypatch.setattr(drive, "export_text", lambda file_id, token: "説明の本文")
    return calls


def test_read_folder(api):
    read = folder_docs.read(FOLDER)
    assert read.error is None
    assert [(f.name, f.kind) for f in read.files] == [("予算書_様式A", "sheet"), ("説明", "doc")]
    assert read.files[1].text == "説明の本文"
    # サブフォルダと PDF は問い合わせの条件で除く
    assert "mimeType='application/vnd.google-apps.spreadsheet'" in parse_qs(
        urlparse(api[0]).query)["q"][0]


def test_read_errors(monkeypatch):
    assert "設定されていません" in folder_docs.read(None).error
    monkeypatch.setattr(drive, "_token", lambda: "tok")

    def denied(url, token):
        raise PermissionError("403")
    monkeypatch.setattr(folder_docs, "_get_json", denied)
    assert "読み取れませんでした" in folder_docs.read(FOLDER).error
    monkeypatch.setattr(folder_docs, "_get_json", lambda url, token: {"files": []})
    assert "ありません" in folder_docs.read(FOLDER).error


# --------------------------------------------------------------------------- #
# 形式チェック・AI の確認（API）
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo():
    r = InMemoryRepository()
    r.upsert_proposal(Proposal(proposal_id="b1", title="2027年度 予算（案）", committee="執行部",
                               doc_type="予算書", storage_uri=FOLDER))
    set_repo(r)
    yield r
    set_repo(None)


def test_format_check_reads_folder_and_reports(repo, api):
    p = proposal_format.check(repo, repo.get_proposal("b1"), NOW)
    fc = p.format_check
    assert not fc.passed and fc.fixed == []
    assert any(i.startswith("数式のエラー") for i in fc.issues)
    assert not any("本文" in i for i in fc.issues)
    # スプレッドシートの様式の全角（「２２０，０００」以外の項目名など）は表記のチェックに使わない
    assert not any(i.startswith("全角") for i in fc.issues)
    assert [s.name for s in p.sources] == ["予算書_様式A", "説明"]
    assert p.sources_read_at == NOW and p.source_error is None
    assert "【予算書_様式A／予算書（案）】" in p.content


def test_format_check_when_folder_cannot_be_read(repo, monkeypatch):
    monkeypatch.setattr(drive, "_token", lambda: "tok")
    monkeypatch.setattr(folder_docs, "_get_json", lambda url, token: {"files": []})
    p = proposal_format.check(repo, repo.get_proposal("b1"), NOW)
    assert not p.format_check.passed
    assert p.format_check.issues == [p.source_error]


def test_llm_review_reads_folder_with_budget_viewpoints(repo, api, monkeypatch):
    seen = {}

    def fake(content, **kw):
        seen["content"], seen["doc_type"] = content, kw.get("doc_type")
        return llm.Generation(text=json.dumps({"summary": "s", "points": [], "concerns": []}))
    monkeypatch.setattr(llm, "generate_review", fake)
    res = client.post("/api/proposals/b1/llm-review")
    assert res.status_code == 200, res.text
    assert seen["doc_type"] == "予算書" and "【予算書_様式A／予算書（案）】" in seen["content"]
    assert "収支差額" in llm._REVIEW_VIEWPOINTS["予算書"]


def test_llm_review_says_why_when_folder_cannot_be_read(repo, monkeypatch):
    monkeypatch.setattr(drive, "_token", lambda: "tok")
    monkeypatch.setattr(folder_docs, "_get_json", lambda url, token: {"files": []})
    res = client.post("/api/proposals/b1/llm-review")
    assert res.status_code == 503 and "ありません" in res.json()["detail"]
