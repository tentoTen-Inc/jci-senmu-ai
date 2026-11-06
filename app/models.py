"""ドメインモデルと列挙型（docs/mvp-design.md §3 準拠）。

Firestore のドキュメントは pydantic モデルの ``model_dump(mode="json")`` で
シリアライズし、``model_validate`` で復元する。
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from . import lom

# --------------------------------------------------------------------------- #
# 列挙型
# --------------------------------------------------------------------------- #

def _default_lom_id() -> str:
    return lom.lom_id()

class AttendanceStatus(StrEnum):
    出席 = "出席"
    WEB出席 = "Web出席"
    欠席 = "欠席"
    委任 = "委任"
    未回答 = "未回答"


class LateLeave(StrEnum):
    全日程参加 = "全日程参加"
    遅刻 = "遅刻"
    早退 = "早退"
    遅刻早退 = "遅刻早退"


class AbsenceReason(StrEnum):
    体調不良 = "体調不良"
    家庭の事情 = "家庭の事情"
    仕事 = "仕事"
    冠婚葬祭 = "冠婚葬祭"
    その他 = "その他"


class MemberType(StrEnum):
    regular = "regular"  # 正会員
    external_auditor = "external_auditor"  # 外部監事
    office = "office"  # 事務局
    ob = "ob"  # OB
    support = "support"  # 賛助会員
    youth_support = "youth_support"  # 青年賛助会員（委員会に所属して活動する。配信対象）


class MemberStatus(StrEnum):
    active = "active"
    inactive = "inactive"


# 配信対象に含める会員区分（OB・賛助は除外）
_DELIVERABLE_TYPES = (
    MemberType.regular, MemberType.external_auditor, MemberType.office, MemberType.youth_support,
)


class EventType(StrEnum):
    例会 = "例会"
    理事会 = "理事会"
    五役会 = "五役会"
    委員会 = "委員会"
    総会 = "総会"
    行事 = "行事"  # 旧「イベント」（カレンダーの【イベント】も行事として読む）
    対外 = "対外"  # 福島ブロック・他LOM など外部の会議
    個別 = "個別"  # 個人の打合せ等（カレンダーからは取り込まない）

    @classmethod
    def _missing_(cls, value):
        # 改名前に保存した「イベント」を行事として読む（2026-10-12 本番で一覧が 500 になった）
        return cls.行事 if value == "イベント" else None


class EventStatus(StrEnum):
    draft = "draft"
    open = "open"
    closed = "closed"
    cancelled = "cancelled"  # 中止（出欠データは保持・再開可, docs/calendar-design.md §3.2）


class EventOrigin(StrEnum):
    """イベントがどこで作られたか（docs/calendar-design.md §3.1）。"""

    system = "system"  # 管理画面・API
    gcal = "gcal"  # Googleカレンダーから取込
    line = "line"  # LINE の自然文（役員）


class GcalSyncState(StrEnum):
    """Googleカレンダーへの反映状態（docs/calendar-design.md §3.1）。"""

    synced = "synced"
    pending = "pending"  # 未反映（tick で再試行）
    error = "error"  # 反映に失敗（tick で再試行・理由は gcal_error）
    disabled = "disabled"  # 連携が無効（GCAL_CALENDAR_ID 未設定）


class TargetScopeKind(StrEnum):
    all = "all"
    committee = "committee"
    officer = "officer"
    custom = "custom"


class DeliveryStatus(StrEnum):
    queued = "queued"
    sent = "sent"
    halted = "halted"


class DeliveryResult(StrEnum):
    ok = "ok"
    blocked = "blocked"
    failed = "failed"


# --------------------------------------------------------------------------- #
# サブモデル
# --------------------------------------------------------------------------- #
class Contact(BaseModel):
    mobile: str | None = None
    email: str | None = None
    home_tel: str | None = None
    home_address: str | None = None
    postal_code: str | None = None
    work: str | None = None
    work_title: str | None = None  # 勤務先での役職（JCの役職ではない）
    work_tel: str | None = None
    fax: str | None = None


class Secondment(BaseModel):
    org: str
    role: str


class CommitteeSeat(BaseModel):
    """委員会への所属（1人が複数の委員会に所属できる。2027年度の組織図で発生）。"""

    committee: str
    role: str | None = None  # 委員長 / 副委員長 / 委員


class TargetScope(BaseModel):
    kind: TargetScopeKind = TargetScopeKind.all
    value: list[str] = Field(default_factory=list)


class AttendanceHistory(BaseModel):
    at: datetime
    status: AttendanceStatus
    by: str = "self"


# --------------------------------------------------------------------------- #
# 主要エンティティ
# --------------------------------------------------------------------------- #
class Member(BaseModel):
    member_id: str
    lom_id: str = Field(default_factory=_default_lom_id)
    name: str
    kana: str | None = None
    birthday: str | None = None  # 生年月日（和暦表記をそのまま保持: 例 "H3.11.15"）
    member_type: MemberType = MemberType.regular
    status: MemberStatus = MemberStatus.active
    committee: str | None = None  # 主たる所属（既存の画面・CSV との互換用）
    committee_role: str | None = None  # 委員長/副委員長/委員
    committees: list[CommitteeSeat] = Field(default_factory=list)  # 兼務を含む全所属
    officer_role: str | None = None  # 理事長/専務理事/...
    #: 副理事長が担当する委員会（席ではないので委員会あての出欠依頼の対象には入らない）
    supervised_committees: list[str] = Field(default_factory=list)
    secondments: list[Secondment] = Field(default_factory=list)
    contact: Contact = Field(default_factory=Contact)
    line_user_id: str | None = None
    linked_at: datetime | None = None
    #: 会議で呼ばれる名前（名前の一部・あだ名）。議事録の話者の判定に使う
    #: （docs/meeting-minutes-design.md §5.1）。会員の取込・名簿の同期では上書きしない
    nicknames: list[str] = Field(default_factory=list)

    @property
    def committee_names(self) -> set[str]:
        """兼務を含む所属委員会の名前（主たる所属も含む）。"""
        names = {seat.committee for seat in self.committees}
        if self.committee:
            names.add(self.committee)
        return names

    @property
    def is_current(self) -> bool:
        """在籍していて、案内を送る会員区分（退会・OB等でない）か。LINE の連携は問わない。"""
        return self.status == MemberStatus.active and self.member_type in _DELIVERABLE_TYPES

    @property
    def is_deliverable(self) -> bool:
        """配信対象（有効・LINE紐付け済み・退会/OB等でない）か。"""
        return self.is_current and self.line_user_id is not None


class InviteCode(BaseModel):
    code: str
    member_id: str
    expires_at: datetime
    used_at: datetime | None = None


class LinkState(BaseModel):
    """LINEユーザー単位の本人確認試行状態（総当たり対策）。"""

    line_user_id: str
    failed_attempts: int = 0
    locked: bool = False


class Event(BaseModel):
    event_id: str
    type: EventType
    title: str
    datetime_start: datetime
    datetime_end: datetime | None = None
    location: str | None = None
    #: 所属する年度（JCの年度=暦年）。None は開催日の年と同じ。
    #: 前年中に開く翌年度の予定だけ入れる（カレンダーのタイトルに「27」等を前置）。
    #: docs/calendar-design.md §4.4
    fiscal_year: int | None = None
    target_scope: TargetScope = Field(default_factory=TargetScope)
    attendance_deadline: datetime | None = None
    material_deadline: datetime | None = None
    delivery_at: datetime | None = None
    reminder_policy_id: str | None = None
    quorum: int | None = None  # 定足数（人数）。理事会等で判定に使う（F4-5）
    status: EventStatus = EventStatus.draft
    origin: EventOrigin = EventOrigin.system
    updated_at: datetime | None = None  # システム側の最終更新（カレンダーとの後勝ち判定に使う）
    # --- Googleカレンダー連携（F3-2, docs/calendar-design.md §3.1）---
    gcal_event_id: str | None = None
    gcal_etag: str | None = None
    gcal_sync_state: GcalSyncState = GcalSyncState.pending
    gcal_synced_at: datetime | None = None
    gcal_error: str | None = None
    # --- Meet（すべての予定に付けて自動録画, docs/calendar-design.md §4.5）---
    meet_url: str | None = None
    meet_auto_record: bool = False  # 自動録画をオンにできたか


class LineGroup(BaseModel):
    """ボットが参加しているグループ・複数人トーク（docs/datalake-design.md §3.5）。"""

    group_id: str
    source_type: str = "group"  # group | room
    group_name: str | None = None
    picture_url: str | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None
    profile_at: datetime | None = None  # 最後にグループ名を取得した時刻


class CalendarSyncState(BaseModel):
    """カレンダー→システム差分取込の状態（シングルトン, docs/calendar-design.md §3.3）。"""

    sync_token: str | None = None  # events.list の nextSyncToken
    last_pulled_at: datetime | None = None
    last_error: str | None = None


class CalendarOpFields(BaseModel):
    """LINE で解釈した予定の中身（作成・変更する項目だけ埋まる）。"""

    type: EventType | None = None
    title: str | None = None
    datetime_start: datetime | None = None
    datetime_end: datetime | None = None
    location: str | None = None
    fiscal_year: int | None = None


class PendingCalendarOp(BaseModel):
    """LINE で確認待ちの予定操作（docs/calendar-design.md §3.4 / §5.2）。

    本人が「はい」を押したときだけ反映する。30分で失効する。
    """

    op_id: str
    member_id: str
    action: str  # create | update | cancel
    event_id: str | None = None
    fields: CalendarOpFields = Field(default_factory=CalendarOpFields)
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending | done | declined | expired


class DocSession(BaseModel):
    """資料の作業セッション（docs/doc-session-design.md §4）。1セッション＝1つの資料。"""

    session_id: str
    member_id: str
    doc_id: str
    doc_title: str
    committee: str
    status: str = "active"  # active | closed
    started_at: datetime
    last_active_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None  # user | switched | expired


class DocTurn(BaseModel):
    """作業セッションの1発言（本人／AI）。AI は直近の発言を文脈にする。"""

    at: datetime
    role: str  # user | assistant
    kind: str  # instruction | edit | ask | answer | applied | declined | reverted | conflict
    text: str
    op_id: str | None = None


class DocEdit(BaseModel):
    """作業セッションの書き換えの案と、書き込みの記録（変更前と変更後）。"""

    op_id: str
    session_id: str
    member_id: str
    doc_id: str
    doc_title: str
    committee: str
    kind: str = "edit"  # edit | revert（直前の書き込みを戻す）
    revert_of: str | None = None
    revision_id: str  # 案を作ったときに読んだ版
    after: dict[str, str] = Field(default_factory=dict)  # 項目名 → 新しい内容
    before: dict[str, str] = Field(default_factory=dict)  # 書き込んだときの元の内容
    summary: str | None = None
    status: str = "proposed"  # proposed | applied | declined | expired | reverted | conflict
    created_at: datetime
    expires_at: datetime
    applied_at: datetime | None = None


class PendingProposalCreate(BaseModel):
    """LINE で確認待ちの議案（事業計画書）の作成（docs/proposal-ledger-design.md §6）。"""

    op_id: str
    member_id: str
    committee: str
    event_id: str
    event_start: datetime
    title: str  # 「3月例会」
    folder_name: str  # 「2703_3月例会」
    doc_name: str  # 「【計画書】3月例会事業計画書」
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending | done | declined | expired


class ChatState(BaseModel):
    """LINE の個別トークの状態（docs/doc-session-design.md §4.2）。

    既定は質問（書き込みなし）。リッチメニューのボタンで始めた作業の間だけ
    `calendar` / `doc` になる。
    """

    member_id: str
    mode: str = "question"  # question | calendar | doc
    session_id: str | None = None  # mode=doc のときの作業セッション
    expires_at: datetime | None = None
    updated_at: datetime | None = None


class Attendance(BaseModel):
    event_id: str
    member_id: str
    status: AttendanceStatus = AttendanceStatus.未回答
    proxy_member_id: str | None = None  # 委任先の会員（委任状・代理出席, F4-5）
    late_leave: LateLeave | None = None
    absence_reasons: list[AbsenceReason] = Field(default_factory=list)
    free_text: str | None = None
    responded_at: datetime | None = None
    history: list[AttendanceHistory] = Field(default_factory=list)

    @property
    def doc_id(self) -> str:
        return f"{self.event_id}_{self.member_id}"


class ReminderStage(BaseModel):
    name: str  # 依頼/リマインド/最終 等
    # 締切からのオフセット（分）。負の値=締切前。
    offset_minutes: int
    audience: str = "unanswered"  # all | unanswered | attendees
    template: str = "remind"


class ReminderPolicy(BaseModel):
    policy_id: str
    event_type: EventType
    stages: list[ReminderStage] = Field(default_factory=list)


class QuietHours(BaseModel):
    start: str = "21:00"
    end: str = "08:00"
    tz: str = "Asia/Tokyo"


class RateLimit(BaseModel):
    per_member_per_day: int = 3
    global_per_min: int = 30


class MeetingNoticeSetting(BaseModel):
    """会議体ごとの案内メールの設定（docs/meeting-notice-design.md §6）。"""

    audience: str = ""  # 宛名（例: 理事各位）
    sender_role: str = ""  # 差出人の役職。会員の役職から名前を引く（例: 理事長）
    sender_title: str = ""  # 文中の肩書（空なら sender_role）
    to_mode: str = "bcc"  # bcc（既定。To は LOM のアドレス）| to（To に並べる）
    cc_roles: list[str] = Field(default_factory=list)  # CC に入れる役職（例: 専務理事）


class DocNumberSetting(BaseModel):
    """文書番号（§6.3）。番号は下書きを作るときに決め、同じ会議では使い回す。"""

    enabled: bool = False
    format: str = "{prefix}第{term}-{seq}号"
    prefix: str = ""
    term: str = ""
    digits: int = 3  # 連番の桁（0 で埋める）
    #: 年度ごとの最初の番号（手で付けた番号の続きから始めるとき）。年度 → 番号
    start_seq: dict[str, int] = Field(default_factory=dict)


class EmailStyle(BaseModel):
    font_family: str = "Arial"
    font_size: int = 11  # pt


class MinutesSetting(BaseModel):
    """議事録の設定（docs/meeting-minutes-design.md §9）。"""

    #: 書き起こしの後に、わからない所を録画の画面で確かめるか。費用が書き起こしと同じくらい
    #: かかるので既定は行わない（わからない所は「不明点」に残し、人が直す）
    verify_screen: bool = False
    #: 画面で確かめる箇所の上限（1回の会議）。超えたぶんは「不明点」に残す
    verify_limit: int = 40
    #: 用語集（委員会名・行事名・地名など）。台帳の委員会・会議は自動で足す
    glossary: list[str] = Field(default_factory=list)
    #: 議事録のドキュメントの置き場所（LOM のドライブのフォルダID）。空なら録画と同じフォルダ
    folder_id: str | None = None


class Settings(BaseModel):
    kill_switch: bool = False
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    #: 運用開始日（年度の始まり）。これより前のイベントは管理画面の一覧に既定で出さず、
    #: カレンダーからも取り込まない（データは消さない）。2027年度は 2027-01-01。
    operation_start: date | None = None
    #: 立場ごとのリッチメニューの ID（member / board / officer。scripts/setup_rich_menu.py が登録）
    rich_menus: dict[str, str] = Field(default_factory=dict)
    #: 日程調整などの既定の案内先にする LINE グループの ID（goyaku=五役 / riji=理事会 /
    #: all=会員全員）。年度ごとにグループが替わるので設定に置く（管理画面で一覧から選ぶ）
    line_groups: dict[str, str] = Field(default_factory=dict)
    #: 会議体（会議の種類）ごとの案内メールの宛名・差出人・宛先の入れ方
    #: （docs/meeting-notice-design.md §6）
    meeting_notices: dict[str, MeetingNoticeSetting] = Field(default_factory=dict)
    #: 案内文の文書番号（§6.3）。使わない LOM は enabled=False のまま
    doc_number: DocNumberSetting = Field(default_factory=lambda: DocNumberSetting())
    #: 案内メールの文字（§4.3）
    email_style: EmailStyle = Field(default_factory=lambda: EmailStyle())
    #: 議事録（docs/meeting-minutes-design.md §9）
    minutes: MinutesSetting = Field(default_factory=lambda: MinutesSetting())


class DeliveryJob(BaseModel):
    job_id: str
    type: str  # attendance_request | reminder | summary
    event_id: str | None = None
    stage: str | None = None
    targets: list[str] = Field(default_factory=list)
    template_id: str | None = None
    scheduled_at: datetime | None = None
    status: DeliveryStatus = DeliveryStatus.queued
    idempotency_key: str | None = None


class DeliveryLog(BaseModel):
    log_id: str
    job_id: str
    member_id: str
    content: str
    result: DeliveryResult
    reason: str | None = None
    sent_at: datetime


class Escalation(BaseModel):
    """メンバーからの事務局への取次依頼（docs/mvp-design.md §4.3 F8-3）。"""

    escalation_id: str
    member_id: str
    kind: str = "contact"  # contact | question | other
    text: str | None = None
    created_at: datetime
    status: str = "open"  # open | handled


class AuditLog(BaseModel):
    """操作・自動配信・設定変更の監査証跡（docs/dashboard-design.md §4.2）。"""

    audit_id: str
    at: datetime
    actor: str  # email or "system"
    action: str
    target: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- #
# 対外連絡（docs/external-notice-design.md §3, F5）
# --------------------------------------------------------------------------- #
class NoticeAttachment(BaseModel):
    name: str
    mime: str | None = None
    storage_uri: str | None = None
    text_excerpt: str | None = None  # PDF等から抽出した本文抜粋


class NoticeDigest(BaseModel):
    """Gemini による生成物。原文（body_text）とは切り分けて保持する（F5-6）。"""

    summary: str
    announcement: str  # メンバー向け告知文（LINE配信用）
    audience_hint: str | None = None  # 「全員」「総務委員会」「出向者」等の推定対象
    deadline: datetime | None = None
    actions: list[str] = Field(default_factory=list)
    model: str | None = None
    generated_at: datetime | None = None


class NoticeDelivery(BaseModel):
    """配信結果（P3-3 で記録）。"""

    job_id: str
    delivered_at: datetime
    target_count: int = 0


class NoticeHistory(BaseModel):
    at: datetime
    action: str
    by: str = "system"


class ExternalNotice(BaseModel):
    """ブロック協議会等からの対外連絡（docs/external-notice-design.md §3.1）。"""

    notice_id: str
    lom_id: str = Field(default_factory=_default_lom_id)
    source: str = "manual"  # gmail | manual
    source_ref: str | None = None  # Gmail message id / 本文ハッシュ（冪等キー）
    received_at: datetime
    from_addr: str | None = None
    from_name: str | None = None
    subject: str
    body_text: str  # 原文（不変）
    attachments: list[NoticeAttachment] = Field(default_factory=list)
    digest: NoticeDigest | None = None
    status: str = "new"  # new | reviewed | delivered | archived
    delivery: NoticeDelivery | None = None
    history: list[NoticeHistory] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 例会アンケート（docs/survey-design.md §3, F7）
# --------------------------------------------------------------------------- #
class SurveyQuestion(BaseModel):
    question_id: str
    title: str
    type: str  # scale | text | choice
    section: str | None = None  # 直前のセクション見出し
    scale_low: int | None = None
    scale_high: int | None = None
    options: list[str] = Field(default_factory=list)


class SurveyResponse(BaseModel):
    response_id: str
    submitted_at: datetime | None = None
    respondent_email: str | None = None
    respondent_name: str | None = None  # 氏名設問の値
    member_id: str | None = None  # 会員照合の結果（P4-2）
    answers: dict[str, str] = Field(default_factory=dict)  # question_id -> 値


class SurveyTheme(BaseModel):
    label: str
    count: int = 0
    examples: list[str] = Field(default_factory=list)


class SurveyDigest(BaseModel):
    """自由記述の Gemini 生成物（集計値とは分けて保持する）。"""

    summary: str
    themes: list[SurveyTheme] = Field(default_factory=list)
    sentiment: dict[str, int] = Field(default_factory=dict)  # positive/neutral/negative の件数
    improvements: list[str] = Field(default_factory=list)
    model: str | None = None
    generated_at: datetime | None = None


class Survey(BaseModel):
    """Googleフォームのアンケート1回分（docs/survey-design.md §3.1）。"""

    survey_id: str
    lom_id: str = Field(default_factory=_default_lom_id)
    form_id: str
    title: str
    kind: str = "internal"  # internal(対内) | external(対外)
    event_id: str | None = None
    questions: list[SurveyQuestion] = Field(default_factory=list)
    responses: list[SurveyResponse] = Field(default_factory=list)
    digest: SurveyDigest | None = None
    synced_at: datetime | None = None
    reminder_count: int = 0
    reminded_at: datetime | None = None


class ConversationTurn(BaseModel):
    at: datetime
    role: str  # user | assistant
    text: str


class Conversation(BaseModel):
    """会員との直近のやり取り（docs/nl-assistant-design.md §4, F8-5）。

    直近数往復のみ保持する（長期記憶は持たない）。
    """

    member_id: str
    turns: list[ConversationTurn] = Field(default_factory=list)
    updated_at: datetime | None = None


class NoticeAction(BaseModel):
    """対外連絡から起票した「対象者がやること」（F5-3/F5-5）。

    対応状況は会員単位で追跡する（`done_by`）。未対応者だけに催促を送る。
    """

    action_id: str
    notice_id: str
    title: str
    due: datetime | None = None
    assignees: list[str] = Field(default_factory=list)  # member_id
    done_by: list[str] = Field(default_factory=list)  # 対応済みの member_id
    created_at: datetime
    status: str = "open"  # open | closed
    reminder_count: int = 0
    reminded_at: datetime | None = None

    @property
    def pending(self) -> list[str]:
        return [m for m in self.assignees if m not in self.done_by]


class InferenceUsage(BaseModel):
    """LLM 1回の呼び出しで消費したトークン（プロバイダ応答から取得）。"""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class InferenceLog(BaseModel):
    """LLM推論ログ。月間コストKPIの元データ（docs/dashboard-design.md §6）。"""

    log_id: str
    at: datetime
    kind: str  # proposal_review | ...（用途）
    model: str
    target: str | None = None  # proposal_id 等
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None


# --------------------------------------------------------------------------- #
# 議案ライフサイクル（docs/dashboard-design.md §4.1, F6）
# --------------------------------------------------------------------------- #
class ProposalStage(StrEnum):
    entry = "entry"  # エントリー
    submitted = "submitted"  # 資料提出（専務の確認待ちを兼ねる）
    goyaku = "goyaku"  # 五役会
    board = "board"  # 理事会上程
    decided = "decided"  # 審議結果
    executing = "executing"  # 事業実施
    reported = "reported"  # 報告
    verified = "verified"  # 検証

    @classmethod
    def _missing_(cls, value: object) -> ProposalStage | None:
        # 旧「専務レビュー」（2026-10-31 廃止）は資料提出として読む
        return cls.submitted if value == "sed_review" else None


# カンバンの表示順
PROPOSAL_STAGE_ORDER = [
    ProposalStage.entry,
    ProposalStage.submitted,
    ProposalStage.goyaku,
    ProposalStage.board,
    ProposalStage.decided,
    ProposalStage.executing,
    ProposalStage.reported,
    ProposalStage.verified,
]


class ProposalDeadlines(BaseModel):
    entry: datetime | None = None
    submit: datetime | None = None
    deliver: datetime | None = None


class FormatCheckResult(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)  # 委員会で直す点
    checked_at: datetime | None = None
    #: 専務AI が文書を直したこと（フォント・半角など体裁だけ。docs/proposal-ledger-design.md §11）
    fixed: list[str] = Field(default_factory=list)
    fixed_at: datetime | None = None
    #: 直したことを委員長に知らせた時刻と相手（静音時間なら、明けてから tick で送る）
    notified_at: datetime | None = None
    notified_to: list[str] = Field(default_factory=list)


class LlmReview(BaseModel):
    summary: str
    points: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    reviewed_at: datetime | None = None
    model: str | None = None


class SedApproval(BaseModel):
    status: str = "pending"  # pending | approved | returned
    by: str | None = None
    at: datetime | None = None
    comment: str | None = None


class ProposalHistory(BaseModel):
    at: datetime
    stage: ProposalStage
    by: str = "system"


class SubmitPlan(BaseModel):
    """提出締切を過ぎた議案の提出予定日（担当副理事長などが LINE のボタンで答えた）。"""

    date: date
    member_id: str
    by: str  # 答えた人の氏名
    at: datetime
    notified_at: datetime | None = None  # 専務に「【提出予定】」を知らせた時刻


class ProposalAlert(BaseModel):
    """未提出の議案について担当副理事長に送った知らせ（届かなければ ok=False）。"""

    key: str  # "overdue:<締切>" / "plan:<予定日>"
    at: datetime
    member_id: str | None = None
    name: str | None = None
    ok: bool = True


class ProposalFeedback(BaseModel):
    """事業計画書の「審議の記録」の1件（docs/proposal-ledger-design.md §2）。"""

    meeting: str  # 五役会議① / 理事会① など
    kind: str  # 意見 / 対応
    no: int
    person: str | None = None  # 質問者/回答者
    text: str


class ProposalSource(BaseModel):
    """資料フォルダから読んだファイル（docs/folder-proposal-check-design.md）。"""

    name: str
    url: str


class Proposal(BaseModel):
    proposal_id: str
    lom_id: str = Field(default_factory=_default_lom_id)
    title: str
    number: str | None = None  # 第N号議案
    committee: str | None = None
    owner_member_id: str | None = None
    event_id: str | None = None  # 上程先の会議体
    #: 上程先の会議が日程調整中のとき、その日程調整（決まれば poll.event_id の会議になる）
    meeting_poll_id: str | None = None
    stage: ProposalStage = ProposalStage.entry
    doc_type: str = "事業計画書"
    content: str | None = None  # 議案本文（Drive取込/手入力）
    storage_uri: str | None = None
    deadlines: ProposalDeadlines = Field(default_factory=ProposalDeadlines)
    format_check: FormatCheckResult | None = None
    llm_review: LlmReview | None = None
    sed_approval: SedApproval = Field(default_factory=SedApproval)
    history: list[ProposalHistory] = Field(default_factory=list)
    status: str = "open"  # open | closed
    # --- 作業文書（Google ドキュメント）から同期した内容（docs/proposal-ledger-design.md §4） ---
    fiscal_year: int | None = None
    doc_id: str | None = None
    doc_modified_at: str | None = None  # 最後に読み取ったときの文書の更新日時（ドライブの値）
    synced_at: datetime | None = None
    basics: dict[str, str] = Field(default_factory=dict)  # 基本情報（委員会名・委員長など）
    sections: dict[str, str] = Field(default_factory=dict)  # 事業要項の各項目
    feedback: list[ProposalFeedback] = Field(default_factory=list)  # 審議の記録
    event_date: str | None = None  # 実施日（YYYY-MM-DD）
    venue: str | None = None
    participants: str | None = None
    #: 専務に送った知らせ（キー → 送った時刻）。同じ知らせを二度送らない
    #: （"submitted:<提出の時刻>"・"deadline:<提出締切>"。docs/proposal-ledger-design.md §10）
    notices: dict[str, datetime] = Field(default_factory=dict)
    #: 提出締切を設定・変更した時刻。これより前に過ぎていた締切では未提出の知らせを送らない
    submit_deadline_set_at: datetime | None = None
    submit_plans: list[SubmitPlan] = Field(default_factory=list)  # 提出予定日（新しいものが最後）
    alerts: list[ProposalAlert] = Field(default_factory=list)  # 担当副理事長への未提出の知らせ
    # --- 資料フォルダ（storage_uri）から読んだ内容（作業文書を持たない予算書など） ---
    sources: list[ProposalSource] = Field(default_factory=list)
    source_error: str | None = None  # 読めなかった理由
    sources_read_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 日程調整（docs/schedule-poll-design.md）
# --------------------------------------------------------------------------- #
class PollAnswer(StrEnum):
    ok = "ok"  # ○
    maybe = "maybe"  # △
    ng = "ng"  # ×


class PollStatus(StrEnum):
    open = "open"
    closed = "closed"  # 締切（決定はしていない）
    decided = "decided"


class PollCandidate(BaseModel):
    candidate_id: str
    start: datetime
    end: datetime | None = None


class SchedulePoll(BaseModel):
    poll_id: str
    title: str
    event_type: EventType
    fiscal_year: int | None = None  # 決定時に作るイベントの年度（カレンダーの「27」前置）
    location: str | None = None
    candidates: list[PollCandidate] = Field(default_factory=list)
    target_scope: TargetScope = Field(default_factory=TargetScope)  # 回答してほしい人
    audience: str | None = None  # 案内文の宛名（「五役の皆さま」等）
    #: 案内・催促・決定を送る LINE グループ。None なら連携済みの対象者に個別に送る
    #: （委員会あてで委員会のグループが無いとき。docs/schedule-poll-design.md §9）
    group_id: str | None = None
    deadline: datetime | None = None
    status: PollStatus = PollStatus.open
    decided_candidate_id: str | None = None
    event_id: str | None = None
    announced_at: datetime | None = None
    reminded_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str | None = None

    def candidate(self, candidate_id: str) -> PollCandidate | None:
        return next((c for c in self.candidates if c.candidate_id == candidate_id), None)


class PollResponse(BaseModel):
    poll_id: str
    line_user_id: str
    display_name: str | None = None  # LINE の表示名
    member_id: str | None = None  # 連携済みなら自動、未連携なら本人が選ぶ（自己申告）
    answers: dict[str, PollAnswer] = Field(default_factory=dict)  # candidate_id → ○△×
    comment: str | None = None
    updated_at: datetime | None = None

    @property
    def doc_id(self) -> str:
        return f"{self.poll_id}_{self.line_user_id}"


# --------------------------------------------------------------------------- #
# LINE 連携の申請（LIFF）と予約投稿（docs/line-link-design.md）
# --------------------------------------------------------------------------- #
class LinkRequestStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class LinkRequest(BaseModel):
    """LIFF で「私はこの会員です」と申請したもの。専務が承認すると Member.line_user_id に入る。"""

    line_user_id: str
    display_name: str | None = None  # LINE の表示名（承認時の確認用）
    member_id: str
    status: LinkRequestStatus = LinkRequestStatus.pending
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None


class ScheduledPostStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class ScheduledPost(BaseModel):
    """指定時刻にグループへ送る投稿（tick が送る。早朝・深夜に送らないため）。"""

    post_id: str
    group_id: str
    #: link_invite（LINE 連携のお願い） | poll_announce（日程調整の案内）
    #: | poll_remind（日程調整の催促） | poll_decide（日程調整の確定と決定の連絡）
    kind: str
    poll_id: str | None = None  # kind=poll_* のとき
    candidate_id: str | None = None  # kind=poll_decide のとき、確定する候補日
    send_at: datetime
    status: ScheduledPostStatus = ScheduledPostStatus.pending
    sent_at: datetime | None = None
    error: str | None = None
    created_at: datetime | None = None
    created_by: str | None = None


# --------------------------------------------------------------------------- #
# 意見箱（docs/feedback-box-design.md）
# --------------------------------------------------------------------------- #
class FeedbackKind(StrEnum):
    bug = "bug"  # 不具合
    feature = "feature"  # ほしい機能
    usability = "usability"  # 使いにくい点
    other = "other"  # その他
    unanswered = "unanswered"  # AI が答えられなかった質問（［意見箱に送る］）


class FeedbackStatus(StrEnum):
    new = "new"  # 新規
    in_progress = "in_progress"  # 対応中
    done = "done"  # 完了
    declined = "declined"  # 見送り


class FeedbackTriage(BaseModel):
    """AI による分類（送った人の選んだ種類は kind に残し、補正はここに持つ）。"""

    kind: FeedbackKind | None = None
    feature: str | None = None  # 対象の機能（出欠・日程調整・AIの回答 など）
    urgency: str | None = None  # 高 | 中 | 低
    summary: str | None = None  # 一行要約
    model: str | None = None
    classified_at: datetime | None = None


class Feedback(BaseModel):
    """意見箱に届いた1件。匿名なら member_id を保存しない。"""

    feedback_id: str
    kind: FeedbackKind
    text: str
    anonymous: bool = False
    member_id: str | None = None  # 匿名なら None（LINE の userId も保存しない）
    source: str = "liff"  # liff | chat（AI が答えられなかった質問）
    status: FeedbackStatus = FeedbackStatus.new
    triage: FeedbackTriage | None = None
    triage_failed_at: datetime | None = None  # 分類に失敗した時刻（次の tick で再試行）
    notified_at: datetime | None = None  # 専務理事に LINE で知らせた時刻
    created_at: datetime
    updated_at: datetime | None = None
    updated_by: str | None = None


class FeedbackTheme(BaseModel):
    label: str
    count: int = 0
    feedback_ids: list[str] = Field(default_factory=list)


class FeedbackDigest(BaseModel):
    """意見箱の傾向のまとめ（似た意見をまとめた論点と件数）。"""

    summary: str
    themes: list[FeedbackTheme] = Field(default_factory=list)
    count: int = 0  # まとめた意見の件数
    model: str | None = None
    generated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 通知テンプレートと会議の配信物（docs/meeting-notice-design.md）
# --------------------------------------------------------------------------- #
class NoticeTemplate(BaseModel):
    """LINE・メールの送信文のテンプレート（差し込み項目つき。§4）。"""

    template_id: str
    name: str
    channel: str = "line"  # line | email
    #: agenda（アジェンダ配信）| meeting_notice（開催案内）| correction（修正の再案内）
    purpose: str = "agenda"
    event_types: list[str] = Field(default_factory=list)  # 空なら全部の会議で使える
    subject: str = ""  # email のみ
    body: str = ""
    mention: str = "none"  # line のみ。none | all（全員へのメンション）
    is_default: bool = False  # その purpose・channel で最初に選ばれる
    builtin: bool = False  # リポジトリの既定のテンプレート（直すと台帳に保存される）
    archived: bool = False  # 使わなくなったもの（消さずに隠す）
    updated_at: datetime | None = None
    updated_by: str | None = None


class LineDispatch(BaseModel):
    """会議の LINE 投稿（準備した文面と送った記録）。"""

    template_id: str | None = None
    text: str | None = None  # 送った本文（差し込み後）
    group_id: str | None = None
    group_name: str | None = None
    recipients: int = 0  # 個別に送ったときの人数
    sent_at: datetime | None = None
    sent_by: str | None = None


class EmailDispatch(BaseModel):
    """会議の案内メール（段階 B は LOM の Gmail の下書きに作る）。"""

    template_id: str | None = None
    subject: str | None = None
    text: str | None = None  # 差し込み後の本文（レイアウト記法のまま）
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    fiscal_year: int | None = None
    doc_seq: int | None = None  # 使った文書番号の連番（同じ会議で作り直すときは使い回す）
    doc_number: str | None = None
    draft_id: str | None = None  # Gmail の下書き
    drafted_at: datetime | None = None
    drafted_by: str | None = None


class MeetingDispatch(BaseModel):
    """会議ごとの配信物（§8）。アジェンダ・意見書の URL（手で入れる）、LINE、案内メール。"""

    event_id: str
    agenda_url: str | None = None
    opinion_url: str | None = None
    line: LineDispatch = Field(default_factory=LineDispatch)
    email: EmailDispatch = Field(default_factory=EmailDispatch)
    history: list[LineDispatch] = Field(default_factory=list)  # 送った LINE（新しいものが最後）
    updated_at: datetime | None = None
    updated_by: str | None = None


# --------------------------------------------------------------------------- #
# 会議の議事録（docs/meeting-minutes-design.md）
# --------------------------------------------------------------------------- #
class MinutesStatus(StrEnum):
    queued = "queued"  # 作業を受け付けた
    copying = "copying"  # 録画を一時置き場に写している
    transcribing = "transcribing"  # 一次の読み取り（30分ずつ）
    verifying = "verifying"  # 画面での確認
    drafting = "drafting"  # まとめ
    ready = "ready"  # 案あり（人の確認待ち）
    failed = "failed"  # 3回失敗して止めた


class MinutesSegment(BaseModel):
    """書き起こしの一区切り（§5.3）。時刻は録画の頭からの "HH:MM:SS"。"""

    start: str
    end: str | None = None
    speaker: str | None = None  # 会員ID。わからなければ None（名前を推測で書かない）
    speaker_by: str | None = None  # voice | called | screen | human
    text: str = ""
    proposal: str | None = None  # 議案ID
    screen: str | None = None  # 画面に映っていたもの（資料名・行など）
    evidence: str | None = None  # 画面で確かめて直したときの根拠


class MinutesUnclear(BaseModel):
    """音声だけではわからなかった所（§5.3〜5.4）。"""

    unclear_id: str
    at: str
    kind: str = "speaker"  # speaker | screen | word
    question: str = ""
    context: str = ""
    status: str = "open"  # open | verified（画面で確かめた）| resolved（人が直した）| skipped
    answer: str | None = None
    evidence: str | None = None


class MinutesOpinion(BaseModel):
    by: str | None = None  # 会員ID
    target: str | None = None  # 意見の箇所
    point: str = ""
    response: str | None = None
    response_by: str | None = None


class MinutesActionItem(BaseModel):
    owner: str | None = None  # 会員ID
    what: str = ""
    due: str | None = None  # 「次の理事会まで」など


class MinutesSection(BaseModel):
    """議案（または議題）ごとのまとめ（§5.5）。"""

    proposal: str | None = None
    title: str = ""
    explained: list[str] = Field(default_factory=list)
    opinions: list[MinutesOpinion] = Field(default_factory=list)
    decided: list[str] = Field(default_factory=list)
    action_items: list[MinutesActionItem] = Field(default_factory=list)
    for_absent: str | None = None


class MinutesSensitive(BaseModel):
    """内々の話（人事・入会候補・個人の事情など）。既定でグループ向けに入れない。"""

    sensitive_id: str
    at: str | None = None
    kind: str = "private"  # personnel | candidate | private | money
    summary: str = ""
    include: bool = False


class MinutesSend(BaseModel):
    """議事録を送った記録。"""

    kind: str = "group"  # group（会議体の要点）| committee（委員会向け）
    channel: str = "group"  # group（LINE グループ）| user（個別の LINE）
    to_id: str | None = None
    to_name: str | None = None
    committee: str | None = None
    text: str = ""
    sent_at: datetime | None = None
    sent_by: str | None = None


class CommitteeMinutes(BaseModel):
    """委員会向けの議事録（その委員会に関わる所だけ）。"""

    committee: str
    text: str = ""
    created_at: datetime | None = None


class MeetingMinutes(BaseModel):
    """会議の議事録の作業と案（§8）。"""

    event_id: str
    status: MinutesStatus = MinutesStatus.queued
    #: 作業の版。やり直すと新しくなり、古い版の作業の知らせは捨てる
    run_id: str = ""
    #: 作業の段の通し番号。同じ段を二重に進めない
    seq: int = 0
    recording_file_id: str | None = None
    recording_name: str | None = None
    recording_folder_id: str | None = None
    source: str = "meet_recording"  # meet_recording | meet_transcript | upload
    gcs_uri: str | None = None
    duration_sec: int | None = None
    chunk: int = 0  # 一次の読み取りで次に読む区切り（0 始まり）
    segments: list[MinutesSegment] = Field(default_factory=list)
    unclear: list[MinutesUnclear] = Field(default_factory=list)
    sections: list[MinutesSection] = Field(default_factory=list)
    confirmations: list[str] = Field(default_factory=list)
    sensitive: list[MinutesSensitive] = Field(default_factory=list)
    committees: list[CommitteeMinutes] = Field(default_factory=list)
    doc_id: str | None = None
    sends: list[MinutesSend] = Field(default_factory=list)
    attempts: int = 0
    error: str | None = None
    requested_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    edited_by: str | None = None
