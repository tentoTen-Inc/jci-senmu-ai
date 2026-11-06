"""会議の録画から議事録を作る（docs/meeting-minutes-design.md）。

流れ（§3）: ① 録画を一時置き場（Cloud Storage）に写す → ② 一次の読み取り（区切りごとに音声＋
まばらな画面）→ ③ 画面での確認（わからない所の前後を細かく見る）→ ④ まとめ → ⑤ 一時置き場を消す。
一つの会議で数分〜10分かかるので、画面の操作の中では行わない。作業（MeetingMinutes）を作って
Pub/Sub に知らせを出し、ワーカー（/pubsub/minutes）が1回に1段ずつ進めて次の段の知らせを出す。

送るのはいつも人の確認の後（管理画面のボタン。app/minutes_send.py）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from . import config, lake, llm, lom, lom_drive
from .audit import write_audit
from .events import resolve_targets
from .inference import record_inference
from .meeting_package import proposals_for
from .models import (
    AttendanceStatus,
    Event,
    InferenceUsage,
    MeetingMinutes,
    Member,
    MinutesActionItem,
    MinutesOpinion,
    MinutesSection,
    MinutesSegment,
    MinutesSensitive,
    MinutesStatus,
    MinutesUnclear,
)
from .repository import Repository
from .sed_notice import notify_sed

logger = logging.getLogger("jci-agent.minutes")

#: 一次の読み取りの区切り（秒）と前後の重なり。Cloud Run の1回の処理（最長10分）に収まる長さ
CHUNK_SEC = 15 * 60
OVERLAP_SEC = 30
#: 一次の読み取りで画面を見る間隔（秒に何枚）。トークンの大半は音声で、画面のぶんは少ない
SPARSE_FPS = 1 / 15
#: 画面での確認で見る前後の秒数と、1回の AI の呼び出しで確かめる数
VERIFY_WINDOW_SEC = 20
VERIFY_BATCH = 5
#: 3回失敗したら止めて専務に知らせる（§5.8）
MAX_ATTEMPTS = 3
VIDEO_MIME = "video/mp4"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
GCS_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
GCS_OBJECT = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}"
#: 録画を写すときに1回で送る大きさ（Cloud Storage の再開可能アップロードは 256KiB の倍数）
COPY_CHUNK = 32 * 1024 * 1024

ACTIVE = (MinutesStatus.queued, MinutesStatus.copying, MinutesStatus.transcribing,
          MinutesStatus.verifying, MinutesStatus.drafting)


class MinutesError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
def bucket() -> str | None:
    return os.environ.get("MINUTES_BUCKET", "").strip() or None


def topic_path() -> str | None:
    value = os.environ.get("MINUTES_TOPIC", "").strip()
    if not value:
        return None
    if value.startswith("projects/"):
        return value
    return f"projects/{config.PROJECT_ID}/topics/{value}"


def is_configured() -> bool:
    return bool(bucket() and topic_path())


# --------------------------------------------------------------------------- #
# 時刻（録画の頭からの "HH:MM:SS"）
# --------------------------------------------------------------------------- #
def to_sec(value: str | None) -> int | None:
    if not value:
        return None
    parts = [p for p in str(value).strip().split(":") if p != ""]
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return sec


def hms(sec: int) -> str:
    sec = max(int(sec), 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def chunks(duration_sec: int) -> list[tuple[int, int]]:
    """一次の読み取りの区切り（始め・終わりの秒）。前後を少し重ねる。"""
    out = []
    start = 0
    while start < duration_sec:
        end = min(start + CHUNK_SEC, duration_sec)
        out.append((max(start - OVERLAP_SEC, 0), min(end + OVERLAP_SEC, duration_sec)))
        start = end
    return out or [(0, max(duration_sec, 1))]


# --------------------------------------------------------------------------- #
# 録画（LOM のドライブ）
# --------------------------------------------------------------------------- #
_DRIVE_ID = re.compile(r"(?:/d/|/folders/|[?&]id=)([A-Za-z0-9_-]{10,})")


def drive_id(url_or_id: str) -> str | None:
    """Google ドライブの URL（ファイル・フォルダ）または ID から ID を取り出す。"""
    text = (url_or_id or "").strip()
    found = _DRIVE_ID.search(text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    return None


def _drive_get(url: str) -> dict:
    """Drive API を GET（LOM 公式アカウント。テストでモックする境界）。"""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {lom_drive._token()}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MinutesError(404, "録画が見つかりません。LOM のアカウントで開ける URL か"
                                    "確かめてください。") from exc
        raise


_FIELDS = "id,name,mimeType,size,parents,videoMediaMetadata(durationMillis)"


def find_recording(url_or_id: str) -> dict:
    """URL のファイル（動画）か、フォルダの中でいちばん大きい動画の情報を返す。"""
    file_id = drive_id(url_or_id)
    if not file_id:
        raise MinutesError(400, "録画の Google ドライブの URL を入れてください。")
    meta = _drive_get(f"{DRIVE_FILES}/{file_id}?supportsAllDrives=true&fields={_FIELDS}")
    if meta.get("mimeType") == lom_drive.FOLDER:
        params = urllib.parse.urlencode({
            "q": f"'{file_id}' in parents and mimeType contains 'video/' and trashed = false",
            "fields": f"files({_FIELDS})", "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        })
        files = _drive_get(f"{DRIVE_FILES}?{params}").get("files", [])
        if not files:
            raise MinutesError(404, "このフォルダに録画（動画）がありません。")
        meta = max(files, key=lambda f: int(f.get("size") or 0))
    if not str(meta.get("mimeType", "")).startswith("video/"):
        raise MinutesError(400, "動画のファイルではありません。Meet の録画を指定してください。")
    return meta


def _duration(meta: dict) -> int | None:
    millis = (meta.get("videoMediaMetadata") or {}).get("durationMillis")
    return int(int(millis) / 1000) if millis else None


def _copy_to_gcs(file_id: str, size: int, object_name: str) -> str:
    """ドライブの録画を一時置き場へ流しながら写す（全部をメモリに載せない）。gs:// を返す。"""
    name = urllib.parse.quote(object_name, safe="")
    init = urllib.request.Request(
        GCS_UPLOAD.format(bucket=bucket()) + f"?uploadType=resumable&name={name}",
        data=json.dumps({"contentType": VIDEO_MIME}).encode(), method="POST")
    init.add_header("Authorization", f"Bearer {lake._token()}")
    init.add_header("Content-Type", "application/json")
    init.add_header("X-Upload-Content-Type", VIDEO_MIME)
    init.add_header("X-Upload-Content-Length", str(size))
    with urllib.request.urlopen(init, timeout=30) as resp:
        session = resp.headers["Location"]
    src = urllib.request.Request(f"{DRIVE_FILES}/{file_id}?alt=media&supportsAllDrives=true")
    src.add_header("Authorization", f"Bearer {lom_drive._token()}")
    sent = 0
    with urllib.request.urlopen(src, timeout=120) as body:
        while sent < size:
            buf = bytearray()
            while len(buf) < COPY_CHUNK and sent + len(buf) < size:
                piece = body.read(min(COPY_CHUNK - len(buf), size - sent - len(buf)))
                if not piece:
                    break
                buf.extend(piece)
            if not buf:
                raise RuntimeError("録画の読み込みが途中で終わりました")
            last = sent + len(buf) - 1
            put = urllib.request.Request(session, data=bytes(buf), method="PUT")
            put.add_header("Content-Range", f"bytes {sent}-{last}/{size}")
            try:
                with urllib.request.urlopen(put, timeout=120):
                    pass
            except urllib.error.HTTPError as exc:
                if exc.code != 308:  # 308 = 続きを待っている
                    raise
            sent = last + 1
    return f"gs://{bucket()}/{object_name}"


def _delete_gcs(gcs_uri: str) -> None:
    path = gcs_uri.removeprefix("gs://")
    bkt, _, name = path.partition("/")
    req = urllib.request.Request(
        GCS_OBJECT.format(bucket=bkt, name=urllib.parse.quote(name, safe="")), method="DELETE")
    req.add_header("Authorization", f"Bearer {lake._token()}")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


# --------------------------------------------------------------------------- #
# AI（Vertex AI Gemini に録画を読ませる）
# --------------------------------------------------------------------------- #
def _post(url: str, body: dict, timeout: int) -> dict:
    """generateContent を呼ぶ（テストでモックする境界）。録画を読むので待ち時間を長くとる。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {lake._token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = " ".join(exc.read().decode("utf-8", "replace").split())[:300]
        raise RuntimeError(f"Gemini API {exc.code}: {detail}") from exc


def _video_part(gcs_uri: str, start: int, end: int, fps: float) -> dict:
    return {"fileData": {"fileUri": gcs_uri, "mimeType": VIDEO_MIME},
            "videoMetadata": {"startOffset": f"{start}s", "endOffset": f"{end}s", "fps": fps}}


def generate(task: str, parts: list[dict], *, resolution: str | None = None,
             thinking_budget: int | None = None, timeout: int = 540) -> tuple[dict, InferenceUsage]:
    """録画・本文を渡して JSON を受け取る。"""
    model, project, location = llm._target(task)
    body: dict = {"contents": [{"role": "user", "parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json"}}
    if resolution:
        body["generationConfig"]["mediaResolution"] = resolution
    if thinking_budget is not None:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{location}/publishers/google/models/{model}:generateContent")
    res = _post(url, body, timeout)
    out = ((res.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in out if not p.get("thought"))
    if not text:
        raise RuntimeError(f"Gemini から本文が返りませんでした: {json.dumps(res)[:300]}")
    meta = res.get("usageMetadata") or {}
    usage = InferenceUsage(
        model=model, input_tokens=int(meta.get("promptTokenCount", 0) or 0),
        output_tokens=int(meta.get("candidatesTokenCount", 0) or 0)
        + int(meta.get("thoughtsTokenCount", 0) or 0))
    return _json(text), usage


def _json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    data = json.loads(cleaned)
    return data if isinstance(data, dict) else {"items": data}


# --------------------------------------------------------------------------- #
# 材料（§5.1）
# --------------------------------------------------------------------------- #
def attendees(repo: Repository, event: Event) -> tuple[list[Member], list[Member]]:
    """出席（予定）の人と欠席の人。出欠の記録がなければ出席予定に入れる。"""
    targets = resolve_targets(repo, event, require_line=False)
    status = {a.member_id: a.status for a in repo.list_attendances(event.event_id)}
    absent_marks = (AttendanceStatus.欠席, AttendanceStatus.委任)
    present = [m for m in targets if status.get(m.member_id) not in absent_marks]
    absent = [m for m in targets if status.get(m.member_id) in absent_marks]
    return present, absent


def _role(m: Member) -> str:
    return m.officer_role or (f"{m.committee}{m.committee_role}" if m.committee_role else "")


def glossary(repo: Repository) -> list[str]:
    """用語集（設定のもの＋委員会名）。LOM ごとの値はコードに書かない。"""
    words = list(repo.get_settings().minutes.glossary)
    for m in repo.list_members():
        words.extend(m.committee_names)
    return sorted({w.strip() for w in words if w and w.strip()})


def materials(repo: Repository, event: Event) -> str:
    present, absent = attendees(repo, event)

    def line(m: Member) -> str:
        bits = [f"{m.member_id}: {m.name}"]
        if m.kana:
            bits.append(f"読み {m.kana}")
        if _role(m):
            bits.append(_role(m))
        if m.nicknames:
            bits.append("呼び名 " + "・".join(m.nicknames))
        return "- " + "／".join(bits)

    names = {m.member_id: m.name for m in repo.list_members()}
    proposals = [f"- {p.proposal_id}: {p.number or ''} {p.title}"
                 + (f"（担当 {names.get(p.owner_member_id, '')}・{p.committee or ''}）"
                    if p.owner_member_id else "")
                 for p in proposals_for(repo, event)]
    return "\n".join([
        f"# 会議\n{event.title}（{event.datetime_start:%Y-%m-%d %H:%M}〜）",
        "# 出席予定者（話者はこの中から会員IDで選ぶ）", *[line(m) for m in present],
        "# 欠席者（話者にはならない）", *[line(m) for m in absent],
        "# 議案（proposal は議案IDで書く）", *(proposals or ["- （登録なし）"]),
        "# 用語集（聞き違いをこの表記に直す）", "、".join(glossary(repo)) or "（なし）",
    ])


_TRANSCRIBE_PROMPT = """あなたは青年会議所の会議の書記です。
オンライン会議（Google Meet）の録画のうち、{start}〜{end} の部分を、
音声と画面から書き起こしてください。
画面は数秒おきの静止画です。共有画面の資料（ファイル名・シート名・行）と、
話している人の枠を見てください。

{materials}

# 書き方
- 発言ごとに区切り、start・end は録画の先頭からの時刻（HH:MM:SS）。
- speaker は出席予定者の会員ID。声・呼ばれ方（呼び名）・画面の発言中の枠から判断する。
  自信がなければ null にし、unclear に入れる（推測で名前を書かない）。
  一人のアカウントで複数人が入っていることがあるので、表示名より声と呼ばれ方を優先する。
- speaker_by は判断の根拠: "voice"（声）| "called"（呼ばれ方）| "screen"（画面）。
- 「えー」「あの」は省くが、意見の中身は省略しない。聞き取れない語は（不明）と書く。
- proposal は話している議案の議案ID（わからなければ null）。共有画面の切り替わりも手がかりにする。
- screen は画面に映っている資料（例「予算書（案） 80行目付近」）。なければ null。
- 「この行」「画面のここ」など画面を指す発言や、聞き取れない名前・語は unclear に入れ、
  画面を細かく見れば確かめられる具体的な問いにする。

# 出力（JSONのみ）
{{"segments": [{{"start": "HH:MM:SS", "end": "HH:MM:SS", "speaker": "会員ID" | null,
  "speaker_by": "voice" | "called" | "screen", "text": "…", "proposal": "議案ID" | null,
  "screen": "…" | null}}],
 "unclear": [{{"at": "HH:MM:SS", "kind": "speaker" | "screen" | "word",
  "question": "確かめたいこと", "context": "前後の発言の要約"}}]}}
"""


_VERIFY_PROMPT = """あなたは青年会議所の会議の書記です。書き起こしで音声だけではわからなかった所を、
録画の画面を細かく見て確かめてください。動画は問いごとに、その時刻の前後の部分です。

{materials}

# 問い
{questions}

# 答え方
- 話者: Meet の発言中の枠（光っている枠）・表示名・口の動きから。表示名が共有のアカウントなら
  声と呼ばれ方を優先する。答えは出席予定者の会員ID。
- 画面: 共有画面の資料の名前・シート・行番号・セルの値を読む。
- 語: 画面に出ている文字（資料・チャット・表示名）に当たるものがあれば、その表記。
- 根拠（evidence）を必ず書く（例「画面: 予算書（案）80行目に『2023』」「発言中の枠: 〇〇」）。
  画面で確かめられなければ resolved を false にする（推測で答えない）。

# 出力（JSONのみ）
{{"answers": [{{"index": 問いの番号, "resolved": true | false, "answer": "答え",
  "speaker": "会員ID" | null, "replace_from": "書き起こしで直す語" | null,
  "replace_to": "直した語" | null, "evidence": "根拠"}}]}}
"""


_DRAFT_PROMPT = """あなたは青年会議所の会議の書記です。
会議の書き起こしから、議事録の案を作ってください。

{materials}

# 書き起こし（[時刻] 話者: 発言）
{transcript}

# まとめ方
- 議案ごとに sections を作る（議案IDがわかれば proposal に入れる）。
  議案以外の議題（報告・連絡など）も一つの section にしてよい。会議の流れの順に並べる。
- explained: 説明の要点。opinions: 意見（by は意見を言った人の会員ID、target は意見の箇所、
  point は意見、response はそれへの答え、response_by は答えた人）。decided: 決まったこと。
  action_items: 宿題（owner は担当の会員ID、what は内容、due は期限「次の理事会まで」など）。
  for_absent: 欠席者に伝えたい一言（なければ null）。
- 文は「です・ます」でなく議事録の体（「〜とする」「〜との意見」）。
- 話者が null の発言は、人を推測せずに内容だけまとめる（by は null）。
- 人事・出向の人選・入会候補者の名前・個人の事情・個人の評価・個人のお金の話は sections に書かず、
  sensitive に入れる（kind は personnel | candidate | private | money）。
- confirmations: 次回の会議・締切など、全員が確かめておくこと。
- 書き起こしにないことを足さない。

# 出力（JSONのみ）
{{"sections": [{{"proposal": "議案ID" | null, "title": "…", "explained": ["…"],
  "opinions": [{{"by": "会員ID" | null, "target": "…" | null, "point": "…", "response": "…" | null,
  "response_by": "会員ID" | null}}], "decided": ["…"],
  "action_items": [{{"owner": "会員ID" | null, "what": "…", "due": "…" | null}}],
  "for_absent": "…" | null}}],
 "confirmations": ["…"],
 "sensitive": [{{"at": "HH:MM:SS" | null, "kind": "…", "summary": "…"}}]}}
"""


def _member_ids(repo: Repository, event: Event) -> set[str]:
    present, absent = attendees(repo, event)
    return {m.member_id for m in present + absent}


def _clean_member(value, allowed: set[str]) -> str | None:
    """AI が返した会員ID。出席予定者でなければ null（名前を作らない）。"""
    return value if isinstance(value, str) and value in allowed else None


def transcript_text(repo: Repository, minutes: MeetingMinutes) -> str:
    names = {m.member_id: m.name for m in repo.list_members()}
    return "\n".join(
        f"[{s.start}] {names.get(s.speaker or '', '（話者不明）')}: {s.text}"
        for s in minutes.segments)


# --------------------------------------------------------------------------- #
# 各段（1回に1段ずつ）
# --------------------------------------------------------------------------- #
def _step_copy(repo: Repository, minutes: MeetingMinutes, event: Event, now: datetime) -> None:
    meta = _drive_get(f"{DRIVE_FILES}/{minutes.recording_file_id}"
                      f"?supportsAllDrives=true&fields={_FIELDS}")
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise RuntimeError("録画の大きさがわかりません")
    object_name = f"minutes/{event.event_id}/{minutes.run_id}.mp4"
    minutes.gcs_uri = _copy_to_gcs(minutes.recording_file_id, size, object_name)
    minutes.duration_sec = minutes.duration_sec or _duration(meta)
    if not minutes.duration_sec:
        raise RuntimeError("録画の長さがわかりません（ドライブで動画の処理が終わっていない可能性）")
    minutes.status = MinutesStatus.transcribing
    minutes.chunk = 0


def _shift(value: str | None, offset: int) -> str | None:
    sec = to_sec(value)
    return hms(sec + offset) if sec is not None else value


def _step_transcribe(repo: Repository, minutes: MeetingMinutes, event: Event,
                     now: datetime) -> None:
    ranges = chunks(minutes.duration_sec or 0)
    start, end = ranges[minutes.chunk]
    prompt = _TRANSCRIBE_PROMPT.format(start=hms(start), end=hms(end),
                                       materials=materials(repo, event))
    data, usage = generate("minutes_transcribe",
                           [_video_part(minutes.gcs_uri, start, end, SPARSE_FPS),
                            {"text": prompt}],
                           resolution="MEDIA_RESOLUTION_LOW", thinking_budget=128,
                           # 1区切りは通常 1〜2 分。返事が止まったときは早めに打ち切ってやり直す
                           timeout=300)
    record_inference(repo, kind="minutes_transcribe", usage=usage, now=now,
                     target=event.event_id)
    allowed = _member_ids(repo, event)
    raw = [s for s in data.get("segments") or [] if isinstance(s, dict) and s.get("text")]
    # 区切りの頭からの時刻で返ってきたら、録画の頭からの時刻に直す
    first = min((to_sec(s.get("start")) or 0 for s in raw), default=0)
    offset = start if start > 0 and first < start - 60 else 0
    last = to_sec(minutes.segments[-1].start) if minutes.segments else None
    keep_from = last if last is not None else -1
    for s in raw:
        speaker = _clean_member(s.get("speaker"), allowed)
        seg = MinutesSegment(
            start=_shift(s.get("start"), offset) or hms(start),
            end=_shift(s.get("end"), offset), text=str(s.get("text", "")).strip(),
            speaker=speaker, speaker_by=s.get("speaker_by") if speaker else None,
            proposal=s.get("proposal") or None, screen=s.get("screen") or None)
        # 前の区切りと重ねた部分は、前の区切りのものを残す
        if (to_sec(seg.start) or 0) > keep_from:
            minutes.segments.append(seg)
    for u in data.get("unclear") or []:
        if isinstance(u, dict) and u.get("at"):
            minutes.unclear.append(MinutesUnclear(
                unclear_id=uuid.uuid4().hex[:10], at=_shift(u.get("at"), offset) or hms(start),
                kind=u.get("kind") or "word", question=str(u.get("question", "")),
                context=str(u.get("context", ""))))
    minutes.chunk += 1
    if minutes.chunk >= len(ranges):
        minutes.status = MinutesStatus.verifying


def _segment_at(minutes: MeetingMinutes, at: str) -> MinutesSegment | None:
    sec = to_sec(at) or 0
    before = [s for s in minutes.segments if (to_sec(s.start) or 0) <= sec]
    return before[-1] if before else (minutes.segments[0] if minutes.segments else None)


def _step_verify(repo: Repository, minutes: MeetingMinutes, event: Event, now: datetime) -> None:
    limit = repo.get_settings().minutes.verify_limit
    done = sum(1 for u in minutes.unclear if u.status != "open")
    todo = [u for u in minutes.unclear if u.status == "open"
            and not u.answer][:max(min(VERIFY_BATCH, limit - done), 0)]
    if not todo:
        minutes.status = MinutesStatus.drafting
        return
    parts: list[dict] = []
    questions = []
    for i, u in enumerate(todo, start=1):
        sec = to_sec(u.at) or 0
        start = max(sec - VERIFY_WINDOW_SEC, 0)
        end = min(sec + VERIFY_WINDOW_SEC, minutes.duration_sec or sec + VERIFY_WINDOW_SEC)
        parts.append(_video_part(minutes.gcs_uri, start, end, 1))
        questions.append(f"{i}. （動画{i}・{hms(start)}〜{hms(end)}）{u.question}\n"
                         f"   前後: {u.context}")
    prompt = _VERIFY_PROMPT.format(materials=materials(repo, event),
                                   questions="\n".join(questions))
    # HIGH は静止画1枚のときだけ使える（動画では 400 になる）ので、動画で使える中で細かい MEDIUM
    data, usage = generate("minutes_verify", [*parts, {"text": prompt}],
                           resolution="MEDIA_RESOLUTION_MEDIUM", thinking_budget=1024)
    record_inference(repo, kind="minutes_verify", usage=usage, now=now, target=event.event_id)
    allowed = _member_ids(repo, event)
    answers = {a.get("index"): a for a in data.get("answers") or [] if isinstance(a, dict)}
    for i, u in enumerate(todo, start=1):
        a = answers.get(i) or {}
        u.answer = str(a.get("answer") or "（確かめられませんでした）")
        u.evidence = a.get("evidence") or None
        if not (a.get("resolved") and u.evidence):
            u.status = "skipped"  # 根拠がなければ直さない。人が録画を見て直す
            continue
        seg = _segment_at(minutes, u.at)
        speaker = _clean_member(a.get("speaker"), allowed)
        if seg and u.kind == "speaker" and speaker:
            seg.speaker, seg.speaker_by, seg.evidence = speaker, "screen", u.evidence
        elif seg and a.get("replace_from") and a.get("replace_to") \
                and a["replace_from"] in seg.text:
            seg.text = seg.text.replace(a["replace_from"], a["replace_to"])
            seg.evidence = u.evidence
        elif seg and u.kind == "screen":
            seg.screen, seg.evidence = u.answer, u.evidence
        u.status = "verified"
    # 上限に達したら残りは不明点のまま
    if done + len(todo) >= limit:
        minutes.status = MinutesStatus.drafting


def _step_draft(repo: Repository, minutes: MeetingMinutes, event: Event, now: datetime) -> None:
    prompt = _DRAFT_PROMPT.format(materials=materials(repo, event),
                                  transcript=transcript_text(repo, minutes))
    data, usage = generate("minutes_draft", [{"text": prompt}], timeout=540)
    record_inference(repo, kind="minutes_draft", usage=usage, now=now, target=event.event_id)
    apply_draft(minutes, data, _member_ids(repo, event))
    if minutes.gcs_uri:
        _delete_gcs(minutes.gcs_uri)
        minutes.gcs_uri = None
    minutes.status = MinutesStatus.ready


def apply_draft(minutes: MeetingMinutes, data: dict, allowed: set[str]) -> None:
    """AI のまとめを議事録の案に入れる。会員IDは出席予定者だけ（名前を作らない）。"""
    sections = []
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        sections.append(MinutesSection(
            proposal=s.get("proposal") or None, title=str(s.get("title") or "（議題）"),
            explained=[str(x) for x in s.get("explained") or []],
            opinions=[MinutesOpinion(
                by=_clean_member(o.get("by"), allowed), target=o.get("target") or None,
                point=str(o.get("point") or ""), response=o.get("response") or None,
                response_by=_clean_member(o.get("response_by"), allowed))
                for o in s.get("opinions") or [] if isinstance(o, dict)],
            decided=[str(x) for x in s.get("decided") or []],
            action_items=[MinutesActionItem(
                owner=_clean_member(a.get("owner"), allowed), what=str(a.get("what") or ""),
                due=a.get("due") or None)
                for a in s.get("action_items") or [] if isinstance(a, dict)],
            for_absent=s.get("for_absent") or None))
    minutes.sections = sections
    minutes.confirmations = [str(x) for x in data.get("confirmations") or []]
    minutes.sensitive = [
        MinutesSensitive(sensitive_id=uuid.uuid4().hex[:10], at=x.get("at") or None,
                         kind=x.get("kind") or "private", summary=str(x.get("summary") or ""))
        for x in data.get("sensitive") or [] if isinstance(x, dict)]


STEPS = {
    MinutesStatus.queued: _step_copy,
    MinutesStatus.copying: _step_copy,
    MinutesStatus.transcribing: _step_transcribe,
    MinutesStatus.verifying: _step_verify,
    MinutesStatus.drafting: _step_draft,
}


# --------------------------------------------------------------------------- #
# 作業の開始と進め方（§5.8）
# --------------------------------------------------------------------------- #
def publish(minutes: MeetingMinutes) -> bool:
    """次の段の知らせを出す（テストでモックする境界）。"""
    topic = topic_path()
    if topic is None:
        return False
    try:
        lake._publish_raw(topic, [lake.to_pubsub_message(
            {"id": f"{minutes.event_id}:{minutes.run_id}:{minutes.seq}", "kind": "minutes",
             "payload": {"event_id": minutes.event_id, "run_id": minutes.run_id,
                         "seq": minutes.seq}})])
        return True
    except Exception:  # noqa: BLE001 - 知らせを出せなければ画面の［続きから］で出し直す
        logger.exception("議事録の作業の知らせを出せませんでした: %s", minutes.event_id)
        return False


def start(repo: Repository, event: Event, *, recording: str, actor: str,
          now: datetime) -> MeetingMinutes:
    """［議事録を作る］: 録画を確かめて作業を作り、ワーカーに知らせる。"""
    if not is_configured():
        raise MinutesError(503, "議事録を作る設定がまだありません（scripts/setup_minutes.sh）。")
    current = repo.get_minutes(event.event_id)
    if current and current.status in ACTIVE and current.updated_at \
            and (now - current.updated_at).total_seconds() < 30 * 60:
        raise MinutesError(409, "議事録を作っているところです。終わるまでお待ちください。")
    meta = find_recording(recording)
    minutes = MeetingMinutes(
        event_id=event.event_id, run_id=uuid.uuid4().hex[:12], seq=0,
        recording_file_id=meta["id"], recording_name=meta.get("name"),
        recording_folder_id=(meta.get("parents") or [None])[0],
        duration_sec=_duration(meta), requested_by=actor, created_at=now, updated_at=now,
        # 人が直した案・送った記録は、作り直しても残す（送った記録は監査のため）
        doc_id=current.doc_id if current else None,
        sends=current.sends if current else [])
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action="minutes.start", target=event.event_id,
                detail=meta.get("name") or meta["id"])
    if not publish(minutes):
        minutes.error = "作業を始められませんでした。［続きから］を押してください。"
        repo.save_minutes(minutes)
    return minutes


def resume(repo: Repository, event: Event, *, actor: str, now: datetime) -> MeetingMinutes:
    """止まった作業を、止まった段から続ける（知らせを出し直す）。"""
    minutes = repo.get_minutes(event.event_id)
    if minutes is None or minutes.status in (MinutesStatus.ready,):
        raise MinutesError(409, "続ける作業がありません。")
    if minutes.status == MinutesStatus.failed:
        minutes.status = MinutesStatus.copying if not minutes.gcs_uri else (
            MinutesStatus.transcribing if minutes.chunk < len(chunks(minutes.duration_sec or 0))
            else MinutesStatus.verifying)
    minutes.attempts, minutes.error = 0, None
    minutes.seq += 1
    minutes.updated_at = now
    repo.save_minutes(minutes)
    write_audit(repo, actor=actor, action="minutes.resume", target=event.event_id,
                detail=minutes.status.value)
    if not publish(minutes):
        raise MinutesError(503, "作業を始められませんでした。"
                                "しばらくしてからもう一度押してください。")
    return minutes


def _notify(repo: Repository, event: Event, text: str, now: datetime) -> None:
    """専務に知らせる（管理画面へのリンクだけ。チャットは検索のみの原則）。

    段階 A は専務が管理画面から始めるので、静音時間に送れなかった知らせは送り直さない。
    """
    url = lom.admin_url(f"/app/events?id={event.event_id}")
    try:
        notify_sed(repo, f"{text}\n\n{url}" if url else text, now, "minutes_notice")
    except Exception:  # noqa: BLE001 - 知らせの失敗で作業の記録を止めない
        logger.exception("議事録の知らせに失敗: %s", event.event_id)


def handle(repo: Repository, payload: dict, now: datetime) -> str:
    """ワーカー: 知らせ1件で1段だけ進め、次の段の知らせを出す。"""
    event_id = payload.get("event_id")
    minutes = repo.get_minutes(event_id) if event_id else None
    if minutes is None or minutes.run_id != payload.get("run_id") \
            or minutes.seq != payload.get("seq") or minutes.status not in STEPS:
        return "stale"
    if not repo.claim_once(f"minutes:{minutes.run_id}:{minutes.seq}"):
        return "duplicate"
    event = repo.get_event(event_id)
    if event is None:
        return "no_event"
    step = minutes.status
    try:
        if step == MinutesStatus.queued:
            minutes.status = MinutesStatus.copying
        STEPS[step](repo, minutes, event, now)
        minutes.attempts, minutes.error = 0, None
    except Exception as exc:  # noqa: BLE001 - 段ごとにやり直す
        logger.exception("議事録の作業に失敗: %s %s", event_id, step)
        minutes = repo.get_minutes(event_id) or minutes  # 途中の変更は捨てる
        minutes.attempts += 1
        minutes.error = f"{step.value}: {exc}"[:500]
        if minutes.attempts >= MAX_ATTEMPTS:
            minutes.status = MinutesStatus.failed
            minutes.updated_at = now
            repo.save_minutes(minutes)
            _notify(repo, event, f"【議事録】{event.title} の議事録を作れませんでした。\n"
                                 f"理由: {minutes.error}\n管理画面の議事録から［続きから］で"
                                 "やり直せます。", now)
            return "failed"
    minutes.seq += 1
    minutes.updated_at = now
    repo.save_minutes(minutes)
    if minutes.status in STEPS and not publish(minutes):
        minutes.error = "続きを始められませんでした。［続きから］を押してください。"
        repo.save_minutes(minutes)
    if minutes.status == MinutesStatus.ready:
        _notify(repo, event, f"【議事録の案】{event.title}\n確かめて直してから、"
                             "管理画面から保存・送信してください。", now)
    return minutes.status.value
