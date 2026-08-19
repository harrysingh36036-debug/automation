#!/usr/bin/env bash
# =============================================================================
# GitHub Secrets Setup Script
# =============================================================================
# Run this from the repository root to configure all required secrets.
#
# Usage:
#   chmod +x scripts/setup-secrets.sh
#   ./scripts/setup-secrets.sh
#
# Prerequisites:
#   - GitHub CLI (gh) installed: https://cli.github.com/
#   - Authenticated: gh auth login
# =============================================================================

set -euo pipefail

echo "============================================="
echo "  Supabase → MongoDB Migration Secrets Setup"
echo "============================================="
echo ""

# --- Supabase Secrets (REQUIRED) ---
echo "[1/5] Setting Supabase secrets..."
gh secret set SUPABASE_URL --body "${SUPABASE_URL:-https://YOUR_PROJECT_REF.supabase.co}"
gh secret set SUPABASE_KEY --body "${SUPABASE_KEY:-YOUR_SUPABASE_ANON_OR_SERVICE_KEY}"
gh secret set SUPABASE_DB_HOST --body "${SUPABASE_DB_HOST:-db.YOUR_PROJECT_REF.supabase.co}"
gh secret set SUPABASE_DB_PORT --body "${SUPABASE_DB_PORT:-5432}"
gh secret set SUPABASE_DB_NAME --body "${SUPABASE_DB_NAME:-postgres}"
gh secret set SUPABASE_DB_USER --body "${SUPABASE_DB_USER:-postgres}"
gh secret set SUPABASE_DB_PASSWORD --body "${SUPABASE_DB_PASSWORD:-YOUR_DATABASE_PASSWORD}"
gh secret set SUPABASE_MAX_DB_SIZE_BYTES --body "${SUPABASE_MAX_DB_SIZE_BYTES:-524288000}"
echo "  ✓ Supabase secrets configured"
echo ""

# --- MongoDB Secrets (REQUIRED) ---
echo "[2/5] Setting MongoDB secrets..."
gh secret set MONGODB_URI --body "${MONGODB_URI:-mongodb+srv://USER:PASS@cluster.mongodb.net/?retryWrites=true&w=majority}"
gh secret set MONGODB_DATABASE --body "${MONGODB_DATABASE:-supabase_migration}"
echo "  ✓ MongoDB secrets configured"
echo ""

# --- Threshold & Config Secrets (OPTIONAL) ---
echo "[3/5] Setting threshold & config secrets..."
gh secret set START_THRESHOLD --body "${START_THRESHOLD:-90}"
gh secret set TARGET_THRESHOLD --body "${TARGET_THRESHOLD:-50}"
gh secret set BATCH_SIZE --body "${BATCH_SIZE:-500}"
gh secret set MAX_RETRIES --body "${MAX_RETRIES:-3}"
gh secret set RETRY_DELAY_SECONDS --body "${RETRY_DELAY_SECONDS:-10}"
gh secret set MIGRATION_ENABLED --body "${MIGRATION_ENABLED:-true}"
gh secret set DRY_RUN --body "${DRY_RUN:-false}"
echo "  ✓ Config secrets configured"
echo ""

# --- AI Secrets (OPTIONAL) ---
echo "[4/5] Setting AI secrets..."
gh secret set AI_ENABLED --body "${AI_ENABLED:-false}"
gh secret set AI_PROVIDER --body "${AI_PROVIDER:-}"
gh secret set AI_API_KEY --body "${AI_API_KEY:-}"
gh secret set AI_MODEL --body "${AI_MODEL:-}"
echo "  ✓ AI secrets configured"
echo ""

# --- Verify ---
echo "[5/5] Verifying secrets..."
echo ""
echo "Secrets set for this repository:"
gh secret list
echo ""
echo "============================================="
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Replace placeholder values in GitHub:"
echo "     https://github.com/$(gh repo view --json nameWithOwner -q '.nameWithOwner')/settings/secrets/actions"
echo ""
echo "  2. Update config/tables.json with your tables"
echo ""
echo "  3. Test with dry-run:"
echo "     gh workflow run migration.yml -f dry_run=true"
echo "============================================="
