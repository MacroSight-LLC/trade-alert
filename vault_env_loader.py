"""Load secrets from HashiCorp Vault into ``os.environ`` at startup.

How it works
------------
1. On **import**, this module checks for ``VAULT_ADDR`` and ``VAULT_TOKEN``
   environment variables.
2. If both are present it connects to the Vault KV v2 engine, reads all
   key-value pairs stored at ``secret/trade-alert`` (configurable via
   ``VAULT_SECRET_PATH``), and injects every pair into ``os.environ``.
3. All existing ``os.getenv()`` calls in pipeline modules (db.py, merger.py,
   notifier_and_logger.py, healthcheck.py, outcome_tracker.py) then find
   the Vault-sourced values automatically — **zero code changes** in those
   modules beyond ``import vault_env_loader``.
4. If Vault is unreachable, not configured, or any error occurs, the loader
   **falls back silently** so local development without Vault still works.

Usage
-----
Add as the **first non-future import** in each pipeline module::

    from __future__ import annotations
    import vault_env_loader  # noqa: F401 — loads Vault secrets

Or run standalone to verify connectivity::

    python vault_env_loader.py          # prints loaded count
    python vault_env_loader.py --export # prints shell export statements
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Path inside Vault's KV v2 engine where trade-alert secrets are stored.
# Override with VAULT_SECRET_PATH env var (default: "trade-alert").
VAULT_SECRET_PATH: str = os.getenv("VAULT_SECRET_PATH", "trade-alert")

# KV v2 mount point (default: "secret").
VAULT_MOUNT: str = os.getenv("VAULT_MOUNT", "secret")

# Guard: only run the loader once per process.
_loaded: bool = False


def load_vault_secrets() -> int:
    """Pull secrets from Vault KV v2 and set them as environment variables.

    Returns:
        Number of secrets successfully injected into ``os.environ``.
        Returns ``0`` when Vault is not configured or unreachable.
    """
    global _loaded  # noqa: PLW0603
    if _loaded:
        return 0

    vault_addr = os.getenv("VAULT_ADDR")
    vault_token = os.getenv("VAULT_TOKEN")

    if not vault_addr or not vault_token:
        logger.debug("VAULT_ADDR/VAULT_TOKEN not set — skipping Vault loader")
        _loaded = True
        return 0

    try:
        import hvac  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("hvac package not installed — cannot load secrets from Vault")
        _loaded = True
        return 0

    try:
        client = hvac.Client(url=vault_addr, token=vault_token)
        if not client.is_authenticated():
            logger.warning("Vault authentication failed — falling back to env vars")
            _loaded = True
            return 0

        resp = client.secrets.kv.v2.read_secret_version(
            path=VAULT_SECRET_PATH,
            mount_point=VAULT_MOUNT,
        )
        data: dict[str, str] = (resp or {}).get("data", {}).get("data", {})

        if not data:
            logger.warning("Vault path %s/%s is empty", VAULT_MOUNT, VAULT_SECRET_PATH)
            _loaded = True
            return 0

        count = 0
        for key, value in data.items():
            # Vault field names are UPPER_CASE env var names by convention.
            env_key = key.upper()
            if value is not None:
                os.environ.setdefault(env_key, str(value))
                count += 1
                logger.debug("Vault → os.environ[%s]", env_key)

        logger.info(
            "Vault loader: injected %d secret(s) from %s/%s",
            count,
            VAULT_MOUNT,
            VAULT_SECRET_PATH,
        )
        _loaded = True
        return count

    except Exception as exc:
        logger.warning("Vault read failed (%s) — falling back to env vars", exc)
        _loaded = True
        return 0


# ---------------------------------------------------------------------------
# Auto-load on import
# ---------------------------------------------------------------------------
_count = load_vault_secrets()

# ---------------------------------------------------------------------------
# CLI mode — verify connectivity or emit shell exports
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    mode = "--export" if "--export" in sys.argv else ("--dotenv" if "--dotenv" in sys.argv else None)

    if mode in ("--export", "--dotenv"):
        # --export  → shell: eval "$(python vault_env_loader.py --export)"
        # --dotenv  → file:  python vault_env_loader.py --dotenv > .env.vault
        _loaded = False
        vault_addr = os.getenv("VAULT_ADDR")
        vault_token = os.getenv("VAULT_TOKEN")
        if not vault_addr or not vault_token:
            print("# VAULT_ADDR or VAULT_TOKEN not set", file=sys.stderr)
            sys.exit(1)
        try:
            import hvac  # type: ignore[import-untyped]

            client = hvac.Client(url=vault_addr, token=vault_token)
            resp = client.secrets.kv.v2.read_secret_version(
                path=VAULT_SECRET_PATH,
                mount_point=VAULT_MOUNT,
            )
            data = (resp or {}).get("data", {}).get("data", {})
            if mode == "--export":
                for key, value in sorted(data.items()):
                    safe_value = str(value).replace("'", "'\\''")
                    print(f"export {key.upper()}='{safe_value}'")
            else:  # --dotenv
                print("# Auto-generated from Vault — do not commit")
                for key, value in sorted(data.items()):
                    print(f"{key.upper()}={value}")
        except Exception as exc:
            print(f"# Vault read failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        if _count > 0:
            print(f"✅ Loaded {_count} secret(s) from Vault")
        else:
            addr = os.getenv("VAULT_ADDR", "(not set)")
            print(f"⚠️  No secrets loaded (VAULT_ADDR={addr})")
