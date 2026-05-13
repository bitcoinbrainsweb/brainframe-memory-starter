#!/usr/bin/env bash
set -euo pipefail

# Memory Starter Onboarding Script
# Stores credentials at ~/.config/memory-starter/.env (outside project tree)
# Run: bash scripts/onboard.sh

echo ""
echo "Memory Starter Setup"
echo "===================="
echo ""

# Prerequisites check
for cmd in curl git bash; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "MISSING: $cmd is required. Install it and rerun."
        exit 1
    fi
done
echo "Prerequisites: OK"
echo ""

# Credential capture
CONFIG_DIR="$HOME/.config/memory-starter"
ENV_FILE="$CONFIG_DIR/.env"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ -f "$ENV_FILE" ]]; then
    echo "Found existing credentials at $ENV_FILE"
    read -rp "Overwrite? (y/N): " overwrite
    [[ "$overwrite" != "y" && "$overwrite" != "Y" ]] && echo "Keeping existing credentials." && SKIP_CREDS=true
fi

if [[ -z "${SKIP_CREDS:-}" ]]; then
    echo "Enter your Supabase project URL (e.g. https://xxxx.supabase.co):"
    read -rp "> " SUPABASE_URL
    echo ""
    echo "Enter your Supabase anon key:"
    read -rp "> " SUPABASE_ANON_KEY
    echo ""

    cat > "$ENV_FILE" << EOF
SUPABASE_URL=$SUPABASE_URL
SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
EOF
    chmod 600 "$ENV_FILE"
    echo "Credentials saved to $ENV_FILE"
fi

source "$ENV_FILE"
echo ""

# Run migration
echo "Running database migration..."
MIGRATION_SQL=$(cat "$(dirname "$0")/migrations/001-initial-schema.sql")

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
    "$SUPABASE_URL/rest/v1/rpc/exec_sql" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"query\": $(echo "$MIGRATION_SQL" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")}")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -1)

# Fallback: run via Supabase SQL editor instruction if RPC not available
if [[ "$HTTP_CODE" != "200" ]]; then
    echo ""
    echo "Auto-migration not available (HTTP $HTTP_CODE)."
    echo "Run the migration manually:"
    echo "  1. Open your Supabase project > SQL Editor"
    echo "  2. Paste the contents of scripts/migrations/001-initial-schema.sql"
    echo "  3. Click Run"
    echo ""
    read -rp "Press Enter once migration is done to continue smoke test..."
fi

# Smoke test
echo ""
echo "Running smoke test..."
SMOKE=$(curl -s -o /dev/null -w "%{http_code}" \
    "$SUPABASE_URL/rest/v1/topics?limit=1" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Authorization: Bearer $SUPABASE_ANON_KEY")

if [[ "$SMOKE" == "200" ]]; then
    echo ""
    echo "Setup complete. Your memory system is ready."
    echo ""
    echo "Next step: paste SYSTEM/PROJECT_INSTRUCTIONS_OWNER.md into a new Claude Project."
    echo "Replace {YOUR_GITHUB_USER} and {YOUR_REPO} placeholders with your values."
    echo ""
else
    echo ""
    echo "SMOKE TEST FAILED: Supabase returned HTTP $SMOKE"
    echo "Check your SUPABASE_URL and SUPABASE_ANON_KEY at $ENV_FILE"
    echo "Then rerun: bash scripts/onboard.sh"
    exit 1
fi
