#!/usr/bin/env bash
# Run Alembic migrations against the production database
# Usage: ./scripts/migrate.sh [revision]
#
# Arguments:
#   revision - Alembic revision to migrate to (default: head)
#
# Required environment variables:
#   DATABASE_URL - PostgreSQL connection string

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REVISION="${1:-head}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL environment variable is required" >&2
  echo "Usage: DATABASE_URL=postgresql://... ./scripts/migrate.sh [revision]" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Migrate
# ---------------------------------------------------------------------------
log "Starting migration to revision: ${REVISION}"

log "Current revision before migration:"
alembic current

log "Running: alembic upgrade ${REVISION}"
alembic upgrade "${REVISION}"

log "Current revision after migration:"
alembic current

log "Migration complete."
