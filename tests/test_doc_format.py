"""議案書の体裁の点検と自動修正、委員長への知らせ（docs/proposal-ledger-design.md §11）。"""
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import committee_docs, doc_format, line_push, main, proposal_format, proposal_submit
from app.deps import set_repo
from app.models import CommitteeSeat, Member, Proposal, ProposalStage
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
MORNING = datetime(2026, 10, 30, 9, 0)
NIGHT = datetime(2026, 10, 30, 22, 30)
ARIAL10 = {"weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
           "fontSize": {"magnitude": 10, "unit": "PT"}}
HIRA10 = {"weightedFontFamily": {"fontFamily": "HiraKakuPro-W3", "weight": 400},
          "fontSize": {"magnitude": 10, "unit": "PT"}}


class _Builder:
    """位置（UTF-16）を数えながら、表のある文書（documents.get の形）を作る。"""

    def __init__(self):
        self.index = 1

    def para(self, *runs):
        elements = []
        for text, style in runs:
            n = len(text.encode("utf-16-le")) // 2
            elements.append({"startIndex": self.index, "endIndex": self.index + n,
                             "textRun": {"content": text, "textStyle": style}})
            self.index += n
        return {"startIndex": elements[0]["startIndex"], "endIndex": self.index,
                "paragraph": {"elements": elements}}

    def cell(self, *paras):
        start = self.index
        self.index += 1
        content = [self.para(*runs) for runs in paras]
        return {"startIndex": start, "endIndex": self.index, "content": content}

    def table(self, rows):
        start = self.index
        self.index += 1
        table_rows = []
        for row in rows:
            self.index += 1
            table_rows.append({"tableCells": [self.cell(*cell) for cell in row]})
        return {"startIndex": start, "endIndex": self.index,
                "table": {"columns": len(rows[0]), "tableRows": table_rows}}


def _doc(font_of_normal=None):
    b = _Builder()
    title = b.para(("2027年度 総務委員会\n", ARIAL10))
    table = b.table([
        [[[("スローガン\n", ARIAL10)]], [[("一所懸命\n", {})]]],
        [[[("事業手法\n", ARIAL10)]],
         [[("・１月の総会（ＫＰＩ）\n", HIRA10)], [("・Arial の行\n", ARIAL10)]]],
        [[[("（参考）LOM日程\n", ARIAL10)]], [[("総会１\n", HIRA10)]]],
    ])
    normal = {"namedStyleType": "NORMAL_TEXT", "textStyle": {}}
    if font_of_normal:
        normal["textStyle"] = {"weightedFontFamily": {"fontFamily": font_of_normal}}
    return {"documentId": "d1", "revisionId": "rev1", "tabs": [{"documentTab": {
        "body": {"content": [title, table]},
        "namedStyles": {"styles": [normal]},
    }}]}


def _text_at(doc, start, end):
    for run in doc_format._all_runs(doc["tabs"][0]["documentTab"]["body"]["content"]):
        if run.start <= start and end <= run.end:
            return run.text[start - run.start:end - run.start]
    raise AssertionError((start, end))


def test_plan_fix_fonts_sizes_and_halfwidth():
    doc = _doc()
    fix = doc_format.plan_fix(doc, "Arial")
    # ヒラギノの2か所（記入欄と参考の行）をそろえる。指定のない文字は既定の Arial なのでそのまま
    assert fix.fonts == {"HiraKakuPro-W3": 2}
    # 大きさの指定がない「一所懸命」を、同じ行の項目名（10pt）にそろえる
    sizes = [r["updateTextStyle"] for r in fix.requests
             if "updateTextStyle" in r and r["updateTextStyle"]["fields"] == "fontSize"]
    assert len(sizes) == 1 and sizes[0]["textStyle"]["fontSize"]["magnitude"] == 10
    assert _text_at(doc, sizes[0]["range"]["startIndex"],
                    sizes[0]["range"]["endIndex"]) == "一所懸命\n"
    # 半角にするのは記入欄だけ（参考の行の「総会１」は変えない）
    assert sorted(fix.halfwidth) == sorted(["１", "（", "Ｋ", "Ｐ", "Ｉ", "）"])
    inserts = [r["insertText"]["text"] for r in fix.requests if "insertText" in r]
    assert sorted(inserts) == sorted(["1", "(KPI)"])
    summary = fix.summary("Arial")
    assert any("HiraKakuPro-W3 → Arial、2か所" in s for s in summary)
    assert any("１→1" in s for s in summary)


def test_halfwidth_keeps_positions_and_restores_style():
    doc = _doc()
    fix = doc_format.plan_fix(doc, "Arial")
    # 置き換えは後ろから。消す・入れる・もとの書式に戻すの3つで1組
    first = fix.requests[:3]
    assert "deleteContentRange" in first[0] and "insertText" in first[1]
    rng = first[0]["deleteContentRange"]["range"]
    assert _text_at(doc, rng["startIndex"], rng["endIndex"]) == "（ＫＰＩ）"
    assert first[2]["updateTextStyle"]["fields"] == "*"
    assert first[2]["updateTextStyle"]["textStyle"] == HIRA10
    starts = [r["deleteContentRange"]["range"]["startIndex"] for r in fix.requests
              if "deleteContentRange" in r]
    assert starts == sorted(starts, reverse=True)
    # 書式の直しは置き換えの後
    last_replace = max(i for i, r in enumerate(fix.requests) if "insertText" in r)
    first_style = min(i for i, r in enumerate(fix.requests)
                      if r.get("updateTextStyle", {}).get("fields") == "weightedFontFamily")
    assert last_replace < first_style


def test_inherited_font_follows_named_style():
    """標準のスタイルが別のフォントなら、指定のない文字もそろえる。"""
    fix = doc_format.plan_fix(_doc(font_of_normal="Meiryo"), "Arial")
    assert fix.fonts == {"HiraKakuPro-W3": 2, "Meiryo": 1}


def test_clean_doc_needs_no_fix():
    fix = doc_format.plan_fix(_doc(), "HiraKakuPro-W3")
    assert "HiraKakuPro-W3" not in fix.fonts
    assert doc_format.plan_fix(
        {"revisionId": "r", "tabs": [{"documentTab": {"body": {"content": []}}}]}).empty


def test_doc_font_env(monkeypatch):
    assert doc_format.doc_font() == "Arial"
    monkeypatch.setenv("DOC_FONT", "Noto Sans JP")
    assert doc_format.doc_font() == "Noto Sans JP"


@pytest.mark.parametrize("name", ["business_plan_doc.json", "committee_summary_doc.json"])
def test_fixtures_do_not_break(name):
    doc = json.loads((Path(__file__).parent / "fixtures" / name).read_text())
    fix = doc_format.plan_fix(doc)
    for r in fix.requests:
        rng = (r.get("updateTextStyle") or r.get("deleteContentRange") or {}).get("range")
        if rng:
            assert rng["startIndex"] < rng["endIndex"]


# --------------------------------------------------------------------------- #
# 形式チェック → 直す → 委員長に知らせる
# --------------------------------------------------------------------------- #
@pytest.fixture
def pushed(monkeypatch):
    sent = []
    monkeypatch.setattr(line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs[0].text)) or True)
    return sent


@pytest.fixture
def docs(monkeypatch):
    """Google ドキュメントの代わり。書き込むと「直った」文書を返す。"""
    state = {"doc": _doc(), "applied": []}
    monkeypatch.setenv("COMMITTEE_DOCS_FOLDER", "root")
    monkeypatch.setattr(committee_docs, "_token", lambda: "tok")

    def read(doc_id, token):
        return state["doc"]

    def apply(doc_id, revision, requests, token):
        assert revision == "rev1"
        state["applied"].append(requests)
        state["doc"] = _fixed_doc()
        return {}
    monkeypatch.setattr(committee_docs, "read_doc", read)
    monkeypatch.setattr(committee_docs, "apply", apply)
    return state


def _fixed_doc():
    text = json.dumps(_doc(), ensure_ascii=False)
    text = text.replace("HiraKakuPro-W3", "Arial")
    text = text.replace("・１月の総会（ＫＰＩ）", "・1月の総会(KPI)")
    fixed = json.loads(text)
    fixed["revisionId"] = "rev2"
    slogan = fixed["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][0]
    slogan["tableCells"][1]["content"][0]["paragraph"]["elements"][0]["textRun"]["textStyle"] = \
        ARIAL10
    return fixed


@pytest.fixture
def repo(pushed):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m_chair", name="委員長 太郎", line_user_id="U_chair",
                           committees=[CommitteeSeat(committee="総務委員会", role="委員長")]))
    r.upsert_member(Member(member_id="m_reg", name="委員 花子", line_user_id="U_reg",
                           committees=[CommitteeSeat(committee="総務委員会", role="委員")]))
    r.upsert_proposal(Proposal(
        proposal_id="p1", title="2027年度 総務委員会 年間計画（サマリー）", committee="総務委員会",
        doc_type="年間計画（サマリー）", doc_id="d1", content="【事業手法】\n・１月",
        storage_uri="https://docs.google.com/document/d/d1/edit"))
    set_repo(r)
    yield r
    set_repo(None)


def test_check_fixes_document_and_tells_chair(repo, docs, pushed):
    p = proposal_format.check(repo, repo.get_proposal("p1"), MORNING)
    fc = p.format_check
    assert len(docs["applied"]) == 1
    assert any("フォント" in x for x in fc.fixed) and any("半角" in x for x in fc.fixed)
    # 直した後の文書を写し直してから調べるので、全角の指摘は残らない
    assert not any("全角" in x for x in fc.issues)
    assert "1月" in p.content
    assert fc.fixed_at == MORNING and fc.notified_at == MORNING
    assert fc.notified_to == ["委員長 太郎"]
    assert [to for to, _ in pushed] == ["U_chair"]
    assert "【体裁を直しました】" in pushed[0][1] and "内容は変えていません" in pushed[0][1]
    # もう一度チェックしても、直す点がなければ書き込まず、知らせない
    proposal_format.check(repo, repo.get_proposal("p1"), MORNING)
    assert len(docs["applied"]) == 1 and len(pushed) == 1


def test_fix_at_night_is_told_in_the_morning(repo, docs, pushed):
    p = proposal_format.check(repo, repo.get_proposal("p1"), NIGHT)
    assert p.format_check.fixed and p.format_check.notified_at is None
    assert pushed == []
    # 夜のうちにもう一度チェックしても、まだ知らせていない直しは消えない
    p = proposal_format.check(repo, repo.get_proposal("p1"), NIGHT)
    assert p.format_check.fixed and p.format_check.fixed_at == NIGHT
    assert proposal_format.send_pending(repo, MORNING) == 1
    assert proposal_format.send_pending(repo, MORNING) == 0
    assert [to for to, _ in pushed] == ["U_chair"]


def test_document_error_keeps_text_check(repo, docs, monkeypatch, pushed):
    def broken(doc_id, revision, requests, token):
        raise RuntimeError("400 revision mismatch")
    monkeypatch.setattr(committee_docs, "apply", broken)
    p = proposal_format.check(repo, repo.get_proposal("p1"), MORNING)
    assert not p.format_check.passed
    assert any("自動で直せませんでした" in x for x in p.format_check.issues)
    assert any("全角英数字" in x for x in p.format_check.issues)
    assert p.format_check.fixed == [] and pushed == []


def test_without_setting_says_so(repo, pushed, monkeypatch):
    monkeypatch.delenv("COMMITTEE_DOCS_FOLDER", raising=False)
    p = proposal_format.check(repo, repo.get_proposal("p1"), MORNING)
    assert p.format_check.fixed == [] and pushed == []
    assert any("COMMITTEE_DOCS_FOLDER" in x for x in p.format_check.issues)
    assert any("全角英数字" in x for x in p.format_check.issues)


def test_without_document_only_text_is_checked(repo, pushed, monkeypatch):
    monkeypatch.delenv("COMMITTEE_DOCS_FOLDER", raising=False)
    repo.upsert_proposal(repo.get_proposal("p1").model_copy(update={"doc_id": None}))
    p = proposal_format.check(repo, repo.get_proposal("p1"), MORNING)
    assert p.format_check.fixed == []
    assert not any("COMMITTEE_DOCS_FOLDER" in x for x in p.format_check.issues)
    assert any("全角英数字" in x for x in p.format_check.issues)


def test_api_records_audit(repo, docs, monkeypatch):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return MORNING
    from app import proposals_api
    monkeypatch.setattr(proposals_api, "datetime", _Clock)
    res = client.post("/api/proposals/p1/format-check")
    assert res.status_code == 200
    assert res.json()["notified_to"] == ["委員長 太郎"]
    actions = [a.action for a in repo.list_audit()]
    assert "proposal.format_fix" in actions


# --------------------------------------------------------------------------- #
# 委員長が［提出する］を押したとき
# --------------------------------------------------------------------------- #
def _submit(repo, check=proposal_format.check):
    repo.upsert_member(Member(member_id="m_sed", name="専務 四郎", line_user_id="U_sed",
                              officer_role="専務理事"))
    chair = repo.get_member("m_chair")
    return proposal_submit.submit(repo, chair, "p1", MORNING, check=check)


def test_submit_checks_and_fixes_before_telling_sed(repo, docs, pushed):
    p = _submit(repo)
    assert p.stage == ProposalStage.submitted
    assert len(docs["applied"]) == 1 and p.format_check.fixed_at == MORNING
    sent = dict(pushed)
    assert "【体裁を直しました】" in sent["U_chair"]
    assert "【提出】" in sent["U_sed"]
    assert f"体裁を直しました: {len(p.format_check.fixed)}点" in sent["U_sed"]


def test_submit_goes_through_even_if_check_fails(repo, pushed):
    def broken(repo, p, now):
        raise RuntimeError("boom")
    p = _submit(repo, check=broken)
    assert p.stage == ProposalStage.submitted
    assert [to for to, _ in pushed] == ["U_sed"]


def test_liff_submit_runs_check(repo, docs, pushed, monkeypatch):
    from app import liff_api
    from app import schedule_poll as sp

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return MORNING
    monkeypatch.setattr(liff_api, "datetime", _Clock)
    monkeypatch.setenv("LIFF_ID", "2000000000-abcdEFGH")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "2000000000")
    sp._token_cache.clear()
    monkeypatch.setattr(sp, "_verify_id_token_raw", lambda t, c: {"sub": "U_chair"})
    res = TestClient(main.app).post("/liff/api/proposals/p1/submit",
                                    headers={"Authorization": "Bearer tok"})
    assert res.status_code == 200, res.text
    assert "体裁" in res.json()["message"]
    assert len(docs["applied"]) == 1
