# 設定項目

本システムの設定は、次の 3 か所にあります。

| 場所 | 中身 | 変え方 |
|---|---|---|
| 環境変数 | 接続先・有効にする機能 | Cloud Run の `--update-env-vars`、手元では `.env` |
| Secret Manager | パスワード・トークン類 | `gcloud secrets versions add` |
| Firestore の設定（`settings`） | 静音時間・送信数の上限・緊急停止・リッチメニュー ID など運用中に変えるもの | 管理画面、または管理 API `/admin/settings` |

手元で動かすときは [.env.example](../.env.example) を `.env` にコピーして使います（`.env` は git に入れないこと）。

環境変数を Cloud Run に設定するときは `--set-env-vars` ではなく `--update-env-vars` を使ってください。
`--set-env-vars` は指定しなかった変数を消します。

---

## シークレット

環境変数があればそれを使い、なければ Secret Manager から読みます（[app/config.py](../app/config.py) の `get_secret`）。
本番では Cloud Run の `--set-secrets` で環境変数としてマウントします。

| 環境変数 | Secret Manager の名前 | 用途 | 必須 |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | `line-channel-secret` | LINE Webhook の署名検証 | ○ |
| `LINE_CHANNEL_ACCESS_TOKEN` | `line-channel-access-token` | LINE への送信 | ○ |
| `ADMIN_API_SECRET` | `admin-api-secret` | `/tasks`・`/admin` を呼ぶときの共有シークレット（ヘッダ `X-Admin-Token`）。回答の出典ファイルを開く署名付きリンクの鍵にも使う | ○ |
| `GMAIL_OAUTH_CLIENT_ID` | `gmail-oauth-client-id` | 対外連絡メールの取込・議案の送信（LOM 公式 Gmail の OAuth） | 対外連絡を使う場合 |
| `GMAIL_OAUTH_CLIENT_SECRET` | `gmail-oauth-client-secret` | 同上 | 同上 |
| `GMAIL_OAUTH_REFRESH_TOKEN` | `gmail-oauth-refresh-token` | 同上（`scripts/gmail_oauth_setup.py` で取得） | 同上 |

---

## 環境変数

### 基本

| 名前 | 既定値 | 説明 |
|---|---|---|
| `GCP_PROJECT_ID` | （なし・必須） | Google Cloud のプロジェクト ID |
| `TZ` | `Asia/Tokyo` | タイムゾーン |
| `ROLE` | （なし） | `admin` にすると管理サービスとして動く。IAP の付けるログイン情報を信用するのはこのときだけ。**公開サービスには設定しない** |
| `PUBLIC_BASE_URL` | （`PUBSUB_PUSH_AUDIENCE` から求める） | 公開サービスの URL。LINE で送るリンク（出典ファイルなど）に使う |

### LOM の情報

LOM ごとに変わる名前・URL です。コードには直書きしません（[app/lom.py](../app/lom.py)）。
公開サービス・管理サービスの**両方**に同じ値を設定してください。

| 名前 | 既定値 | 説明 |
|---|---|---|
| `LOM_ID` | `lom` | データに付ける LOM の識別子（英数字。例: `inawashiro`） |
| `LOM_NAME` | `青年会議所` | LOM の名前（例: `猪苗代青年会議所`）。AI への指示・画面の文言に使う |
| `LOM_SHORT_NAME` | （`LOM_NAME`） | 短い呼び名（例: `猪苗代JC`） |
| `BOT_NAME` | `専務AI` | LINE 公式アカウントの名前（例: `猪苗代専務AI`）。LINE の文面・LIFF・管理画面に出る |
| `LINE_BASIC_ID` | （なし） | LINE 公式アカウントのベーシック ID（例: `@abc1234`）。友だち追加のリンクに使う |
| `ADMIN_BASE_URL` | （なし） | 管理画面の URL（例: `https://admin.example.org`）。LINE のメニューの「管理画面を開く」などに使う |
| `LOM_GOOGLE_ACCOUNT` | （なし） | LOM 公式の Google アカウント（カレンダー・ドライブ・Gmail の持ち主） |

サービスアカウント（`drive-reader`・`calendar-sync`・`doc-editor`）のメールアドレスは、`GCP_PROJECT_ID` から
`<名前>@<プロジェクト ID>.iam.gserviceaccount.com` として求めます。

### AI（Vertex AI）

| 名前 | 既定値 | 説明 |
|---|---|---|
| `VERTEX_AI_LOCATION` | `asia-northeast1` | Vertex AI のリージョン |
| `GEMINI_MODEL` | `gemini-2.5-pro` | タスク別のモデル表（[app/llm.py](../app/llm.py) の `TASK_MODELS`）に無いタスクで使うモデル |
| `LLM_PRICE_INPUT_USD_PER_1M` | （モデル別の既定単価） | 管理画面の「当月コスト」の概算に使う入力単価（USD / 100 万トークン） |
| `LLM_PRICE_OUTPUT_USD_PER_1M` | （同上） | 同、出力単価 |
| `INFRA_MONTHLY_USD` | `0` | 同、Cloud Run などの固定費の月額概算（USD） |

### LINE

| 名前 | 既定値 | 説明 |
|---|---|---|
| `LIFF_ID` | （なし） | 日程調整・予定と出欠などの LIFF 画面の ID。未設定なら LIFF 画面は使えない |
| `LINE_LOGIN_CHANNEL_ID` | （なし） | LIFF を置いた LINE ログインチャネルの ID（ID トークンの検証に使う） |
| `LINE_ASYNC_REPLY` | （なし） | `1` で、LINE への返信を Webhook の外（Pub/Sub 経由）で作る。AI の回答で Webhook が時間切れになるのを防ぐ。`scripts/setup_line_reply.sh` の後に設定する |

### データレイク・検索（`scripts/setup_datalake.sh`・`scripts/setup_lake_ai.sh`）

| 名前 | 既定値 | 説明 |
|---|---|---|
| `LINE_EVENTS_TOPIC` | （なし） | LINE の受信イベントを流す Pub/Sub トピック。未設定ならデータレイクは無効 |
| `LINE_CONTENT_BUCKET` | （なし） | LINE で共有されたファイルを保存する Cloud Storage のバケット |
| `LINE_CONTENT_MAX_BYTES` | `52428800`（50MB） | 保存するファイルの上限サイズ |
| `PUBSUB_PUSH_SA` | （なし） | Pub/Sub の Push を送るサービスアカウント（受け取り側で検証する） |
| `PUBSUB_PUSH_AUDIENCE` | （なし） | Pub/Sub の Push の宛先 URL（OIDC トークンの audience） |
| `LAKE_DATASET` | `line_lake` | BigQuery のデータセット |
| `LAKE_LOCATION` | `asia-northeast1` | BigQuery のロケーション |
| `LAKE_EMBEDDING_MODEL` | （なし） | 埋め込みモデル（BigQuery のリモートモデル）。未設定なら資料・やり取りの検索は無効 |

### Google Workspace

| 名前 | 既定値 | 説明 |
|---|---|---|
| `GCAL_CALENDAR_ID` | （なし） | 連携する Google カレンダーの ID。未設定ならカレンダー連携は無効 |
| `DRIVE_KNOWLEDGE_FOLDERS` | （なし） | 検索の対象にするドライブのフォルダ ID（カンマ区切り）。未設定ならドライブ取込は無効 |
| `COMMITTEE_DOCS_FOLDER` | （なし） | 委員会資料のフォルダ ID。LINE から資料を編集する機能に使う |
| `PLAN_TEMPLATE_DOC` | （なし） | 議案書の雛形にする Google ドキュメントの ID |
| `GMAIL_LABEL` | `対外連絡` | 対外連絡として取り込む Gmail のラベル |

### 手元の開発用

| 名前 | 説明 |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Google Cloud の認証情報。通常は `gcloud auth application-default login` を使い、設定しない |

---

## Firestore の設定（`settings`）

運用中に変える値は Firestore に置き、管理画面から変えます。主なもの:

| 項目 | 説明 |
|---|---|
| `kill_switch` | `true` で LINE への自動送信をすべて止める（[runbook.md](runbook.md) §4） |
| `quiet_hours` | 送らない時間帯（既定 21:00〜8:00） |
| `rate_limit` | 1 人あたり・全体の送信数の上限 |
| `operation_start` | 運用開始日（年度の始まり）。これより前の予定は管理画面の一覧に既定で出さず、カレンダーからも取り込まない |
| `rich_menus` | 立場ごとのリッチメニュー ID（`scripts/setup_rich_menu.py --create` が保存） |
| `line_groups` | 日程調整などの既定の案内先にする LINE グループ（`goyaku`=五役 / `riji`=理事会 / `all`=会員全員）。管理画面の「設定」で、ボットが参加しているグループから選ぶ（グループ ID で保存）。年度ごとに選び直す |

項目の一覧は [app/models.py](../app/models.py) の `Settings` を参照してください。
