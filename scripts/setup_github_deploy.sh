#!/usr/bin/env bash
# GitHub Actions → Cloud Run の自動デプロイ基盤を作る（1回だけ実行すればよい）。
#
# 方式: Workload Identity 連携（鍵レス）。サービスアカウントのJSON鍵は作らない。
#       GitHub の OIDC トークンを GCP が検証し、指定リポジトリの「production 環境」で
#       動くジョブからのみ、デプロイ用サービスアカウントの権限を借用できるようにする。
#       リポジトリは名前ではなく ID で照合する（名前を付け替えた別のリポジトリを通さない）。
#       production 環境には「承認してから実行」（実行した gh のユーザーを承認者にする）と
#       「main からだけ」の決まりを付ける。何度実行してもよい（既存の設定はこの内容に更新する）。
#
# 前提: gcloud がプロジェクトのオーナー相当のアカウントで認証済み。
#       必要な環境変数（GCP_PROJECT_ID など）を設定した状態で、リポジトリ直下で実行する。
#       例: set -a; source .env; set +a
# 使い方: GITHUB_REPO=<オーナー>/<リポジトリ> ./scripts/setup_github_deploy.sh
#
# 実行後、GitHub リポジトリに以下の Secrets を登録する（gh があれば自動登録する）。
#   GCP_WIF_PROVIDER : projects/<番号>/locations/global/workloadIdentityPools/github/providers/github
#   GCP_DEPLOY_SA    : github-deployer@<project>.iam.gserviceaccount.com
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:?GCP_PROJECT_ID を設定してください}"
GITHUB_REPO="${GITHUB_REPO:?GITHUB_REPO（例: your-org/jci-senior-executive-director-agent）を設定してください}"
RUNTIME_SA="app-runtime@${PROJECT}.iam.gserviceaccount.com"
DEPLOY_SA_ID="github-deployer"
DEPLOY_SA="${DEPLOY_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
POOL="github"
PROVIDER="github"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
REPO_ID="$(gh api "repos/${GITHUB_REPO}" -q .id)"
CONDITION="assertion.repository_id == '${REPO_ID}' && assertion.environment == 'production'"

echo ">> project=${PROJECT} (${PROJECT_NUMBER}) repo=${GITHUB_REPO}"

echo ">> 必要なAPIを有効化"
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  --project "${PROJECT}"

echo ">> デプロイ用サービスアカウント"
if ! gcloud iam service-accounts describe "${DEPLOY_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${DEPLOY_SA_ID}" \
    --display-name "GitHub Actions deployer" \
    --project "${PROJECT}"
else
  echo "   既に存在: ${DEPLOY_SA}"
fi

echo ">> 権限付与（Cloud Runデプロイ / イメージpush / ランタイムSAの利用）"
for role in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member "serviceAccount:${DEPLOY_SA}" \
    --role "${role}" \
    --condition=None \
    --quiet >/dev/null
done
# Cloud Run のサービスに app-runtime SA を紐付けるために必要
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member "serviceAccount:${DEPLOY_SA}" \
  --role roles/iam.serviceAccountUser \
  --project "${PROJECT}" \
  --quiet >/dev/null

echo ">> Workload Identity プール"
if ! gcloud iam workload-identity-pools describe "${POOL}" \
  --location=global --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${POOL}" \
    --location=global \
    --display-name "GitHub Actions" \
    --project "${PROJECT}"
else
  echo "   既に存在: ${POOL}"
fi

echo ">> OIDC プロバイダ（このリポジトリの production 環境からのみ許可）"
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
  --location=global --workload-identity-pool="${POOL}" \
  --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --display-name "GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="${CONDITION}" \
    --project "${PROJECT}"
else
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --attribute-condition="${CONDITION}" \
    --project "${PROJECT}" >/dev/null
  echo "   既に存在: ${PROVIDER}（許可の条件を更新: ${CONDITION}）"
fi

echo ">> リポジトリからの権限借用を許可"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  --role roles/iam.workloadIdentityUser \
  --project "${PROJECT}" \
  --quiet >/dev/null

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

echo
echo ">> GitHub の production 環境（承認してから実行・main からだけ）"
REVIEWER_ID="$(gh api user -q .id)"
gh api -X PUT "repos/${GITHUB_REPO}/environments/production" --input - >/dev/null <<JSON
{"reviewers": [{"type": "User", "id": ${REVIEWER_ID}}],
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
if ! gh api "repos/${GITHUB_REPO}/environments/production/deployment-branch-policies" \
  -q '.branch_policies[].name' | grep -qx main; then
  gh api -X POST "repos/${GITHUB_REPO}/environments/production/deployment-branch-policies" \
    -f name=main -f type=branch >/dev/null
fi
echo "   承認者: $(gh api user -q .login)"

echo
echo ">> GitHub Secrets の登録"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh secret set GCP_WIF_PROVIDER --repo "${GITHUB_REPO}" --body "${WIF_PROVIDER}"
  gh secret set GCP_DEPLOY_SA --repo "${GITHUB_REPO}" --body "${DEPLOY_SA}"
  gh variable set GCP_PROJECT_ID --repo "${GITHUB_REPO}" --body "${PROJECT}"
  gh variable set GCP_REGION --repo "${GITHUB_REPO}" --body "${GCP_REGION:-asia-northeast1}"
  echo "   登録しました（Secrets: GCP_WIF_PROVIDER / GCP_DEPLOY_SA、Variables: GCP_PROJECT_ID / GCP_REGION）"
else
  cat <<EOF
   gh が使えないため手動で登録してください:
     gh secret set GCP_WIF_PROVIDER --repo ${GITHUB_REPO} --body "${WIF_PROVIDER}"
     gh secret set GCP_DEPLOY_SA    --repo ${GITHUB_REPO} --body "${DEPLOY_SA}"
     gh variable set GCP_PROJECT_ID --repo ${GITHUB_REPO} --body "${PROJECT}"
     gh variable set GCP_REGION     --repo ${GITHUB_REPO} --body "${GCP_REGION:-asia-northeast1}"
EOF
fi

cat <<EOF

完了。これ以降、main へのマージで CI 成功後にデプロイが始まり、
GitHub の Actions で承認すると本番に反映されます（.github/workflows/deploy.yml）。

  GCP_WIF_PROVIDER = ${WIF_PROVIDER}
  GCP_DEPLOY_SA    = ${DEPLOY_SA}

手動デプロイが必要なときは従来どおり ./scripts/deploy.sh、
または GitHub Actions の Deploy ワークフローを Run workflow で実行。
EOF
