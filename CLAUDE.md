# CLAUDE.md – Project Context for Claude

This is the `/trade-alert` project: a production trading alert engine built on `cuga-agent`.

## SSOT
The full architecture, schemas, and implementation rules are in:
**`CUGA-Trading-Alert-System-SPEC-v1.2.md`** at the repo root.

## Rules
- Always read and follow `CUGA-Trading-Alert-System-SPEC-v1.2.md` before generating or editing any code.
- Do not deviate from its architecture, file names, or schemas.
- Do not modify files under `src/cuga/` — treat them as a library.
- No secrets in code or YAML. Secrets are stored in HashiCorp Vault (`secret/trade-alert`) and loaded at runtime by `vault_env_loader.py`. The `.env` file holds only non-secret tunables and connectivity URLs.
- Generate one file at a time, scoped to the section referenced.
