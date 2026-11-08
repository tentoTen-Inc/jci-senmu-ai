#!/usr/bin/env bash
# ドライブの資料・Google フォームを読むためのセットアップ（冪等。何度実行してもよい）。
# 設計: docs/drive-knowledge-design.md、docs/survey-design.md
#
#   Cloud Run (app-runtime) ──権限借用──► drive-reader ──► Drive API / Forms API（読み取りだけ）
#   drive-reader にはプロジェクトの IAM ロールを付けない（読めるのは、ドライブで共有したものだけ）。
#
# 使うところ: ドライブ資料の取込（DRIVE_KNOWLEDGE_FOLDERS）、議案フォルダの読み取り、
#             例会アンケート（Google フォーム）の集計。
# 前提: gcloud がプロジェクトのオーナー相当で認証済み。
#       必要な環境変数（GCP_PROJECT_ID など）を設定した状態で、リポジトリ直下で実行する。
#       例: set -a; source .env; set +a
# 使い方: ./scripts/setup_drive_reader.sh
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?GCP_PROJECT_ID を設定してください}"
READER_NAME="drive-reader"
READER_SA="${READER_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"

echo ">> project=${PROJECT} reader=${READER_SA}"

echo ">> API を有効化（Drive / Forms / 権限借用）"
gcloud services enable drive.googleapis.com forms.googleapis.com iamcredentials.googleapis.com \
  --project "${PROJECT}"

echo ">> 読み取り用のサービスアカウント"
if ! gcloud iam service-accounts describe "${READER_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${READER_NAME}" --project "${PROJECT}" \
    --display-name "ドライブ・フォームの読み取り（共有したものだけ）"
else
  echo "   既に存在: ${READER_SA}"
fi

echo ">> app-runtime に権限借用（トークン作成者）を付与"
gcloud iam service-accounts add-iam-policy-binding "${READER_SA}" --project "${PROJECT}" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/iam.serviceAccountTokenCreator \
  --condition=None --quiet >/dev/null

cat <<EOF

完了しました。残りは次の作業です。

1) ドライブで、AI に読ませたいフォルダを次のアドレスに「閲覧者」で共有する
     ${READER_SA}
   アンケートに使う Google フォームも、同じアドレスに共有する（フォームごと）。
   ※ 名簿・住所録など個人情報の資料は、共有するフォルダに置かないでください。
     名前で除外しているだけです（docs/setup.md「ドライブ資料の取込」を参照）。

2) 読ませるフォルダの ID を、両方のサービスに設定する（既存の設定は保持されます）
     gcloud run services update jci-sed-agent --region <リージョン> --project ${PROJECT} \\
       --update-env-vars DRIVE_KNOWLEDGE_FOLDERS=<フォルダ ID（カンマ区切り）>
     gcloud run services update jci-sed-admin --region <リージョン> --project ${PROJECT} \\
       --update-env-vars DRIVE_KNOWLEDGE_FOLDERS=<フォルダ ID（カンマ区切り）>

サービスアカウントの名前を変えた場合だけ、環境変数 DRIVE_READER_SA に
${READER_SA} の代わりのアドレスを設定してください（既定はこのアドレスです）。
EOF
