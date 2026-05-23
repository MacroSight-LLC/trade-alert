"""Trade-alert application package (incremental migration from flat root modules)."""

from trade_alert.bootstrap import load_secrets

__all__ = ["load_secrets"]
