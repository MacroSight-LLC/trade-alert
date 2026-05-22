# CUGA upstream tests (staging area)

The test modules in this directory exercise CUGA library internals living
under `src/cuga/` — they import from `cuga.backend.*` and have nothing to
do with trade-alert application code. They are **excluded from the
trade-alert CI suite** via a single `--ignore=tests/cuga_upstream/`
flag in [`.github/workflows/trade-alert-tests.yml`](../../.github/workflows/trade-alert-tests.yml)
and are kept here as a staging area until they are merged into the
upstream [`cuga-agent`](https://github.com/cuga-project/cuga-agent) test
tree, at which point this directory should be deleted from this repo.
