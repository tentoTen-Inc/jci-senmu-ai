# 運用手順（Runbook）

本番環境を運用するときの手順です。どの LOM でも同じ手順で使えるよう、環境ごとの値は次のように書いています。

| 書き方 | 意味 |
|---|---|
| `<URL>` | 公開サービス（`jci-sed-agent`）の URL |
| `<secret>` | Secret Manager の `admin-api-secret` の値（`gcloud secrets versions access latest --secret admin-api-secret`） |
| `<REGION>` | Cloud Run のリージョン（例: `asia-northeast1`） |

立ち上げの手順は [setup.md](setup.md)、設定項目は [configuration.md](configuration.md) を参照してください。
各 LOM の本番の値（URL・チャネル ID など）は、その LOM の運用記録に残します（猪苗代 JC は `docs/inawashiro/operations.md`。公開版には含めない）。

---

## 1. システム構成

| 要素 | 内容 |
|---|---|
| 公開サービス | Cloud Run `jci-sed-agent`。LINE Webhook・LIFF・毎時の処理。実行 SA `app-runtime@…` |
| 管理サービス | Cloud Run `jci-sed-admin`（`ROLE=admin`、IAP）。管理画面 `/app` と管理 API `/api` |
| データ | Firestore（Native） |
| 毎時の処理 | Cloud Scheduler `jci-tick`（毎時 8–21 時 JST、期限 300 秒）→ `POST /tasks/tick`。予約投稿・催促を先に送り、ドライブ取込・索引は最後 |
| シークレット | Secret Manager: `line-channel-secret` / `line-channel-access-token` / `admin-api-secret`（ほか Gmail の OAuth） |

公開: `/`、`/health`、`/line/webhook`（署名検証）、`/liff/*`（LINE ログインで本人確認）、`/files/*`（署名付きリンク）。
保護: `/admin/*`・`/api/*`（IAP または `X-Admin-Token`）、`/tasks/*`（`X-Admin-Token`）。

---

## 2. 立ち上げ時のチェックリスト

- [ ] `/health` が 200 を返す（`curl <URL>/health`）
- [ ] LINE の「Webhook の利用」が **オン**、「応答メッセージ」が **オフ**（`GET /v2/bot/info` の `chatMode` が `bot`）
- [ ] LINE Developers で Webhook の「検証」が成功する
- [ ] 既定の催促ルールを入れた（`GET /admin/policies` に `rp_例会_default` などがある）
- [ ] リッチメニューを作った（`scripts/setup_rich_menu.py --create`）
- [ ] 会員名簿・組織図を取り込んだ（§3）
- [ ] 静音時間・送信数の上限を確認した（`GET /admin/settings`）
- [ ] テスト用の会員で、友だち追加 → 連携 → 出欠回答 → 管理画面に反映、を実機で確かめた
- [ ] テスト用のデータを消した
- [ ] 緊急停止（§4）を一度試した

---

## 3. 主なオペレーション

### デプロイ

`main` にマージすると、GitHub Actions が CI の成功後に両サービスへデプロイします（[.github/workflows/deploy.yml](../.github/workflows/deploy.yml)）。
CI が動かないときの手動デプロイ:

```bash
./scripts/deploy.sh                       # 公開サービス
gcloud run deploy jci-sed-admin --source . --region <REGION>   # 管理サービス（管理画面を変えたとき）
```

### 既定の催促ルールを入れる

```bash
curl -X POST <URL>/admin/policies/seed -H "X-Admin-Token: <secret>"
```

### 対外連絡メールの取込（初回）

```bash
# 1. Gmail API を有効にする
gcloud services enable gmail.googleapis.com
# 2. Cloud コンソールで OAuth クライアントを作り、JSON をダウンロードする
#    「デスクトップ アプリ」なら追加の入力は不要
#    「ウェブ アプリケーション」なら承認済みリダイレクト URI に http://localhost:8765/ を登録
#    ※ 同意画面の公開ステータスが「テスト」のままだと refresh token が 7 日で失効する。
#      続けて使うなら「本番」に切り替える（対象が 1 アカウントなので審査は不要）
# 3. LOM 公式 Gmail で一度だけ同意して refresh token を取る（--write-secrets で Secret Manager に登録）
python scripts/gmail_oauth_setup.py --client-secret-file client_secret_....json --write-secrets
# 4. Gmail 側で「LOM の受信箱 → LOM 公式 Gmail へ転送」と「対外連絡」ラベルを付けるフィルタを作る
# 5. 動作確認（dry_run は保存しない）
curl -X POST <URL>/admin/notices/import-gmail -H "X-Admin-Token: <secret>" \
  -H "Content-Type: application/json" -d '{"dry_run":true}'
```

取込は毎時の `POST /tasks/tick` で動きます。未設定なら取込は飛ばし、ほかの処理はふだんどおり動きます。

### 会員名簿・組織図の取込（個人情報。ファイルは git に入れない）

```bash
python scripts/import_roster.py 会員名簿.xlsx --sheet <シート名>            # 確認だけ
python scripts/import_roster.py 会員名簿.xlsx --sheet <シート名> --upsert   # 投入
python scripts/import_org.py 組織図.json                                   # 確認だけ
python scripts/import_org.py 組織図.json --apply                           # 投入
```

### 会員の LINE 連携

会員がメニューの連携リンクから自分の名前を選んで申請し、専務が管理画面で承認します（[line-link-design.md](line-link-design.md)）。
招待コードで連携する場合:

```bash
curl -X POST <URL>/admin/members/<member_id>/invite -H "X-Admin-Token: <secret>"
```

### 予定の登録・集計・締め

ふだんは管理画面の「予定」から行います。API で行う場合:

```bash
curl -X POST <URL>/admin/events -H "X-Admin-Token: <secret>" -H "Content-Type: application/json" -d '{
  "type":"例会","title":"7月例会","datetime_start":"2026-07-XXT19:00:00",
  "location":"会館","attendance_deadline":"2026-07-XXT23:59:00",
  "target_scope":{"kind":"all","value":[]},"reminder_policy_id":"rp_例会_default","status":"open"}'
curl <URL>/admin/events/<event_id>/summary -H "X-Admin-Token: <secret>"          # 集計
curl -X POST <URL>/admin/events/<event_id>/close -H "X-Admin-Token: <secret>"    # 締め（五役へサマリ）
```

---

## 4. 緊急停止（キルスイッチ）

LINE への自動送信をすぐに止めます。管理画面の「設定」→「キルスイッチ」でも同じです。

```bash
# 今の値
curl <URL>/admin/settings -H "X-Admin-Token: <secret>"
# 止める（kill_switch=true）
curl -X PUT <URL>/admin/settings -H "X-Admin-Token: <secret>" -H "Content-Type: application/json" \
  -d '{"kill_switch":true,"quiet_hours":{"start":"21:00","end":"08:00","tz":"Asia/Tokyo"},"rate_limit":{"per_member_per_day":3,"global_per_min":30}}'
```

> ON の間、LINE への Push はすべて送られない（催促・予約投稿・日程調整の案内・議案のリマインド・対外連絡など）。
> 催促と予約投稿は OFF 後の次の tick で送られる。それ以外（画面のボタンから送ったもの）は送信失敗になるので、OFF 後に送り直す。
> 利用者からのメッセージへの返信は止めない。

---

## 5. ロールバック

```bash
# 直近のリビジョン
gcloud run revisions list --service jci-sed-agent --region <REGION>
# 1 つ前にすべてのトラフィックを戻す（管理サービスは --service jci-sed-admin）
gcloud run services update-traffic jci-sed-agent --region <REGION> --to-revisions <REVISION>=100
```

特定のリビジョンに固定すると、そのあとのデプロイでも新しいリビジョンには流れません。直したら
`gcloud run services update-traffic <サービス名> --region <REGION> --to-latest` で最新に戻します。コードは `main` の squash 履歴を PR 単位で `git revert` できます。

---

## 6. 監視・調査

```bash
# 直近のログ
gcloud run services logs read jci-sed-agent --region <REGION> --limit 100
# tick の手動実行（同じ時間帯にもう動いていれば何もしない。二重送信を防ぐため）
gcloud scheduler jobs run jci-tick --location <REGION>
```

- 送信の記録・エスカレーションは Firestore の `deliveryLogs` / `escalations`。
- 管理画面での操作は `auditLogs` に残る（操作した人のメールアドレス付き）。
- 障害通知・バックアップ・予算アラートは `scripts/setup_ops.sh` で設定する。
- 会員の発言の本文はアプリのログには出さない。調査にはデータレイク（BigQuery の `line_lake`）を使う。

---

## 7. 既知の事項・制約

- `/healthz` は Google Front End が先に応答して 404 を返す。監視には `/health` を使う。
- 静音時間に止めた催促は「次の tick で送る」方式（Cloud Tasks の遅延配信は使っていない）。
- 外部の人（会員以外）向けのアンケートは Google フォームのまま。
- 組織ポリシーで「ドメインで制限された共有」が有効な組織では、公開サービスを誰でも呼べるようにするため、プロジェクトで上書きが要る。
