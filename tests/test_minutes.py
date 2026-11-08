"""会議の録画から議事録を作る（docs/meeting-minutes-design.md §13）。

AI（Gemini）・ドライブ・Cloud Storage・Pub/Sub はモック。架空の会議・会員・議案で確かめる。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app import minutes as mn
from app import minutes_send as ms
from app.deps import set_repo
from app.models import (
    Attendance,
    AttendanceStatus,
    Event,
    EventType,
    LineGroup,
    Member,
    MinutesSetting,
    MinutesStatus,
    QuietHours,
    Settings,
    TargetScope,
    TargetScopeKind,
)
from app.repository import InMemoryRepository
from tests.conftest import ADMIN_AUTH

client = TestClient(main.app, headers=ADMIN_AUTH)
NOW = datetime(2026, 11, 5, 23, 0)
REC = "https://drive.google.com/file/d/REC1234567890/view"
NO_QUIET = QuietHours(start="00:00", end="00:00")

TRANSCRIPTS = [
    # 1つ目の区切り（録画の頭からの時刻）。m9 は出席予定者にいないので話者にしない
    {"segments": [
        {"start": "00:00:10", "end": "00:00:30", "speaker": "m1", "speaker_by": "voice",
         "text": "第1号議案の説明をします", "proposal": "p1", "screen": "予算書（案）"},
        {"start": "00:05:00", "speaker": "m9", "text": "この80行目は2023になっています"},
        {"start": "00:06:00", "speaker": None, "text": "ハナちゃんの件ですが"}],
     "unclear": [
        {"at": "00:05:00", "kind": "speaker", "question": "話しているのは誰か",
         "context": "80行目"},
        {"at": "00:06:00", "kind": "word", "question": "ハナちゃんは誰か", "context": "呼び名"}]},
    # 2つ目の区切り（区切りの頭からの時刻で返ってきた → 録画の頭からに直す）。区切りは重ねない
    {"segments": [
        {"start": "00:00:10", "speaker": "m1", "text": "区切りの頭"},
        {"start": "00:01:00", "speaker": "m2", "text": "次回は理事会で確認します"},
        # 時刻の桁の取り違え（本番で 00:10:35 を 01:00:35 と書いた）→ 直前の時刻に置き換える
        {"start": "01:00:35", "end": "01:00:50", "speaker": "m1", "text": "続けて説明します"}],
     "unclear": []},
]


@pytest.fixture
def repo(monkeypatch):
    r = InMemoryRepository()
    r.upsert_member(Member(member_id="m1", name="青木 一郎", officer_role="理事長",
                           line_user_id="U1"))
    r.upsert_member(Member(member_id="m2", name="江口 隆之", officer_role="専務理事",
                           line_user_id="U2"))
    r.upsert_member(Member(member_id="m3", name="杉山 花子", officer_role="副理事長",
                           line_user_id="U3"))
    r.upsert_member(Member(member_id="m4", name="田村 修", committee="まちづくり委員会",
                           committee_role="委員長", line_user_id="U4"))
    r.upsert_event(Event(
        event_id="ev1", type=EventType.五役会, title="第2回五役会",
        datetime_start=datetime(2026, 11, 5, 19), datetime_end=datetime(2026, 11, 5, 21),
        location="オンライン",
        target_scope=TargetScope(kind=TargetScopeKind.officer,
                                 value=["理事長", "専務理事", "副理事長"])))
    r.upsert_attendance(Attendance(event_id="ev1", member_id="m3",
                                 status=AttendanceStatus.欠席))
    r.save_line_group(LineGroup(group_id="Cgoyaku", group_name="五役のグループ",
                                joined_at=datetime(2026, 10, 1)))
    r.save_line_group(LineGroup(group_id="Cmachi", group_name="まちづくり委員会",
                                joined_at=datetime(2026, 10, 1)))
    # 画面からの送信は今の時刻で判定するので、静音時間なしにしておく（静音時間は別のテストで）
    # 画面で確かめる段も通す（既定はしない。しないときは別のテストで）
    r.save_settings(Settings(line_groups={"goyaku": "Cgoyaku"}, quiet_hours=NO_QUIET,
                             minutes=MinutesSetting(verify_screen=True)))
    set_repo(r)

    monkeypatch.setenv("MINUTES_BUCKET", "test-minutes")
    monkeypatch.setenv("MINUTES_TOPIC", "minutes-jobs")
    published, sent, notices, calls = [], [], [], []
    monkeypatch.setattr(mn, "publish", lambda m: published.append(
        {"event_id": m.event_id, "run_id": m.run_id, "seq": m.seq}) or True)
    monkeypatch.setattr(mn, "_drive_get", lambda url: {
        "id": "REC1234567890", "name": "第2回五役会 - 2026/11/05 19:00 JST.mp4",
        "mimeType": "video/mp4", "size": "1000", "parents": ["FOLDER1"],
        "videoMediaMetadata": {"durationMillis": str(20 * 60 * 1000)}})
    monkeypatch.setattr(mn, "_copy_to_gcs", lambda fid, size, name: f"gs://test-minutes/{name}")
    monkeypatch.setattr(mn, "_delete_gcs", lambda uri: calls.append(("delete", uri)))
    monkeypatch.setattr(mn, "_notify", lambda repo, event, text, now: notices.append(text))
    monkeypatch.setattr(ms.line_push, "push_messages",
                        lambda to, msgs: sent.append((to, msgs)) or True)

    def generate(task, parts, **kw):
        calls.append((task, parts, kw))
        usage = mn.InferenceUsage(model="gemini-2.5-pro", input_tokens=10, output_tokens=5)
        if task == "minutes_transcribe":
            n = sum(1 for c in calls if c[0] == "minutes_transcribe")
            return TRANSCRIPTS[n - 1], usage
        if task == "minutes_verify":
            return {"answers": [
                {"index": 1, "resolved": True, "speaker": "m2", "answer": "江口",
                 "evidence": "発言中の枠: 江口"},
                {"index": 2, "resolved": False, "answer": "わからない", "evidence": None}]}, usage
        if task == "minutes_draft":
            return {"sections": [{
                "proposal": "p1", "title": "第1号議案 予算（案）",
                "explained": ["今年度の補正予算がベース"],
                "opinions": [{"by": "m2", "target": "80行目", "point": "年を2027に直す",
                              "response": "直します", "response_by": "m1"},
                             {"by": "m7", "point": "話者を作らない"}],
                "decided": ["理事会に上程する"],
                "action_items": [{"owner": "m1", "what": "80行目の年を直す",
                                  "due": "次の理事会まで"},
                                 {"owner": "m3", "what": "欠席者の宿題"}]}],
                "confirmations": ["次回は11月13日"],
                "sensitive": [{"at": "00:06:00", "kind": "candidate",
                               "summary": "入会候補者の話"}]}, usage
        if task == "minutes_committee":
            return {"text": "【第2回五役会　まちづくり委員会に関する議事録】\n■意見\n・…"}, usage
        raise AssertionError(task)

    monkeypatch.setattr(mn, "generate", generate)
    r.published, r.sent, r.notices, r.calls = published, sent, notices, calls
    yield r
    set_repo(None)


def _run(repo) -> None:
    """知らせを順に処理して、案ができるまで進める。"""
    for _ in range(20):
        if not repo.published:
            return
        mn.handle(repo, repo.published.pop(0), NOW)


def _ready(repo):
    res = client.post("/api/events/ev1/minutes", json={"recording": REC})
    assert res.status_code == 200, res.text
    _run(repo)
    return repo.get_minutes("ev1")


# --------------------------------------------------------------------------- #
# 小さな部品
# --------------------------------------------------------------------------- #
def test_time_helpers_and_chunks():
    assert mn.to_sec("01:02:03") == 3723 and mn.to_sec("05:00") == 300
    assert mn.hms(3723) == "01:02:03"
    assert mn.chunks(20 * 60) == [(0, 900), (900, 1200)]
    assert mn.chunks(0) == [(0, 1)]


def test_chunk_times_fix_relative_and_mistyped():
    # 区切りの頭からの時刻 → 録画の頭からに直す
    assert mn._chunk_offset([10, 60, 300], 900, 1800) == 900
    assert mn._chunk_offset([910, 960], 900, 1800) == 0
    # 範囲に入らない時刻（桁の取り違え・空）は直前の正しい時刻にする。前の時刻より戻らない
    assert mn._chunk_times([600, 3635, None, 620], 0, 900, 0) == [600, 600, 600, 620]
    assert mn._chunk_times([7140, 950, 920], 900, 1800, 0) == [900, 950, 950]
    # 範囲に入る時刻が半分に満たなければ、区切りの中に順に割り振る（発言は捨てない）
    assert mn._chunk_times([7140, 7200, None, 950], 900, 1800, 0) == [900, 1125, 1350, 1575]


def test_drive_id_from_urls():
    assert mn.drive_id(REC) == "REC1234567890"
    assert mn.drive_id("https://drive.google.com/drive/u/4/folders/FOLDERabcdef12") \
        == "FOLDERabcdef12"
    assert mn.drive_id("https://example.com/") is None


def test_split_at_section_breaks():
    text = "\n\n".join(["■" + "あ" * 3000, "■" + "い" * 3000])
    parts = ms._split(text)
    assert len(parts) == 2 and parts[1].startswith("■い")
    assert all(len(p) <= ms.LINE_LIMIT for p in ms._split("x" * 12000))


# --------------------------------------------------------------------------- #
# 作業の流れ
# --------------------------------------------------------------------------- #
def test_pipeline_runs_step_by_step(repo):
    minutes = _ready(repo)
    assert minutes.status == MinutesStatus.ready
    # 区切りの頭からの時刻は録画の頭からに直す。取り違えた時刻があっても発言を捨てない
    assert [s.start for s in minutes.segments] == ["00:00:10", "00:05:00", "00:06:00",
                                                   "00:15:10", "00:16:00", "00:16:00"]
    assert minutes.segments[5].end is None
    # 出席予定者でない会員IDは話者にしない。画面で確かめた所だけ根拠つきで直す
    seg = minutes.segments[1]
    assert seg.speaker == "m2" and seg.speaker_by == "screen" and "発言中の枠" in seg.evidence
    assert minutes.segments[2].speaker is None
    assert [u.status for u in minutes.unclear] == ["verified", "skipped"]
    # 録画の範囲・画面の枚数・解像度
    transcribe = [c for c in repo.calls if c[0] == "minutes_transcribe"]
    video = transcribe[1][1][0]
    assert video["videoMetadata"]["startOffset"] == "900s"
    assert video["fileData"]["fileUri"].startswith("gs://test-minutes/minutes/ev1/")
    assert transcribe[0][2]["resolution"] == "MEDIA_RESOLUTION_LOW"
    verify = next(c for c in repo.calls if c[0] == "minutes_verify")
    assert verify[2]["resolution"] == "MEDIA_RESOLUTION_MEDIUM"
    assert verify[1][0]["videoMetadata"] == {"startOffset": "280s", "endOffset": "320s",
                                             "fps": 1}
    # まとめ: 作った会員IDは消し、内々の話は分ける。一時置き場は消す
    assert minutes.sections[0].opinions[1].by is None
    assert minutes.sensitive[0].kind == "candidate" and not minutes.sensitive[0].include
    assert minutes.gcs_uri is None and any(c[0] == "delete" for c in repo.calls)
    assert "【議事録の案】第2回五役会" in repo.notices[-1]
    kinds = {log.kind for log in repo.list_inference_logs()}
    assert {"minutes_transcribe", "minutes_verify", "minutes_draft"} <= kinds


def test_screen_check_is_off_by_default(repo):
    repo.save_settings(repo.get_settings().model_copy(update={"minutes": MinutesSetting()}))
    minutes = _ready(repo)
    assert minutes.status == MinutesStatus.ready
    # 画面は見ずに、わからない所は人が直す不明点として残す
    assert not any(c[0] == "minutes_verify" for c in repo.calls)
    assert [u.status for u in minutes.unclear] == ["open", "open"]
    assert minutes.segments[1].speaker is None


def _with_transcripts(repo, monkeypatch, replies):
    """書き起こしの返事を順に差し替える（ほかの段は元のモックのまま）。"""
    original = mn.generate
    queue = list(replies)

    def generate(task, parts, **kw):
        if task == "minutes_transcribe":
            repo.calls.append((task, parts, kw))
            return queue.pop(0), mn.InferenceUsage(model="m", input_tokens=1, output_tokens=1)
        return original(task, parts, **kw)

    monkeypatch.setattr(mn, "generate", generate)


def test_empty_chunk_is_retried(repo, monkeypatch):
    # 本番で区切りによって発言が1件も入らない返事があった → やり直して埋める
    _with_transcripts(repo, monkeypatch, [{"segments": [], "unclear": []}, *TRANSCRIPTS])
    minutes = _ready(repo)
    assert minutes.status == MinutesStatus.ready
    assert len([c for c in repo.calls if c[0] == "minutes_transcribe"]) == 3
    assert minutes.segments[0].start == "00:00:10"
    assert repo.calls[0][2]["schema"] is mn.TRANSCRIBE_SCHEMA  # 返事の形を決めて頼む


def test_empty_chunk_left_for_people_after_retries(repo, monkeypatch):
    empty = {"segments": [], "unclear": []}
    _with_transcripts(repo, monkeypatch, [empty, empty, empty, TRANSCRIPTS[1]])
    minutes = _ready(repo)
    assert minutes.status == MinutesStatus.ready
    gap = next(u for u in minutes.unclear if u.kind == "gap")
    assert gap.at == "00:00:00" and "00:00:00〜00:15:00 を書き起こせませんでした" in gap.question


def test_list_reply_is_read_as_segments(repo, monkeypatch):
    _with_transcripts(repo, monkeypatch, [{"items": TRANSCRIPTS[0]["segments"]}, TRANSCRIPTS[1]])
    minutes = _ready(repo)
    assert minutes.segments[0].text == "第1号議案の説明をします"


def test_stale_and_duplicate_messages_are_ignored(repo):
    client.post("/api/events/ev1/minutes", json={"recording": REC})
    first = repo.published.pop(0)
    assert mn.handle(repo, {**first, "run_id": "old"}, NOW) == "stale"
    assert mn.handle(repo, first, NOW) == "transcribing"
    assert mn.handle(repo, first, NOW) == "stale"  # 同じ段は二重に進めない


def test_failures_stop_after_three_and_resume(repo, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("Gemini API 500")

    monkeypatch.setattr(mn, "generate", broken)
    client.post("/api/events/ev1/minutes", json={"recording": REC})
    _run(repo)
    minutes = repo.get_minutes("ev1")
    assert minutes.status == MinutesStatus.failed and minutes.attempts == 3
    assert "Gemini API 500" in minutes.error and "作れませんでした" in repo.notices[-1]
    res = client.post("/api/events/ev1/minutes/resume")
    assert res.status_code == 200, res.text
    minutes = repo.get_minutes("ev1")
    assert minutes.status == MinutesStatus.transcribing and minutes.attempts == 0
    assert repo.published


def test_start_requires_setup_and_rejects_double_start(repo, monkeypatch):
    client.post("/api/events/ev1/minutes", json={"recording": REC})
    res = client.post("/api/events/ev1/minutes", json={"recording": REC})
    assert res.status_code == 409
    monkeypatch.delenv("MINUTES_BUCKET")
    res = client.post("/api/events/ev1/minutes", json={"recording": REC})
    assert res.status_code == 503 and "setup_minutes" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 確認と送信
# --------------------------------------------------------------------------- #
def test_group_preview_mentions_and_sensitive(repo):
    _ready(repo)
    view = client.get("/api/events/ev1/minutes").json()
    preview = view["group_preview"]
    assert preview.startswith("@All\n") and "@杉山 花子 欠席でしたので" in preview
    assert "出席：青木理事長、江口専務理事" in preview and "欠席：杉山副理事長" in preview
    assert "80行目の年を直す（次の理事会まで） →@青木 一郎" in preview
    assert "入会候補者" not in preview and "録画から AI でまとめた" in preview
    assert view["group"] == {"group_id": "Cgoyaku", "group_name": "五役のグループ"}
    # 内々の話を入れると決めたときだけ入る
    minutes = repo.get_minutes("ev1")
    body = {"sections": [s.model_dump() for s in minutes.sections],
            "confirmations": minutes.confirmations,
            "include_sensitive": [minutes.sensitive[0].sensitive_id]}
    res = client.put("/api/events/ev1/minutes", json=body)
    assert "■その他\n・入会候補者の話" in res.json()["group_preview"]


def test_send_group_checks_destination(repo):
    _ready(repo)
    res = client.post("/api/events/ev1/minutes/send-group", json={"expected_group_id": "Cx"})
    assert res.status_code == 409 and repo.sent == []
    res = client.post("/api/events/ev1/minutes/send-group",
                      json={"expected_group_id": "Cgoyaku"})
    assert res.status_code == 200, res.text
    to, msgs = repo.sent[0]
    payload = msgs[0].to_dict()
    assert to == "Cgoyaku" and payload["type"] == "textV2"
    subs = payload["substitution"]
    assert subs["everyone"]["mentionee"]["type"] == "all"
    assert {v["mentionee"].get("userId") for k, v in subs.items() if k != "everyone"} \
        == {"U1", "U3"}
    sends = repo.get_minutes("ev1").sends
    assert sends[0].to_name == "五役のグループ" and sends[0].kind == "group"
    assert any(a.action == "minutes.send_group" for a in repo.list_audit())


def test_send_blocked_in_quiet_hours_and_kill_switch(repo):
    _ready(repo)
    event = repo.get_event("ev1")
    repo.save_settings(Settings(line_groups={"goyaku": "Cgoyaku"},
                                quiet_hours=QuietHours(start="22:00", end="07:00")))
    with pytest.raises(mn.MinutesError, match="静音時間"):
        ms.send_group(repo, event, expected_group_id="Cgoyaku", actor="t", now=NOW)
    repo.save_settings(Settings(line_groups={"goyaku": "Cgoyaku"}, quiet_hours=NO_QUIET,
                                kill_switch=True))
    with pytest.raises(mn.MinutesError, match="一時停止"):
        ms.send_group(repo, event, expected_group_id="Cgoyaku", actor="t", now=NOW)
    assert repo.sent == []


def test_committee_minutes_to_chair_or_group(repo):
    _ready(repo)
    view = client.get("/api/events/ev1/minutes").json()
    machi = next(c for c in view["committees"] if c["committee"] == "まちづくり委員会")
    assert machi["chair"] == {"member_id": "m4", "name": "田村 修", "linked": True}
    assert machi["group"] == {"group_id": "Cmachi", "group_name": "まちづくり委員会"}
    res = client.post("/api/events/ev1/minutes/committee",
                      json={"committee": "まちづくり委員会"})
    assert res.status_code == 200, res.text
    prompt = next(c for c in repo.calls if c[0] == "minutes_committee")[1][0]["text"]
    assert "江口専務理事" in prompt and '"m2"' not in prompt  # 会員IDでなく役職＋名字で渡す
    # 送り先が画面と違えば送らない
    res = client.post("/api/events/ev1/minutes/committee/send", json={
        "committee": "まちづくり委員会", "channel": "user", "expected_to_id": "m1"})
    assert res.status_code == 409 and repo.sent == []
    # 委員長への個別の LINE（画面で直した文面で）
    res = client.post("/api/events/ev1/minutes/committee/send", json={
        "committee": "まちづくり委員会", "channel": "user", "expected_to_id": "m4",
        "text": "直した文面"})
    assert res.status_code == 200, res.text
    assert repo.sent[-1][0] == "U4" and repo.sent[-1][1][0].text == "直した文面"
    # 委員会のグループ
    res = client.post("/api/events/ev1/minutes/committee/send", json={
        "committee": "まちづくり委員会", "channel": "group", "expected_to_id": "Cmachi"})
    assert res.status_code == 200, res.text
    assert repo.sent[-1][0] == "Cmachi"
    sends = repo.get_minutes("ev1").sends
    assert [(s.channel, s.to_name) for s in sends] == [
        ("user", "田村 修（まちづくり委員会委員長）"), ("group", "まちづくり委員会")]


def test_committee_chair_without_line_is_refused(repo):
    _ready(repo)
    repo.upsert_member(repo.get_member("m4").model_copy(update={"line_user_id": None}))
    client.post("/api/events/ev1/minutes/committee", json={"committee": "まちづくり委員会"})
    res = client.post("/api/events/ev1/minutes/committee/send", json={
        "committee": "まちづくり委員会", "channel": "user", "expected_to_id": "m4"})
    assert res.status_code == 409 and repo.sent == []


def test_resolve_unclear_and_remember_nickname(repo):
    minutes = _ready(repo)
    skipped = next(u for u in minutes.unclear if u.status == "skipped")
    res = client.post(f"/api/events/ev1/minutes/unclear/{skipped.unclear_id}", json={
        "status": "resolved", "speaker": "m3", "remember_nickname": "ハナちゃん"})
    assert res.status_code == 200, res.text
    assert repo.get_minutes("ev1").segments[2].speaker == "m3"
    assert repo.get_member("m3").nicknames == ["ハナちゃん"]
    assert "呼び名 ハナちゃん" in mn.materials(repo, repo.get_event("ev1"))
    # 出席予定者でない人は選べない
    res = client.post(f"/api/events/ev1/minutes/unclear/{skipped.unclear_id}",
                      json={"speaker": "m4"})
    assert res.status_code == 400


def test_save_doc_reuses_the_same_file(repo, monkeypatch):
    _ready(repo)
    uploads = []
    monkeypatch.setattr(ms, "_upload", lambda doc_id, name, folder, body: uploads.append(
        (doc_id, name, folder, body)) or "DOC1")
    assert client.post("/api/events/ev1/minutes/doc").status_code == 200
    assert client.post("/api/events/ev1/minutes/doc").status_code == 200
    assert [u[0] for u in uploads] == [None, "DOC1"] and uploads[0][2] == "FOLDER1"
    body = uploads[0][3]
    assert "付録：書き起こし" in body and "（話者不明）" in body and "入会候補者" not in body
    assert client.get("/api/events/ev1/minutes").json()["doc_url"].endswith("/DOC1/edit")
