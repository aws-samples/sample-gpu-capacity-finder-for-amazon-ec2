#!/usr/bin/env bash
# Deploys the GPU Capacity Finder stack with the account and Region from your AWS environment.
#
#   AWS_PROFILE=<profile> AWS_REGION=<region> ./deploy.sh
#
# Extra arguments go to "cdk deploy", for example: ./deploy.sh -c testLaunchAllowSpot=false
# Set ASSUME_YES=1 to skip this script's own confirmation prompt. The cdk command still asks you to
# approve IAM changes; add "--require-approval never" after the script name to skip that prompt too
# (the arguments pass through to "cdk deploy").
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required tool: $1" >&2; exit 1; }
}
need python3
need aws
need cdk

cdk_log="$(mktemp)"
trap 'rm -f "$cdk_log"' EXIT

# Python environment for the CDK app.
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --disable-pip-version-check -r requirements.txt

# Show where the stack goes before anything is created.
identity="$(aws sts get-caller-identity --query '[Account,Arn]' --output text)" || {
  echo "Could not read AWS credentials. Set AWS_PROFILE or sign in first." >&2
  exit 1
}
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [ -z "$region" ]; then
  echo "Set AWS_REGION to the Region where the stack should deploy." >&2
  exit 1
fi
export AWS_REGION="$region"

echo "CDK CLI : $(cdk --version)"
echo "Account : $(printf '%s' "$identity" | cut -f1)"
echo "Identity: $(printf '%s' "$identity" | cut -f2)"
echo "Region  : $region"

if [ "${ASSUME_YES:-}" != "1" ]; then
  read -r -p "Deploy to this account and Region? [y/N] " answer
  case "$answer" in
    y | Y) ;;
    *) echo "Cancelled."; exit 1 ;;
  esac
fi

# Keep stdout on the terminal so cdk can ask for approval; copy stderr to a log file for the hint.
if ! { cdk deploy "$@" 2>&1 1>&3 | tee "$cdk_log" >&2; } 3>&1; then
  echo >&2
  if grep -q "not compatible" "$cdk_log"; then
    echo "Deploy did not finish. The CDK CLI is not compatible with the CDK library." >&2
    echo "Upgrade the CLI with: npm install -g aws-cdk@latest" >&2
  else
    echo "Deploy did not finish. See the messages above." >&2
  fi
  exit 1
fi

# Find the stack name (the default, or the value passed with -c stackName=...).
stack_name="gpu-capacity-finder"
for arg in "$@"; do
  case "$arg" in
    stackName=*) stack_name="${arg#stackName=}" ;;
  esac
done

site_url="$(aws cloudformation describe-stacks --stack-name "$stack_name" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue" --output text)"
pool_id="$(aws cloudformation describe-stacks --stack-name "$stack_name" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)"

cat <<MSG

Site URL     : $site_url
User pool ID : $pool_id

Create your first user:

aws cognito-idp admin-create-user --region $region --user-pool-id $pool_id \\
  --username you@example.com \\
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \\
  --desired-delivery-mediums EMAIL
MSG
