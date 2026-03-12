# Copilot Instructions – trade-alert

This repo is a production trading alert engine built on `cuga-agent`.

## SSOT
All architecture, schemas, file names, and implementation rules are defined in:
**`CUGA-Trading-Alert-System-SPEC-v1.2.md`** at the repo root.

## Rules
- Always read `CUGA-Trading-Alert-System-SPEC-v1.2.md` before generating or editing any code.
- Do not deviate from its architecture, file names, schemas, or workflows.
- Do not modify anything under `src/cuga/` — it is a library dependency.
- Generate only the file explicitly requested. Do not auto-refactor other files.
- Secrets are stored in HashiCorp Vault (`secret/trade-alert`) and loaded at runtime by `vault_env_loader.py`. Never write keys in code, YAML, or `.env`. The `.env` file holds only non-secret tunables and connectivity URLs.
- All Python models must import from `models.py`. No ad-hoc schemas.
- LLM decision agent outputs must be strict JSON matching `PlaybookAlert`.

## Stack Reference
- 10 MCP servers (ports 8001–8010)
- Redis for snapshot queues (TTL 900s)
- Postgres for alert logging (JSONB)
- CUGA YAML workflows (collectors + decisions)
- Claude Sonnet 4 for decision engine
- Discord MCP for output embeds
