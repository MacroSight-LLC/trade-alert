"""Bootstrap helpers for trade-alert entrypoints."""


def load_secrets() -> int:
    """Load Vault secrets into os.environ (idempotent)."""
    import vault_env_loader

    return vault_env_loader.load_vault_secrets()
