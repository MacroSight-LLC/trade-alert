# Development Contributing Guide

## How to Contribute

1. Fork the repository to your own GitHub account. (not needed if you are CUGA team)
2. Create a feature branch from `main` in your fork: `git checkout -b feature/<short-topic>` (see Branch Naming Convention below).
3. Keep PRs small and focused (prefer < ~300 changed lines and limited file count).
4. Follow Conventional Commits for all commits and PR titles.
5. Run formatting, linting, and tests locally before opening a PR.
6. Open a Pull Request from your fork to `main` with a clear description and checklist results.

Notes:
- All PRs are merged using "Squash and merge". The PR title will become the final commit message — write it carefully using the Conventional Commits format.
- Prefer one topic per PR. If your changes touch many areas, split into multiple PRs.

## DCO

This repository requires a Developer's Certificate of Origin 1.1 signoff on every commit. A DCO provides your assurance to the community that you wrote the code you are contributing or have the right to pass on the code that you are contributing. It is generally used in place of a Contributor License Agreement (CLA). You can easily signoff a commit by using the -s or --signoff flag:

```bash
git commit -s -m 'This is my commit message'
```

If you are using the web interface, this should happen automatically. If you've already made a commit, you can fix it by amending the commit and force-pushing the change:

```bash
git commit --amend --no-edit --signoff
git push -f
```

This will only amend your most recent commit and will not affect the message. If there are multiple commits that need fixing, you can try:

```bash
git rebase --signoff HEAD~<n>
git push -f
```

where `<n>` is the number of commits missing signoffs.

## Commit Messages: Conventional Commits

We use the Conventional Commits specification. See the full spec at [conventionalcommits.org](https://www.conventionalcommits.org/en/v1.0.0/).

Structure:

```
<type>[optional scope]: <short description>

[optional body]

[optional footer(s)]
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `build`, `ci`, `perf`, `style`.

Good examples:

```
feat(api): add list-accounts endpoint to registry
fix(browser): prevent crash when page has no active frame
```

Breaking change example:

```
feat(api)!: switch account id field to string

BREAKING CHANGE: API consumers must treat account ids as strings.
```

Bad examples (do not use):

```
update stuff
wip: changes
fixes
typo
```

Why this matters:
- Enables clean history and automated tooling (changelogs, versioning).
- Because we squash-merge, the PR title becomes the final commit — use Conventional Commits in the PR title too.

## Branch Naming Convention

We follow the Conventional Branch specification. See the full spec at [conventional-branch.github.io](https://conventional-branch.github.io/).

### Branch Naming Structure

```
<type>/<description>
```

### Supported Branch Types

| Type         | Good Example                | Why It's Good                   | Bad Example                  | Why It's Bad                      |
| ------------ | --------------------------- | ------------------------------- | ---------------------------- | --------------------------------- |
| Feature      | `feature/add-login-page`    | Lowercase, hyphens, descriptive | `Feature/AddLoginPage`       | Uppercase & no hyphens            |
| Fix          | `bugfix/header-bug`         | Clear, lowercase                | `feat/add_login`             | Uses underscore instead of hyphen |
| Hotfix       | `hotfix/security-patch`     | Clear, proper prefix            | `hotfix#security-patch`      | Contains invalid character `#`    |
| Release      | `release/v1.2.0`            | Correct dot usage for versions  | `release/v1..2.0`            | Consecutive dots                  |
| Chore        | `chore/update-dependencies` | Descriptive and valid           | `chore/update-dependencies-` | Trailing hyphen                   |
| Missing Desc | `feat/issue-123-new-login`  | Includes ticket, traceable      | `feature/`                   | Missing description               |

### Branch Naming Rules

1. **Use lowercase alphanumerics, hyphens, and dots**: Always use lowercase letters (`a-z`), numbers (`0-9`), and hyphens(`-`) to separate words. Avoid special characters, underscores, or spaces. For release branches, dots (`.`) may be used in the description to represent version numbers (e.g., `release/v1.2.0`).
2. **No consecutive, leading, or trailing hyphens or dots**: Ensure that hyphens and dots do not appear consecutively, nor at the start or end of the description.
3. **Keep it clear and concise**: The branch name should be descriptive yet concise, clearly indicating the purpose of the work.
4. **Include ticket numbers**: If applicable, include the ticket number from your project management tool to make tracking easier.

Why this matters:
- **Clear Communication**: The branch name alone provides a clear understanding of its purpose.
- **Automation-Friendly**: Easily hooks into automation processes (e.g., different workflows for `feature`, `release`, etc.).
- **Team Collaboration**: Encourages collaboration by making branch purpose explicit.

## Pull Request Guidelines

- Keep diffs small; avoid drive-by refactors. Separate formatting-only PRs from feature/fix PRs.
- Include a brief summary of what/why, and link related issues (e.g., `Refs: #123`).
- Add/update tests when changing behavior.
- Do not include generated files, large assets, secrets, or local config (e.g., `.env`).
- Ensure CI passes. If you see flaky tests, note it in the PR description.

## CI Workflows

trade-alert uses two CI workflow families:

- [`.github/workflows/trade-alert-tests.yml`](.github/workflows/trade-alert-tests.yml) — gates trade-alert PRs; path-filtered to trade-alert source files; runs unit + integration tests + docker build.
- [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) — runs on every push to `main`; lint → test → build GHCR images; deploy + smoke **only when** repo variable `HETZNER_PROVISIONED=true` (Hetzner not provisioned yet — see [`RELEASING.md`](RELEASING.md)).
- [`.github/workflows/tests.yml`](.github/workflows/tests.yml) / [`.github/workflows/stability-tests.yml`](.github/workflows/stability-tests.yml) — CUGA upstream suite; uses Python 3.12 and Playwright; do not modify for trade-alert changes.

When opening a trade-alert PR, `trade-alert-tests.yml` must be green. The CUGA workflows run independently and are not a blocker for trade-alert merges.

**Branch protection:** `deploy.yml` is independent of the path-filtered PR gate. Direct pushes to `main` skip `trade-alert-tests.yml`. Require PR checks (at minimum `trade-alert-tests.yml`) via branch protection before merging to `main`.

Ruff lint/format targets are centralized in [`.github/ruff-targets.txt`](.github/ruff-targets.txt) and used by both workflows.

Release process: see [`RELEASING.md`](RELEASING.md).

### Pull Request Templates

We provide specific PR templates to help you create well-structured pull requests. When creating a PR, you can use one of these templates by adding the appropriate query parameter to the GitHub URL:

- **Feature PRs**: `?template=feature.md` - For new features and enhancements
- **Bug Fix PRs**: `?template=bugfix.md` - For bug fixes and issue resolutions
- **Documentation PRs**: `?template=docs.md` - For documentation updates and improvements
- **Chore PRs**: `?template=chore.md` - For maintenance tasks, dependency updates, and refactoring

Each template includes:
- Related issue linking
- Type of changes checkboxes
- Testing checklist
- Standard review checklist

GitHub will also automatically suggest these templates when you create a new pull request.

### Pre-PR Checklist (run locally)

Use `uv` for environment and tooling.

```
uv sync --dev
uv run ruff format
uv run ruff check --fix
# Run tests as described below
```

Must:
- If your change touches the browser/env, verify relevant demos still run.
- Update README.md or docs if only needed, discuss before

## Security Scanning

Before committing, run security scanning to detect potential secrets:

```bash
uv pip install --upgrade "git+https://github.com/ibm/detect-secrets.git@master#egg=detect-secrets"
detect-secrets scan --update .secrets.baseline
detect-secrets audit .secrets.baseline
```

If everything passes, no need to mark secrets or false positives. This ensures no sensitive information is accidentally committed to the repository.

## Running Tests

### 1) Install dev dependencies

```bash
uv sync --dev
```


### Run tests

### trade-alert Tests

```bash
# Unit tests (620+ tests, no infrastructure needed)
pytest tests/unit/ -q

# Gate validation tests only
pytest tests/unit/test_validate_and_filter.py -v

# All tests with coverage
pytest tests/unit/ --cov=. --cov-report=term-missing

# Integration smoke test (needs Docker running)
python tests/integration/integration_smoke.py
```

### Reconciliation pipeline (validate_and_filter test fixtures)

Two server-side stages in `validate_and_filter.py` silently transform inputs
in ways that are invisible from the public `validate_and_filter()` signature.
Test fixtures must account for both:

1. **Server-side `sources_agree` reconciliation** — Before gate thresholds
   run, the LLM-claimed `sources_agree` is overwritten with a deterministic
   value from `_aligned_family_count()` over aligned signal families in the
   snapshot, plus optional macro-context injection (`SA_INCLUDE_MACRO_CONTEXT`)
   and forecast confirmation bonus. Tests that hardcode both a `sources_agree`
   value and a snapshot fixture get the LLM value clobbered without warning.
   Derive expected SA from snapshot signal families instead.

2. **High-confidence / SA coupling** — When `confidence >= HIGH_CONFIDENCE_MIN`
   (default `0.85`), `sources_agree` must also be `>= HIGH_CONFIDENCE_MIN_SA`
   (default `5`) or the alert is rejected with `HIGH_CONFIDENCE_ALIGNMENT`.
   High confidence alone does not bypass the SA gate.

Relevant env tunables: `SA_FAMILY_MIN_SCORE`, `SA_INCLUDE_MACRO_CONTEXT`,
`SA_MACRO_CONTEXT_SCORE`, `HIGH_CONFIDENCE_MIN`, `HIGH_CONFIDENCE_MIN_SA`.

Reference fixtures: [`tests/unit/test_validate_and_filter_extended.py`](./tests/unit/test_validate_and_filter_extended.py).

### `gates/` package (validate_and_filter extraction)

Gate helpers live under `gates/` (`regime`, `session`, `dedup`, `watch`, `rr_volume`).
Circuit breaker, Prometheus metrics, and the public `validate_and_filter()` entry point
stay in `validate_and_filter.py`.

New helpers added to `gates/` must be re-exported from `validate_and_filter.py` if any
test patches them via `vf._helper` or imports them from `validate_and_filter`.

Workflow code blocks import via a sandbox allowlist in `pipeline_runner.py`
(`_IMPORT_ALLOWLIST`). When adding a new project module callable from workflow
`code:` steps, add its dotted path to that set (including `gates.*` submodules).

### CUGA Framework Tests (upstream)

```bash
chmod +x ./src/scripts/run_tests.sh
./src/scripts/run_tests.sh
```


## Adding new source files

Any new `.py` file added to the repo root or a tracked subdirectory MUST be added
to the directory layout in `CUGA-Trading-Alert-System-SPEC-v1.3.md` §6 in the
same PR. The SSOT is the single source of truth for what lives in the repo, so a
new module that is missing from §6 will fail review.

`SSOT.md` at the repo root is a symlink to `CUGA-Trading-Alert-System-SPEC-v1.3.md`.
Application Python modules live at the repo root by design; `src/cuga/` is the
upstream CUGA library and must not be edited. CUGA runtime workflows live in
`workflows/`; GitHub Actions CI configs live in `.github/workflows/`.

Design prototypes (mockups, HTML/CSS sketches, visual references) belong in
`docs/`, not at the repo root. The original `design.html` was moved to
[`docs/design.html`](./docs/design.html) for this reason.

## Secrets baseline

This repo uses IBM `detect-secrets` (configured in
[`.pre-commit-config.yaml`](./.pre-commit-config.yaml)) with the baseline file
[`.secrets.baseline`](./.secrets.baseline). When a hook flags a potential secret
that turns out to be a false positive, audit the new finding and regenerate the
baseline:

```bash
uv run detect-secrets scan --baseline .secrets.baseline
# review the diff, then commit
```

| Baseline audit | Date       | Commit SHA |
| -------------- | ---------- | ---------- |
| Last update    | 2026-05-23 | IBM fork `0.13.1+ibm.64.dss` rescan |

Do not commit any of `.env`, `.env.secrets`, `.env.local`, `secrets/`, or any
other file whose name suggests credentials. The relevant `.gitignore` patterns
are intentional; only `.secrets.baseline` is exempt.

## IDE Setup Quick Links

First make sure that your IDE environment is properly configured
[See Python Code Formatting Guide](#python-code-formatting-guide)

# Python Code Formatting Guide
Before every commit make sure to run:
```commandline
ruff format
ruff check --fix
```

### Ruff formatter and linter installation on IDE

#### VS Code
[https://github.com/astral-sh/ruff-vscode](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff)

#### Pycharm
https://docs.astral.sh/ruff/editors/setup/#pycharm

# IDE Debug Mode Setup

## VSCode and PyCharm Debug Mode

**Important**: Select the correct Python interpreter for debugging:

- **VS Code**: Press `Ctrl+Shift+P` → "Python: Select Interpreter" → Choose the `.venv` from your previous setup
- **PyCharm**: Go to Settings → Project → Python Interpreter → Select the uv virtual environment

## Available Configurations

### Demo Mode

For local development and testing:

1. **API Registry Demo** - Runs the API registry server for demo environment

   - Port: 8001
   - Uses: `mcp_servers.yaml`

2. **Cuga Demo** - Runs the main FastAPI server for demo
   - Port: 7860

**To run demo mode:**

1. Start "API Registry Demo" first
2. Then start "Cuga Demo"


## VSCode Instructions

1. Open VS Code's Run and Debug panel
2. Select the desired configuration from the dropdown
3. Start debugging
