# セットアップ手順（新しい LOM で立ち上げる）

自分の LOM 用に、Google Cloud と LINE の上で本システムを立ち上げる手順です。
1 つの LOM につき 1 つの Google Cloud プロジェクトを使います（複数 LOM で 1 つの環境を共有する作りにはなっていません）。

> [!IMPORTANT]
> 公開準備中のため、まだ手作業の手順が多くあります。
> LOM 名などの設定は [§3](#3-lom-の情報を設定する) を参照してください。

設定値の一覧は [configuration.md](configuration.md) を参照してください。

---

## 0. 用意するもの

| もの | 用途 |
|---|---|
| Google Cloud の課金が有効なプロジェクト | Cloud Run・Firestore・Vertex AI など。オーナー権限のあるアカウントで作業する |
| LOM の共有 Google アカウント（例: LOM 公式 Gmail） | カレンダー・ドライブ・Gmail をこのアカウントのものとして扱う |
| LINE 公式アカウント | 会員とのやり取り。LINE Developers で Messaging API を有効にする |
| 手元の PC | `gcloud`・`bq`（Google Cloud CLI）、Python 3.12、`openssl` |
| GitHub リポジトリ（任意） | 自動デプロイを使う場合。本リポジトリを fork する |

費用は Cloud Run・Vertex AI・BigQuery などの従量課金です。
`scripts/setup_ops.sh` で月額予算のアラートを設定できます。

以下、例として次のシェル変数を使います。

```bash
export PROJECT=your-lom-sed-agent   # Google Cloud のプロジェクト ID
export REGION=asia-northeast1
gcloud config set project "$PROJECT"
```

---

## 1. Google Cloud の基本

### 1-1. API を有効にする

```bash
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  aiplatform.googleapis.com iamcredentials.googleapis.com iap.googleapis.com
```

機能ごとに必要な API（Pub/Sub・BigQuery・Calendar・Drive・Docs・Gmail など）は、§6 の各スクリプトが有効にします。

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

---

## 2. LINE の設定

### 2-1. Messaging API チャネル

LINE Developers コンソールで、LINE 公式アカウントの Messaging API チャネルを開き、次を設定します。

- 「チャネルシークレット」と「チャネルアクセストークン（長期）」を控える
- 「Webhook の利用」を **オン**（URL は §4 のデプロイ後に設定）
- 「応答メッセージ」を **オフ**、「あいさつメッセージ」は任意
- 「グループトーク・複数人トークへの参加を許可する」を **オン**（グループでの案内・質問に使う）

### 2-2. LINE ログインチャネルと LIFF アプリ

日程調整や「予定と出欠」の画面は LIFF で開きます。

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
| ドライブのフォルダ名の決まり（個人情報を除外する名前、役員向けフォルダの名前） | `app/drive_knowledge.py` |
| 独自ドメイン | `scripts/setup_domains.sh` |

## 4. デプロイ

### 4-1. 公開サービス（LINE Webhook・LIFF・毎時の処理）

```bash
GCP_PROJECT_ID="$PROJECT" ./scripts/deploy.sh
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

同じソースを `ROLE=admin` で、認証なしの呼び出しを許可せずにデプロイし、IAP を有効にします。

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

1. **催促ルールの既定値**を入れる

   ```bash
   curl -X POST "${URL}/admin/policies/seed" \
     -H "X-Admin-Token: $(gcloud secrets versions access latest --secret admin-api-secret)"
   ```

2. **会員名簿**を取り込む（個人情報です。ファイルはリポジトリの外か、git に入らない `data/` に置く）

   ```bash
   .venv/bin/python scripts/import_roster.py path/to/会員名簿.xlsx --sheet <シート名>          # 確認だけ
   .venv/bin/python scripts/import_roster.py path/to/会員名簿.xlsx --sheet <シート名> --upsert # 投入
   ```

3. **年度の組織図**（役職・委員会・所属）を取り込む

   ```bash
   .venv/bin/python scripts/import_org.py path/to/組織図.json          # 確認だけ
   .venv/bin/python scripts/import_org.py path/to/組織図.json --apply  # 投入
   ```

4. **リッチメニュー**を作る

   ```bash
   .venv/bin/python scripts/setup_rich_menu.py --create --liff-id <LIFF ID>
   ```

5. 会員に LINE で友だち追加してもらい、管理画面の「会員」から招待コードを発行して本人と結びつける

---

## 6. 機能ごとの追加設定

使う機能だけ設定します。各スクリプトは何度実行しても同じ結果になるように作っています（冒頭のコメントに前提と使い方があります）。

| 機能 | スクリプト・手順 | 設定する環境変数 |
|---|---|---|
| LINE のデータレイク（会話・ファイルの保存） | `scripts/setup_datalake.sh` | `LINE_EVENTS_TOPIC`・`LINE_CONTENT_BUCKET`・`PUBSUB_PUSH_SA`・`PUBSUB_PUSH_AUDIENCE` |
| 資料・やり取りの検索（RAG） | `scripts/setup_lake_ai.sh`（データレイクの後） | `LAKE_EMBEDDING_MODEL` |
| AI の返信を Webhook の外で作る（推奨） | `scripts/setup_line_reply.sh`（データレイクの後） | `LINE_ASYNC_REPLY=1` |
| Google カレンダー連携 | `scripts/setup_calendar_sync.sh`、カレンダーをサービスアカウントに共有 | `GCAL_CALENDAR_ID` |
| ドライブ資料の取込 | ドライブのフォルダを実行用サービスアカウントに閲覧者で共有 | `DRIVE_KNOWLEDGE_FOLDERS` |
| 委員会資料の編集・議案書の体裁の自動修正 | `scripts/setup_doc_editor.sh <フォルダ ID>` | `COMMITTEE_DOCS_FOLDER`（会員向けと管理画面の両方のサービス） |
| 議案書の作成 | 雛形の Google ドキュメントを用意 | `PLAN_TEMPLATE_DOC` |
| 対外連絡メールの取込 | `scripts/gmail_oauth_setup.py --write-secrets`（LOM 公式 Gmail で同意） | `GMAIL_LABEL`（任意） |
| 障害通知・バックアップ・予算アラート | `BUDGET_EMAIL=<通知先> ./scripts/setup_ops.sh` | — |
| 独自ドメイン | `scripts/setup_domains.sh` | `PUBLIC_BASE_URL` |

詳しい仕組みは各機能の設計書（[docs/README.md](README.md)）にあります。

---

## 7. 動作確認

- `curl ${URL}/health` が 200 を返す
- 自分の LINE で友だち追加 → 招待コードで連携 → リッチメニューが切り替わる
- 管理画面で予定を 1 件作り、自分だけを対象に出欠確認を送る → LINE で回答 → 管理画面に反映される
- 緊急停止の手順（[runbook.md](runbook.md) §4）を一度試しておく
