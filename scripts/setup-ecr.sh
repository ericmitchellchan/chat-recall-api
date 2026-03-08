#!/usr/bin/env bash
# One-time ECR repository setup
# Usage: ./scripts/setup-ecr.sh
#
# Creates the ECR repository for chat-recall-api if it does not already exist
# and configures a lifecycle policy to keep the last 10 images.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AWS_REGION="us-west-2"
ECR_REPO="chat-recall-api"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
}

# ---------------------------------------------------------------------------
# Create repository
# ---------------------------------------------------------------------------
log "Creating ECR repository '${ECR_REPO}' (if not exists)..."

REPO_URI=$(aws ecr describe-repositories \
  --repository-names "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --query 'repositories[0].repositoryUri' \
  --output text 2>/dev/null) || true

if [ -z "${REPO_URI}" ] || [ "${REPO_URI}" = "None" ]; then
  REPO_URI=$(aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --query 'repository.repositoryUri' \
    --output text)
  log "Repository created: ${REPO_URI}"
else
  log "Repository already exists: ${REPO_URI}"
fi

# ---------------------------------------------------------------------------
# Lifecycle policy - keep last 10 images
# ---------------------------------------------------------------------------
log "Setting lifecycle policy (keep last 10 images)..."

LIFECYCLE_POLICY='{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Keep only the last 10 images",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": {
        "type": "expire"
      }
    }
  ]
}'

aws ecr put-lifecycle-policy \
  --repository-name "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --lifecycle-policy-text "${LIFECYCLE_POLICY}" \
  > /dev/null

log "Lifecycle policy applied."

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "ECR Repository URI: ${REPO_URI}"
echo ""
echo "To authenticate Docker with this repository:"
echo "  aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin \$(echo ${REPO_URI} | cut -d/ -f1)"
