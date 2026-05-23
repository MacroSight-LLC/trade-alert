# Releasing trade-alert

This document describes how version numbers, GitHub releases, and production deploys relate.

## Version source of truth

The canonical version lives in [`pyproject.toml`](pyproject.toml) (`version = "X.Y.Z"`). [`CHANGELOG.md`](CHANGELOG.md) records user-facing changes per release using [Keep a Changelog](https://keepachangelog.com/) format.

Production deploys are **not** tag-gated: every push to `main` triggers [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), which lints, tests, and builds GHCR images. **SSH deploy and smoke only run when the Hetzner VPS is provisioned** (see below).

## Hetzner deploy target (planned)

The production deploy path targets a **Hetzner VPS**, but **no server is provisioned yet**. Until it is:

- **lint**, **test**, and **build** (GHCR push) jobs run on every push to `main`.
- **deploy** and **smoke** are **skipped** when repository variable `HETZNER_PROVISIONED` is not `true` (default: unset or `false`).
- A **deploy-preflight** job emits a workflow warning instead of failing the pipeline.

When the server is ready, complete the checklist in [`SETUP_AND_OPERATIONS.md`](SETUP_AND_OPERATIONS.md) § Hetzner provisioning checklist, set the secrets below, then enable deploy:

```bash
gh variable set HETZNER_PROVISIONED --body true --repo MacroSight-LLC/trade-alert
```

### Required GitHub Actions secrets (before first deploy)

| Secret | Purpose |
| ------ | ------- |
| `HETZNER_HOST` | VPS IP address or hostname |
| `HETZNER_SSH_KEY` | Private key for the `deploy` user on the VPS |
| `GHCR_READ_TOKEN` | Fine-grained PAT with **Packages: Read** on `MacroSight-LLC/trade-alert` (used on the VPS for `docker login ghcr.io`) |

### Required repository variable

| Variable | Value | Purpose |
| -------- | ----- | ------- |
| `HETZNER_PROVISIONED` | `true` when ready; omit or `false` until then | Gates `deploy` and `smoke` jobs in `deploy.yml` |

Without these configured, deploy/smoke would fail at SSH or `docker login` (401). The gate prevents alert fatigue while Hetzner is not in use.

## Release workflows

| Workflow | Trigger | What it does |
| -------- | ------- | ------------ |
| [`release-pr.yml`](.github/workflows/release-pr.yml) | Manual `workflow_dispatch` (patch / minor / major) | Bumps `pyproject.toml` + `uv.lock`, opens a `release/vX.Y.Z` PR |
| [`release-tag.yml`](.github/workflows/release-tag.yml) | Merged release PR, or manual dispatch with version | Creates and pushes git tag `vX.Y.Z`, opens GitHub Release with auto-generated notes |
| [`release.yml`](.github/workflows/release.yml) | Push tag `v*` | Publishes the Python package to PyPI via `uv publish` |

### Cutting a release (recommended flow)

1. Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` with the changes for this release.
2. Run **Release PR** workflow (`Actions → Release PR → Run workflow`) and choose bump type:
   - **patch** — bug fixes, CI/docs-only changes
   - **minor** — new features, backward-compatible
   - **major** — breaking changes
3. Review and merge the opened PR (`chore: release vX.Y.Z`).
4. **Release Tag** runs automatically on merge and creates tag `vX.Y.Z` plus a GitHub Release.
5. If PyPI publishing is configured, **Release** workflow publishes on tag push.

### Manual tag (fallback)

Use **Release Tag** workflow dispatch and enter the version (e.g. `0.2.11`) if you need to tag without the release PR flow.

## Deploy vs release

| Event | Result |
| ----- | ------ |
| Push to `main` | CI lints, tests, builds GHCR images; deploys to VPS **only if** `HETZNER_PROVISIONED=true` |
| Git tag `vX.Y.Z` | GitHub Release + optional PyPI publish; **does not** change deploy behavior |

Image tags in GHCR:

- `ghcr.io/MacroSight-LLC/trade-alert/cuga:<sha>`
- `ghcr.io/MacroSight-LLC/trade-alert/mcp:<sha>`
- `ghcr.io/MacroSight-LLC/trade-alert/timesfm:<sha>`
- `ghcr.io/MacroSight-LLC/trade-alert/dashboard:<sha>`

## Production deploy prerequisites

When `HETZNER_PROVISIONED=true`, the deploy job requires the secrets listed in [§ Hetzner deploy target (planned)](#hetzner-deploy-target-planned). The VPS `deploy` user must have Docker installed and read access to the organization's GHCR packages.

## Local / manual deploy

For development or disaster recovery without CI, use [`scripts/deploy.sh`](scripts/deploy.sh), which builds images locally from source. See [`SETUP_AND_OPERATIONS.md`](SETUP_AND_OPERATIONS.md) § Remote / VPS Deployment.

CI deploy uses `docker compose pull` + `--no-build`; manual deploy may still use `docker compose build`.

## PyPI note

[`release.yml`](.github/workflows/release.yml) is inherited from the CUGA upstream template. trade-alert's runtime is container-based; PyPI publish is optional and only relevant if you distribute the Python package separately from Docker images.
