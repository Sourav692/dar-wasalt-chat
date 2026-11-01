#!/usr/bin/env bash
# One-time GCP setup for CI/CD from GitHub Actions with Workload Identity Federation (no keys).
# Safe to re-run: every create is skipped when the resource exists; IAM bindings are idempotent.
# (An existing provider keeps its old attribute mapping; update it by hand if this file changes.)
#
#   PROJECT=my-project REPO=owner/name ./infra/setup-gcp.sh
#
# Prerequisites: gcloud logged in as a project owner; the Artifact Registry repo "app", the
# bucket "$PROJECT-corpus" and the secret "openrouter-api-key" already exist (see README).
set -euo pipefail

PROJECT=${PROJECT:-dar-wasalt-chat-22ytj}
REPO=${REPO:-Sourav692/dar-wasalt-chat}
REGION=${REGION:-asia-south1}
BUCKET=gs://$PROJECT-corpus
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
POOL=github
PROVIDER=github
RUNTIME_SA=chat-runtime@$PROJECT.iam.gserviceaccount.com   # what the service runs as
DEPLOY_SA=gh-deploy@$PROJECT.iam.gserviceaccount.com       # main-branch deploys
CI_SA=gh-ci@$PROJECT.iam.gserviceaccount.com               # every CI run: read the index
PRINCIPAL=principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL

g() { gcloud --project "$PROJECT" --quiet "$@"; }

for sa in chat-runtime:"Chat service runtime" gh-deploy:"GitHub deploy" gh-ci:"GitHub CI"; do
  id=${sa%%:*}
  g iam service-accounts describe "$id@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    g iam service-accounts create "$id" --display-name "${sa#*:}"
done

# Runtime: read the one secret, nothing else.
g secrets add-iam-policy-binding openrouter-api-key \
  --member "serviceAccount:$RUNTIME_SA" --role roles/secretmanager.secretAccessor >/dev/null

# Deployer: deploy Cloud Run, push images, read the index, act as the runtime account.
g projects add-iam-policy-binding "$PROJECT" --condition=None \
  --member "serviceAccount:$DEPLOY_SA" --role roles/run.admin >/dev/null
g artifacts repositories add-iam-policy-binding app --location "$REGION" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/artifactregistry.writer >/dev/null
g iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/iam.serviceAccountUser >/dev/null
for sa in "$DEPLOY_SA" "$CI_SA"; do
  gcloud storage buckets add-iam-policy-binding "$BUCKET" \
    --member "serviceAccount:$sa" --role roles/storage.objectViewer >/dev/null
done

# Federation: only tokens issued to this repository are accepted at all.
g iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 ||
  g iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
g iam workload-identity-pools providers describe "$PROVIDER" --location global \
  --workload-identity-pool "$POOL" >/dev/null 2>&1 ||
  g iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --display-name "GitHub OIDC" \
    --issuer-uri https://token.actions.githubusercontent.com \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.workflow_ref=assertion.job_workflow_ref" \
    --attribute-condition "assertion.repository == '$REPO'"

# CI (any branch or PR in the repo) may read the index. Deploying needs the job to run from
# ci.yml on main: a new workflow file, or ci.yml on another branch, cannot use the deploy account.
g iam service-accounts add-iam-policy-binding "$CI_SA" --role roles/iam.workloadIdentityUser \
  --member "$PRINCIPAL/attribute.repository/$REPO" >/dev/null
g iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role roles/iam.workloadIdentityUser \
  --member "$PRINCIPAL/attribute.workflow_ref/$REPO/.github/workflows/ci.yml@refs/heads/main" >/dev/null

echo "WIF provider: projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
