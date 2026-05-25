#!/usr/bin/env bash
# Copy dashboard.html to static/trade-alert/index.html for www.macrosight.net/trade-alert/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_BASE="${TRADE_ALERT_API_BASE:-https://trade-alert-api.macrosight.net}"
DEST="$ROOT/static/trade-alert/index.html"
mkdir -p "$(dirname "$DEST")"
sed "s|<meta name=\"trade-alert-api-base\" content=\"\">|<meta name=\"trade-alert-api-base\" content=\"${API_BASE}\">|" \
  "$ROOT/dashboard.html" > "$DEST"
echo "Wrote $DEST (API base: $API_BASE)"
