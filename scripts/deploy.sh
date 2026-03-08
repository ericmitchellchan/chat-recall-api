#!/usr/bin/env bash
# Deploy chat-recall-api to ECS Fargate
# Usage: ./scripts/deploy.sh [environment]
#
# Environment variables (documented, not required in script):
#   AWS_ACCOUNT_ID - AWS account ID for ECR URI construction
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Docker installed and running
#   - ECR repository created (see setup-ecr.sh)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENVIRONMENT="${1:-prod}"
AWS_REGION="us-west-2"
ECR_REPO="chat-recall-api"
ECS_CLUSTER="chat-recall-${ENVIRONMENT}"
ECS_SERVICE="chat-recall-api"
GIT_SHA="$(git rev-parse --short HEAD)"
TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
}

cleanup() {
  local exit_code=$?
  if [ $exit_code -ne 0 ]; then
    log "ERROR: Deploy failed with exit code ${exit_code}"
    log "Environment: ${ENVIRONMENT}"
    log "Git SHA: ${GIT_SHA}"
  fi
  exit $exit_code
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
log "Starting deployment to ${ENVIRONMENT} (git: ${GIT_SHA})"

# Step 1: Authenticate with ECR
log "Step 1/6: Authenticating with ECR..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Step 2: Build Docker image
log "Step 2/6: Building Docker image..."
docker build -t "${ECR_REPO}:${GIT_SHA}" .

# Step 3: Tag with ECR URI
log "Step 3/6: Tagging image..."
docker tag "${ECR_REPO}:${GIT_SHA}" "${ECR_URI}:${GIT_SHA}"
docker tag "${ECR_REPO}:${GIT_SHA}" "${ECR_URI}:latest"

# Step 4: Push both tags
log "Step 4/6: Pushing images to ECR..."
docker push "${ECR_URI}:${GIT_SHA}"
docker push "${ECR_URI}:latest"

# Step 5: Force new ECS deployment
log "Step 5/6: Forcing new ECS deployment..."
aws ecs update-service \
  --cluster "${ECS_CLUSTER}" \
  --service "${ECS_SERVICE}" \
  --force-new-deployment \
  --region "${AWS_REGION}" \
  > /dev/null

# Step 6: Wait for service stability
log "Step 6/6: Waiting for service to stabilize..."
aws ecs wait services-stable \
  --cluster "${ECS_CLUSTER}" \
  --services "${ECS_SERVICE}" \
  --region "${AWS_REGION}"

log "Deployment complete!"
log "  Environment: ${ENVIRONMENT}"
log "  Image: ${ECR_URI}:${GIT_SHA}"
log "  Timestamp: ${TIMESTAMP}"
