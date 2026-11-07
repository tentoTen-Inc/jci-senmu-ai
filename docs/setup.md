# セットアップ手順（新しい LOM で立ち上げる）

自分の LOM 用に、Google Cloud と LINE の上で本システムを立ち上げる手順です。
1 つの LOM につき 1 つの Google Cloud プロジェクトを使います（複数 LOM で 1 つの環境を共有する作りにはなっていません）。

> [!IMPORTANT]
> ターミナル（コマンドを打つ画面）での作業が多くあります。Google Cloud を触ったことがない場合は、慣れた人と一緒に進めてください。
> 始める前に、README の[前提と制限](../README.md#前提と制限)と[費用の目安](../README.md#費用の目安)を読んでください。
> LOM 名などの設定は [§3](#3-lom-の情報を設定する) を参照してください。

設定値の一覧は [configuration.md](configuration.md) を参照してください。

---

## 0. 用意するもの

| もの | 用途 |
|---|---|
| Google Cloud の課金が有効なプロジェクト | Cloud Run・Firestore・Vertex AI など。オーナー権限のあるアカウントで作業する |
| LOM の共有 Google アカウント（例: LOM 公式 Gmail） | カレンダー・ドライブ・Gmail をこのアカウントのものとして扱う |
| LINE 公式アカウント | 会員とのやり取り。LINE Developers で Messaging API を有効にする。送る数によっては有料プランが要る（README「前提と制限」） |
| Meet の録画ができる Google のプラン（任意） | 会議の自動録画と、録画からの議事録の下書きに使う。LOM 公式アカウントに付ける（README「前提と制限」） |
| 手元の PC | `gcloud`・`bq`（Google Cloud CLI）、Python 3.12、`openssl`、`git` |
| GitHub リポジトリ（任意） | 自動デプロイを使う場合。本リポジトリを fork する |

費用は Cloud Run・Vertex AI・BigQuery などの従量課金です（README「費用の目安」）。
`scripts/setup_ops.sh` で月額予算のアラートを設定できます。

### 0-1. 手元の PC を準備する

リポジトリを取ってきて、Python の仮想環境（このリポジトリ専用の Python の置き場所）を作ります。
§5 以降のスクリプトは、ここで作った `.venv/bin/python` で動かします。

```bash
git clone https://github.com/<GitHub のオーナー>/<リポジトリ名>.git
cd <リポジトリ名>
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 名簿の読み込み・リッチメニューの画像づくりの部品も入る
```

Google Cloud にログインします。2 つとも必要です。

```bash
gcloud auth login                       # gcloud コマンド用
gcloud auth application-default login   # スクリプト（Python）から Google Cloud を使う用
```

以下、例として次のシェル変数を使います。スクリプトは `GCP_PROJECT_ID`（と、あれば `GCP_REGION`）を読みます。

```bash
export PROJECT=your-project-id      # Google Cloud のプロジェクト ID
export REGION=asia-northeast1
export GCP_PROJECT_ID="$PROJECT" GCP_REGION="$REGION"
gcloud config set project "$PROJECT"
```

設定を毎回打たずに済ませたいときは、[.env.example](../.env.example) を `.env` にコピーして値を書き、
作業を始めるたびに次のように読み込みます（`.env` は git に入れないこと）。

```bash
set -a; source .env; set +a
```

各スクリプトの冒頭にある「必要な環境変数（GCP_PROJECT_ID など）を設定した状態で」は、このどちらかをした状態のことです。

---

## 1. Google Cloud の基本

### 1-1. API を有効にする

```bash
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  aiplatform.googleapis.com iamcredentials.googleapis.com iap.googleapis.com
```

Google Workspace の API は、使う機能の分だけ有効にします（まとめて有効にしてもかまいません）。

```bash
gcloud services enable \
  drive.googleapis.com docs.googleapis.com calendar-json.googleapis.com gmail.googleapis.com \
  forms.googleapis.com meet.googleapis.com
```

| API | 使う機能 |
|---|---|
| `drive.googleapis.com`・`docs.googleapis.com` | ドライブ資料の取込、委員会資料の編集、議案書の作成、議事録（録画の読み取り） |
| `calendar-json.googleapis.com` | Google カレンダー連携 |
| `gmail.googleapis.com` | 対外連絡メールの取込、会議の案内メールの下書き |
| `forms.googleapis.com` | アンケート（Google フォームの集計） |
| `meet.googleapis.com` | 会議の予定に付けた Meet を自動録画にする |

Pub/Sub・BigQuery など、機能ごとに必要なほかの API は §6 の各スクリプトが有効にします。

### 1-2. Firestore を作る

```bash
gcloud firestore databases create --location="$REGION" --type=firestore-native
```

### 1-3. 実行用サービスアカウントを作る

Cloud Run は `app-runtime` というサービスアカウントで動きます（名前はスクリプトの前提です）。

```bash
gcloud iam service-accounts create app-runtime --display-name "Cloud Run runtime"
SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
for role in roles/datastore.user roles/secretmanager.secretAccessor \
            roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${SA}" --role "$role"
done
```

BigQuery 関連のロールは §6 の `setup_datalake.sh`・`setup_lake_ai.sh` が付けます。

Google Workspace（ドライブ・カレンダー・ドキュメント）を読み書きするときは、`app-runtime` が次のサービスアカウントの権限を借りて動きます（鍵は作りません）。
どれも §6 のスクリプトが作ります。権限は、ドライブやカレンダーで**そのアドレスに共有したものだけ**です。

| サービスアカウント | 作るスクリプト | 共有するもの | 名前を変えたときの環境変数 |
|---|---|---|---|
| `drive-reader@<プロジェクト ID>.iam.gserviceaccount.com` | `scripts/setup_drive_reader.sh` | AI に読ませるドライブのフォルダ、アンケートの Google フォーム（閲覧者） | `DRIVE_READER_SA` |
| `calendar-sync@<プロジェクト ID>.iam.gserviceaccount.com` | `scripts/setup_calendar_sync.sh` | LOM のカレンダー（予定の変更） | `GCAL_SYNC_SA` |
| `doc-editor@<プロジェクト ID>.iam.gserviceaccount.com` | `scripts/setup_doc_editor.sh` | 委員会のフォルダ（例: `04-委員会`。編集者） | `DOC_EDITOR_SA` |

名前を変えなければ、環境変数は設定しなくてかまいません（`GCP_PROJECT_ID` からアドレスを求めます）。

---

## 2. LINE の設定

### 2-1. Messaging API チャネル

LINE Developers コンソールで、LINE 公式アカウントの Messaging API チャネルを開き、次を設定します。

- 「チャネルシークレット」と「チャネルアクセストークン（長期）」を控える
- 「Webhook の利用」を **オン**（URL は §4 のデプロイ後に設定）
- 「応答メッセージ」を **オフ**、「あいさつメッセージ」は任意
- 「グループトーク・複数人トークへの参加を許可する」を **オン**（グループでの案内・質問に使う）

### 2-2. LINE ログインチャネルと LIFF アプリ

日程調整や「予定と出欠」の画面は LIFF（LINE の中で開く Web 画面）で開きます。

1. Messaging API と**同じプロバイダー**に LINE ログインチャネルを作る
2. そのチャネルに LIFF アプリを追加する
   - エンドポイント URL: `https://<公開サービスの URL>/liff/poll`（§4 のデプロイ後に決まる）
   - サイズ: Tall、Scope: `openid` `profile`
3. ログインチャネルの「チャネル ID」と LIFF の「LIFF ID」を控える

### 2-3. シークレットを Secret Manager に入れる

値を画面やシェルの履歴に残さないよう、標準入力から渡します。

```bash
printf '%s' '<チャネルシークレット>'     | gcloud secrets create line-channel-secret --data-file=-
printf '%s' '<チャネルアクセストークン>' | gcloud secrets create line-channel-access-token --data-file=-
openssl rand -hex 32 | tr -d '\n'       | gcloud secrets create admin-api-secret --data-file=-
```

`admin-api-secret` は Cloud Scheduler から `/tasks/tick` を呼ぶときの共有シークレットです。

---

## 3. LOM の情報を設定する

LOM 名・ボット名・管理画面の URL などは環境変数で設定します（[configuration.md](configuration.md#lom-の情報)）。
§4 のデプロイのあと、公開サービス・管理サービスの**両方**に同じ値を入れてください。

```bash
LOM_VARS="LOM_ID=yourlom,LOM_NAME=○○青年会議所,LOM_SHORT_NAME=○○JC,BOT_NAME=○○専務AI,\
LINE_BASIC_ID=@xxxxxxx,ADMIN_BASE_URL=https://<管理サービスの URL>,LOM_GOOGLE_ACCOUNT=<LOM 公式の Gmail>"
for s in jci-sed-agent jci-sed-admin; do
  gcloud run services update "$s" --region "$REGION" --update-env-vars "$LOM_VARS"
done
```

日程調整の既定の案内先（年度ごとに変わる LINE グループ）は、ボットをグループに招待したあと、管理画面の「設定」→「案内先の LINE グループ」で一覧から選びます。

次の値は、まだ設定に移せていません。fork したリポジトリで必要に応じて書き換えてください。

| 値 | 主な場所 |
|---|---|
| 役職・会議体の名前（理事長・副理事長・五役会・理事会など） | `app/models.py`、`app/roles.py` |
| 役員向けのドライブのフォルダの名前（`執行部`・`五役会`） | `app/drive_knowledge.py` の `OFFICER_FOLDERS` |
| 議案まわりのフォルダ名・雛形の決まり | [§6-2](#6-2-議案まわりの前提フォルダ名と雛形) |
| 独自ドメイン | `scripts/setup_domains.sh` |

個人情報として取り込まない名前（`名簿` など）は、環境変数 `DRIVE_EXCLUDE_WORDS` で足せます（[§6-1](#6-1-ドライブの資料を読ませる)）。

## 4. デプロイ

### 4-1. 公開サービス（LINE Webhook・LIFF・毎時の処理）

```bash
./scripts/deploy.sh   # GCP_PROJECT_ID を設定した状態で
```

表示された URL（`https://jci-sed-agent-xxxx.a.run.app`）を控え、次を設定します。

- LINE Developers の Webhook URL: `https://<公開サービスの URL>/line/webhook` →「検証」が成功すること
- LIFF のエンドポイント URL: `https://<公開サービスの URL>/liff/poll`
- 公開サービスの環境変数に LIFF を設定する

```bash
gcloud run services update jci-sed-agent --region "$REGION" \
  --update-env-vars "LIFF_ID=<LIFF ID>,LINE_LOGIN_CHANNEL_ID=<ログインチャネル ID>"
```

### 4-2. 管理サービス（管理画面・IAP）

同じソースを `ROLE=admin` で、認証なしの呼び出しを許可せずにデプロイし、IAP（Google アカウントでのログイン）を有効にします。

```bash
gcloud run deploy jci-sed-admin --source . --region "$REGION" \
  --service-account "app-runtime@${PROJECT}.iam.gserviceaccount.com" \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT},TZ=Asia/Tokyo,ROLE=admin" \
  --no-allow-unauthenticated --iap
```

管理画面に入れる人（専務・事務局・役員の Google アカウント）を許可リストに加えます。

```bash
gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service=jci-sed-admin \
  --region "$REGION" --member "user:<メールアドレス>" --role roles/iap.httpsResourceAccessor
```

`@gmail.com` のアカウントを通すには、IAP に自前の OAuth クライアントを設定します（`scripts/setup_iap_oauth.sh` の冒頭の説明を参照）。

### 4-3. 毎時の処理（Cloud Scheduler）

催促・予約投稿・カレンダーやドライブの取込は、毎時 `/tasks/tick` を呼んで動かします。

```bash
URL="$(gcloud run services describe jci-sed-agent --region "$REGION" --format='value(status.url)')"
gcloud scheduler jobs create http jci-tick --location "$REGION" \
  --schedule "0 8-21 * * *" --time-zone Asia/Tokyo \
  --uri "${URL}/tasks/tick" --http-method POST --attempt-deadline 300s \
  --headers "X-Admin-Token=$(gcloud secrets versions access latest --secret admin-api-secret)"
```

### 4-4. 自動デプロイ（任意）

GitHub の `main` へのマージで両サービスへ自動デプロイするには、次を 1 回実行します。
Workload Identity 連携を作り、GitHub の Secrets（`GCP_WIF_PROVIDER`・`GCP_DEPLOY_SA`）と Variables（`GCP_PROJECT_ID`・`GCP_REGION`）を登録します。

```bash
GITHUB_REPO=<GitHub のオーナー>/<リポジトリ名> ./scripts/setup_github_deploy.sh
```

あわせて、GitHub の `production` 環境に「承認してから実行」（承認者はスクリプトを実行した人）と「main からだけ」の決まりを付けます。main にマージすると CI のあとにデプロイが始まり、GitHub の Actions の画面で［Review deployments］→［Approve］を押すと本番に反映されます。
Google Cloud 側は、このリポジトリ（ID で照合）の `production` 環境で動くジョブからしか、デプロイを受け付けません。

Variables の `GCP_PROJECT_ID` が未設定のリポジトリでは、自動デプロイは動きません（CI だけが動きます）。
fork した場合も、元のリポジトリの Secrets・Variables は引き継がれないので、自分のプロジェクトで上のスクリプトを実行してください。

---

## 5. 初期データ

スクリプトは、§0-1 で作った `.venv/bin/python` で、環境変数 `GCP_PROJECT_ID` を設定した状態で動かします。

### 5-1. 催促ルールの既定値

管理画面の「設定」→「催促のタイミング」で［標準の設定を入れる］を押します（例会・理事会の締切に合わせた依頼・催促のタイミングが入ります）。

### 5-2. 会員と組織図

会員の名前・役職・委員会を入れます。次の 3 つのやり方があります。人数や手元の資料に合わせて選んでください。

| やり方 | 向いているとき |
|---|---|
| A. 管理画面で 1 人ずつ | 人数が少ない。あとから 1 人だけ足す |
| B. 組織図を JSON にして取り込む（`scripts/import_org.py`） | 年度の始めに、役職・委員会をまとめて入れる |
| C. 会員名簿の xlsx を取り込む（`scripts/import_roster.py`） | 決まった様式（下記）の名簿があり、連絡先も入れたい |

名簿・組織図は個人情報です。ファイルはリポジトリの外か、git に入らない `data/` に置いてください。

**A. 管理画面で 1 人ずつ**

管理画面の「会員・LINE連携」→［＋ 会員を追加］で、氏名・ふりがな・所属委員会・役職・会員区分を入れます。

**B. 組織図（JSON）を取り込む**

見本: [docs/samples/org.sample.json](samples/org.sample.json)（架空の名前です）。これをコピーして、自分の LOM の組織図に書き換えます。

| 項目 | 必須 | 内容 |
|---|---|---|
| `name` | ○ | 氏名。既存の会員とは**氏名で**突き合わせます（空白の有無は無視） |
| `kana` | | ふりがな |
| `member_type` | | 会員区分。`regular`（正会員・既定）／`youth_support`（青年賛助会員）／`office`（事務局）／`external_auditor`（外部監事）／`ob`／`support`（賛助会員） |
| `officer_role` | | 役職。`理事長`・`副理事長`・`専務理事`・`監事`・`直前理事長` など |
| `committees` | | 所属する委員会（兼務も）。`{"committee": "委員会名", "role": "委員長"}` の並び。**先頭が主たる所属**。`role` は `委員長`・`副委員長`・`委員` |
| `supervised_committees` | | 副理事長が担当する委員会の名前の並び（所属ではありません） |
| `secondments` | | 出向先。`{"org": "出向先", "role": "役職"}` の並び |

委員会名は、ドライブの委員会フォルダの名前（並び順の番号を除いたもの）とそろえてください（[§6-2](#6-2-議案まわりの前提フォルダ名と雛形)）。

```bash
.venv/bin/python scripts/import_org.py data/org.json          # 確認だけ（何も書き込まない）
.venv/bin/python scripts/import_org.py data/org.json --apply  # 書き込む
```

年度の切り替えでは `--operation-start <年度の始まりの日>`（例: `2027-01-01`）と、名簿にいない人を「無効」にする `--deactivate-missing` も使えます（削除はしません）。

**C. 会員名簿（xlsx）を取り込む**

`scripts/import_roster.py` は、次の様式（1 人 2 行のカード型）の xlsx だけを読めます（[app/roster_import.py](../app/roster_import.py)）。
ほかの様式の名簿は、この形に並べ直すか、A・B のやり方を使ってください。

| 列 | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| 上の行 | No | 氏名 | 〒 | 自宅TEL | 勤務先 | 勤務先TEL | Mobile |
| 下の行 | | 生年月日 | 自宅住所 | | 役職名 | FAX | E-Mail |

B 列が「氏名」の行を見出しとして探し、見出し 2 行の次から 2 行ずつ読みます。

```bash
.venv/bin/python scripts/import_roster.py data/会員名簿.xlsx --sheet <シート名>          # 確認だけ
.venv/bin/python scripts/import_roster.py data/会員名簿.xlsx --sheet <シート名> --upsert # 投入
```

B と C を両方使うときは、C（名簿）→ B（組織図）の順にすると、組織図が名簿の会員に氏名で重なります。

### 5-3. リッチメニュー

```bash
.venv/bin/python scripts/setup_rich_menu.py --create --liff-id <LIFF ID>
```

### 5-4. 会員と LINE を結びつける

会員に LINE で友だち追加してもらい、管理画面の「会員・LINE連携」で招待コードを発行して本人に渡します（または、会員が連携リンクから申請し、管理画面で承認します）。

---

## 6. 機能ごとの追加設定

使う機能だけ設定します。各スクリプトは何度実行しても同じ結果になるように作っています（冒頭のコメントに前提と使い方があります）。

| 機能 | スクリプト・手順 | 設定する環境変数 |
|---|---|---|
| LINE のデータレイク（会話・ファイルの保存） | `scripts/setup_datalake.sh` | `LINE_EVENTS_TOPIC`・`LINE_CONTENT_BUCKET`・`PUBSUB_PUSH_SA`・`PUBSUB_PUSH_AUDIENCE` |
| 資料・やり取りの検索（RAG） | `scripts/setup_lake_ai.sh`（データレイクの後） | `LAKE_EMBEDDING_MODEL` |
| AI の返信を Webhook の外で作る（推奨） | `scripts/setup_line_reply.sh`（データレイクの後） | `LINE_ASYNC_REPLY=1` |
| ドライブ資料の取込 | `scripts/setup_drive_reader.sh` → 表示されたアドレスにフォルダを閲覧者で共有（[§6-1](#6-1-ドライブの資料を読ませる)） | `DRIVE_KNOWLEDGE_FOLDERS`、（任意）`DRIVE_EXCLUDE_WORDS` |
| アンケート（Google フォームの集計） | `scripts/setup_drive_reader.sh` → フォームごとに同じアドレスへ共有 | — |
| Google カレンダー連携 | `scripts/setup_calendar_sync.sh`、カレンダーをサービスアカウントに共有 | `GCAL_CALENDAR_ID` |
| 会議の予定に Meet を付けて自動録画 | カレンダー連携の後、`gmail_oauth_setup.py --with-calendar`（下記）。Meet API と、録画できるプランが要る | — |
| 委員会資料の編集・議案書の体裁の自動修正 | `scripts/setup_doc_editor.sh <フォルダ ID>` | `COMMITTEE_DOCS_FOLDER`（会員向けと管理画面の両方のサービス） |
| 議案書の作成 | 委員会資料の編集の後、雛形の Google ドキュメントを用意し、`gmail_oauth_setup.py --with-drive`（[§6-2](#6-2-議案まわりの前提フォルダ名と雛形)） | `PLAN_TEMPLATE_DOC` |
| 対外連絡メールの取込 | `scripts/gmail_oauth_setup.py --write-secrets`（LOM 公式 Gmail で同意） | `GMAIL_LABEL`（任意） |
| 会議の案内メール（LOM の Gmail に下書きを作る） | `gmail_oauth_setup.py --with-send` | — |
| 議事録の下書き（Meet の録画から） | `gmail_oauth_setup.py --with-drive`（録画を読む）、`scripts/setup_minutes.sh`（データレイクの後） | `MINUTES_BUCKET`・`MINUTES_TOPIC`（スクリプトが設定） |
| 障害通知・バックアップ・予算アラート | `BUDGET_EMAIL=<通知先> ./scripts/setup_ops.sh` | — |
| 独自ドメイン | `scripts/setup_domains.sh` | `PUBLIC_BASE_URL` |

**LOM 公式アカウントの OAuth（`scripts/gmail_oauth_setup.py`）について**

Gmail・議案書の作成・Meet・議事録は、LOM 公式の Google アカウントとして動きます。そのアカウントで一度だけ同意し、得られた鍵（refresh token）を Secret Manager に入れます。
同意し直すと、前の鍵の権限は新しい鍵に置き換わります。**使う機能の印をまとめて付けて**実行してください。

```bash
# 例: 対外連絡・議案書の作成・議事録・案内メール・Meet の自動録画をすべて使う
.venv/bin/python scripts/gmail_oauth_setup.py --client-secret-file <ダウンロードした JSON> \
  --with-drive --with-send --with-calendar --write-secrets
```

| 印 | 許可する権限 | 使う機能 |
|---|---|---|
| （なし） | Gmail の読み取り | 対外連絡メールの取込 |
| `--with-drive` | ドライブ | 議案書の作成、議事録（録画を読む・一時置き場へ写す） |
| `--with-send` | Gmail の送信・下書き | 会議の案内メールの下書き |
| `--with-calendar` | カレンダーの予定・Meet の設定 | 会議の予定に Meet を付けて自動録画 |

OAuth クライアントの作り方と、同意画面を「本番」にする理由（「テスト」のままだと 7 日で失効する）は、スクリプトの冒頭の説明と [runbook.md](runbook.md) の「対外連絡メールの取込（初回）」にあります。

詳しい仕組みは各機能の設計書（[docs/README.md](README.md)）にあります。

### 6-1. ドライブの資料を読ませる

1. `./scripts/setup_drive_reader.sh` を実行する。最後に表示されるアドレス（`drive-reader@<プロジェクト ID>.iam.gserviceaccount.com`）を控える
2. ドライブで、AI に読ませたいフォルダ（例: 年度のフォルダ）を、そのアドレスに**閲覧者**で共有する
3. フォルダの ID（URL の `folders/` の後ろ）を `DRIVE_KNOWLEDGE_FOLDERS` に入れる（カンマ区切りで複数可）

> [!WARNING]
> 名簿などの個人情報は、**フォルダ名・ファイル名に決まった語が含まれているかどうか**で除外しているだけです。中身は見ていません。
> 既定で除外する語: `名簿`・`住所録`・`入会`・`認承証`・`個人情報`・`事務局`。
> LOM で使っている名前があれば、環境変数 `DRIVE_EXCLUDE_WORDS`（カンマ区切り。例: `連絡先,緊急連絡網`）で足せます。
>
> 個人情報の資料は、**共有するフォルダに置かない**か、**名前に除外する語を入れて**ください。

あわせて、次の決まりがあります。

- パスに `執行部` か `五役会` を含むフォルダの資料は、五役・監事への回答にだけ使います。それ以外は会員全員への回答に使います。役員だけで見る資料は、この名前のフォルダに置いてください。
- LINE グループの中での質問には、会員全員向けの資料だけを使います。

### 6-2. 議案まわりの前提（フォルダ名と雛形）

議案書の作成・体裁のチェック・議案台帳への同期は、**開発元の LOM のドライブの並べ方と事業計画書の様式を前提に**作っています。
同じ並べ方にそろえるか、fork したリポジトリで下の値を書き換えてください（今は設定では変えられません）。

**フォルダの並べ方**

```
<委員会フォルダの親>            ← COMMITTEE_DOCS_FOLDER（例: 04-委員会）。doc-editor に編集者で共有
├── 01-総務委員会               ← 委員会ごとのフォルダ。「番号-」を除いた名前が委員会名
│   ├── 00-年間計画（サマリー）  ← 委員会サマリー（名前に「サマリー」「summary」を含む文書でもよい）
│   └── 01-例会・事業            ← 議案書の作成で作られる（中に「YYMM_事業名」のフォルダ）
│       └── 2703_3月例会
│           └── 【計画書】3月例会事業計画書
└── 02-まちづくり委員会
```

- 委員会フォルダの名前から先頭の番号（`01-` など）を除いたものを委員会名として扱います。組織図の委員会名（§5-2）とそろえてください。
- 事業計画書は、名前が `【計画書】` で始まる Google ドキュメントを台帳に写します。
- 委員会サマリーは、名前に `サマリー` か `summary` を含む文書、またはパスに `年間計画` を含む文書です。

**事業計画書の雛形（`PLAN_TEMPLATE_DOC`）**

- 基本情報は「項目名｜内容」の表で書きます。作成のときに `ファイル名`・`委員会名`・`担当副理事長`・`委員長`・`文書作成人（役職/氏名）`・`文書作成日時` を記入します。
- 雛形の記入例の年（`2026年◯月◯日(土)`・`2026年度様式`・`2026年　月　日`）を、その議案の年と実施日に置き換えます。`◯` は未記入の印として扱います。
- 体裁のチェックでは、次の見出しがあるかを確かめます（2024 年の様式から抜き出したもの）: `事業名`・`事業実施に至る背景`・`事業の対象者`・`事業目的`・`KPI`・`実施日時`・`実施場所`・`予算`・`事業内容`・`討議`。

**自分の LOM に合わせて変えるところ**

| 変えたいもの | 場所 |
|---|---|
| 議案書を作るフォルダの名前（`01-例会・事業`） | `app/proposal_intent.py` の `PLANS_FOLDER` |
| 雛形の記入例の年（`2026`） | `app/proposal_intent.py` の `TEMPLATE_YEAR` |
| 事業計画書の名前の頭（`【計画書】`） | `app/proposal_sync.py` の `PLAN_PREFIX` |
| 台帳に写す基本情報の項目 | `app/proposal_sync.py` の `BASIC_KEYS` |
| 体裁のチェックで確かめる見出し | `app/format_check.py` の `REQUIRED_SECTIONS_BY_DOCTYPE` |

---

## 7. 動作確認

- `curl ${URL}/health` が 200 を返す
- 自分の LINE で友だち追加 → 招待コードで連携 → リッチメニューが切り替わる
- 管理画面で予定を 1 件作り、自分だけを対象に出欠確認を送る → LINE で回答 → 管理画面に反映される
- 緊急停止の手順（[runbook.md](runbook.md) §4）を一度試しておく
