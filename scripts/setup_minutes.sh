#!/usr/bin/env bash
# 会議の録画から議事録を作る設定（冪等。何度実行してもよい。docs/meeting-minutes-design.md §5.2・§5.8）
#
#   管理画面［議事録を作る］→ Pub/Sub(minutes-jobs)
#        └ Push サブスクリプション minutes-jobs-push → /pubsub/minutes（1回に1段ずつ進める）
#   録画は LOM のドライブから一時置き場（Cloud Storage）に写して Gemini に読ませ、終わったら消す。
#
# 前提: setup_datalake.sh 済み（Push 用 SA・PUBSUB_PUSH_AUDIENCE）、かつ /pubsub/minutes を
#       含むコードがデプロイ済み（main へのマージ後に実行する）。
# 使い方: ./scripts/setup_minutes.sh   （direnv 有効な状態で）
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?GCP_PROJECT_ID を設定してください}"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="${AGENT_SERVICE:-jci-sed-agent}"
BUCKET="${MINUTES_BUCKET_NAME:-${PROJECT}-minutes}"
TOPIC="${MINUTES_TOPIC_NAME:-minutes-jobs}"
SUB="${TOPIC}-push"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
PUSH_SA="pubsub-push@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
VERTEX_AGENT="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
# 一時置き場の録画を自動で消すまでの日数（作業が終われば、その場で消す）
KEEP_DAYS=7

AGENT_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --project "${PROJECT}" --format='value(status.url)')"
AUDIENCE="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --format=json | python3 -c 'import json,sys
for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", []):
    if e.get("name") == "PUBSUB_PUSH_AUDIENCE": print(e.get("value", ""))')"
if [[ -z "${AUDIENCE}" ]]; then
  echo "!! ${SERVICE} に PUBSUB_PUSH_AUDIENCE がありません。先に setup_datalake.sh を実行してください。" >&2
  exit 1
fi
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${AGENT_URL}/pubsub/minutes")"
if [[ "${code}" == "404" ]]; then
  echo "!! /pubsub/minutes がまだデプロイされていません。main へのマージ後に実行してください。" >&2
  exit 1
fi

echo ">> 一時置き場 gs://${BUCKET}（${KEEP_DAYS}日で自動削除・公開しない）"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project "${PROJECT}" --location "${REGION}" \
    --uniform-bucket-level-access --public-access-prevention >/dev/null
else
  echo "   既に存在: gs://${BUCKET}"
fi
LIFECYCLE="$(mktemp)"
trap 'rm -f "${LIFECYCLE}"' EXIT
cat > "${LIFECYCLE}" <<JSON
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": ${KEEP_DAYS}}}]}
JSON
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file "${LIFECYCLE}" >/dev/null
# アプリが録画を写す・消す。Vertex AI（Gemini）が録画を読む
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/storage.objectAdmin >/dev/null
gcloud beta services identity create --service aiplatform.googleapis.com \
  --project "${PROJECT}" >/dev/null 2>&1 || true
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${VERTEX_AGENT}" --role roles/storage.objectViewer >/dev/null

echo ">> Pub/Sub トピック ${TOPIC} と Push サブスクリプション ${SUB} → ${AGENT_URL}/pubsub/minutes"
if ! gcloud pubsub topics describe "${TOPIC}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud pubsub topics create "${TOPIC}" --project "${PROJECT}" >/dev/null
else
  echo "   既に存在: topic ${TOPIC}"
fi
gcloud pubsub topics add-iam-policy-binding "${TOPIC}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/pubsub.publisher \
  --project "${PROJECT}" >/dev/null
if ! gcloud pubsub subscriptions describe "${SUB}" --project "${PROJECT}" >/dev/null 2>&1; then
  # 1段は最長 10 分（録画の読み取り）。失敗のやり直しはアプリが作業の記録で行う
  gcloud pubsub subscriptions create "${SUB}" \
    --topic "${TOPIC}" \
    --push-endpoint "${AGENT_URL}/pubsub/minutes" \
    --push-auth-service-account "${PUSH_SA}" \
    --push-auth-token-audience "${AUDIENCE}" \
    --ack-deadline 600 \
    --min-retry-delay 30s --max-retry-delay 600s \
    --message-retention-duration=1d \
    --expiration-period=never \
    --project "${PROJECT}" >/dev/null
else
  echo "   既に存在: subscription ${SUB}"
fi

# 1段（録画 15 分ぶんの読み取り）が 5 分を超えることがあるので、応答の待ち時間を 10 分にする
echo ">> ${SERVICE}: 設定を追加（既存の環境変数は保持）・待ち時間 600 秒"
gcloud run services update "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --update-env-vars "MINUTES_BUCKET=${BUCKET},MINUTES_TOPIC=${TOPIC}" \
  --timeout 600 --quiet >/dev/null

cat <<MSG

完了しました。管理画面の予定 → 議事録 →［議事録を作る］で、録画の URL を指定して作れます。
元に戻す: gcloud run services update ${SERVICE} --region ${REGION} --project ${PROJECT} --remove-env-vars MINUTES_BUCKET,MINUTES_TOPIC
MSG
