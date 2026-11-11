#!/usr/bin/env bash
# Plan or apply the four dev roots in dependency order from GitHub Actions (ADR 0035).
#
#   scripts/ci/terraform_dev.sh plan    # plan role: summaries + fingerprints
#   scripts/ci/terraform_dev.sh apply   # deploy role: re-plan, check fingerprint, apply
#
# Inputs (environment): AWS_ACCOUNT_ID and TF_STATE_KMS_KEY_ARN (secrets, so GitHub masks them),
# DBT_IMAGE_DIGEST and DATA_LAKE_OPERATOR (repository variables). In apply mode,
# EXPECTED_<ROOT> holds the fingerprint from the plan job, which the reviewer approved.
#
# The repository is public: full plan output and plan files never leave the runner. Only
# resource addresses and actions are printed (scripts/ci/tf_plan_summary.py).
# Bootstrap and the disposable dev/cdc stack are never run from CI.
set -euo pipefail

MODE="${1:?usage: terraform_dev.sh plan|apply}"
[[ "$MODE" == plan || "$MODE" == apply ]] || { echo "mode must be plan or apply" >&2; exit 2; }
: "${AWS_ACCOUNT_ID:?secret AWS_ACCOUNT_ID is not set}"
: "${TF_STATE_KMS_KEY_ARN:?secret TF_STATE_KMS_KEY_ARN is not set}"
: "${DATA_LAKE_OPERATOR:?repository variable DATA_LAKE_OPERATOR is not set (e.g. user/operator)}"
# An empty digest would plan the removal of the state machines (orchestrated = digest != "").
: "${DBT_IMAGE_DIGEST:?repository variable DBT_IMAGE_DIGEST is empty: set it after make dbt-image}"

REGION=ca-central-1
STATE_BUCKET="procurelake-tfstate-${AWS_ACCOUNT_ID}-${REGION}"
ACCOUNT_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}"
ROOTS=(foundation data_platform security observability)
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
OUTPUT="${GITHUB_OUTPUT:-/dev/null}"

make glue-wheel >/dev/null # data_platform uploads it; builds are reproducible (ADR 0025)

total=0
for root in "${ROOTS[@]}"; do
  dir="infrastructure/live/dev/${root}"
  cat >"${dir}/backend.hcl" <<EOF
bucket       = "${STATE_BUCKET}"
region       = "${REGION}"
encrypt      = true
kms_key_id   = "${TF_STATE_KMS_KEY_ARN}"
use_lockfile = true
EOF
  {
    echo "aws_account_id = \"${AWS_ACCOUNT_ID}\""
    [[ "$root" == foundation ]] || echo "state_bucket = \"${STATE_BUCKET}\""
    if [[ "$root" == security || "$root" == data_platform ]]; then
      echo "operator_arns = [\"${ACCOUNT_ARN}:${DATA_LAKE_OPERATOR}\"]"
    fi
    if [[ "$root" == security ]]; then
      # Must match the operator's local tfvars: the settings resource replaces the whole list.
      echo "data_lake_admin_arns = [\"${ACCOUNT_ARN}:${DATA_LAKE_OPERATOR}\", \"${ACCOUNT_ARN}:role/procurelake-github-deploy-dev\"]"
      echo "data_lake_read_only_admin_arns = [\"${ACCOUNT_ARN}:role/procurelake-github-plan\"]"
    fi
  } >"${dir}/terraform.tfvars"

  extra=()
  [[ "$root" == data_platform ]] && extra=(-var "dbt_image_digest=${DBT_IMAGE_DIGEST}")

  terraform -chdir="$dir" init -input=false -backend-config=backend.hcl >/dev/null
  terraform -chdir="$dir" plan -input=false -lock-timeout=5m -out=ci.tfplan "${extra[@]}" >/dev/null
  summary="$(terraform -chdir="$dir" show -json ci.tfplan | python3 scripts/ci/tf_plan_summary.py "$root")"
  changes="$(jq -r .changes <<<"$summary")"
  fingerprint="$(jq -r .fingerprint <<<"$summary")"
  jq -r .markdown <<<"$summary" | tee -a "$SUMMARY" # addresses and actions only
  total=$((total + changes))

  if [[ "$MODE" == plan ]]; then
    echo "${root}=${fingerprint}" >>"$OUTPUT"
    continue
  fi

  expected_var="EXPECTED_$(tr '[:lower:]' '[:upper:]' <<<"$root")"
  expected="${!expected_var:-}"
  if [[ "$fingerprint" != "$expected" ]]; then
    echo "::error::${root}: the plan differs from the one approved (fingerprint ${fingerprint:0:12} != ${expected:0:12}). Re-run the workflow to review the new plan." >&2
    exit 1
  fi
  if [[ "$changes" == 0 ]]; then
    echo "${root}: no changes, nothing to apply"
    continue
  fi
  terraform -chdir="$dir" apply -input=false -lock-timeout=5m ci.tfplan >/dev/null
  echo "${root}: applied ${changes} change(s)"
done

echo "changes=${total}" >>"$OUTPUT"
