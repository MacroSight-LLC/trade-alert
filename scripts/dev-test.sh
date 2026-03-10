#!/usr/bin/env bash
# scripts/dev-test.sh — Quick dev loop: lint, type-check, test
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

TRADE_ALERT_FILES=(
    models.py merger.py db.py notifier_and_logger.py
    healthcheck.py outcome_tracker.py normalizers/
)

echo "=== Ruff lint ==="
ruff check "${TRADE_ALERT_FILES[@]}"

echo ""
echo "=== Mypy type-check ==="
mypy "${TRADE_ALERT_FILES[@]}" || true

echo ""
echo "=== Unit tests (with coverage) ==="
pytest tests/unit/ -v --tb=short \
    --cov=. --cov-report=term-missing \
    --cov-include="models.py,merger.py,db.py,notifier_and_logger.py,healthcheck.py,outcome_tracker.py,normalizers/*"

echo ""
echo "=== All checks passed ==="
