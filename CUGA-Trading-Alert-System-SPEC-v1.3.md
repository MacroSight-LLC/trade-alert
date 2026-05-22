# CUGA‑Trading‑Alert‑System‑SPEC-v1.3.md
**Single Source of Truth | Version 1.3 | March 12, 2026**

> This document is the authoritative specification for the `/trade-alert` repository.
> All AI tools (Claude Opus 4.6 in VS Code, GitHub Copilot, Copilot Agents, etc.) MUST treat this file as the **single source of truth** for architecture, naming, schemas, and workflows.
>
> `SSOT.md` at the repo root is a symlink to this file; either path is canonical.

## Implementation Status

| Phase    | Description                                         | Status        | Tag     |
| -------- | --------------------------------------------------- | ------------- | ------- |
| Phase 1  | Models, Redis collectors                            | ✅ Done        | v0.1.0  |
| Phase 2  | TA collector                                        | ✅ Done        | v0.2.0  |
| Phase 3  | Sentiment, macro collectors                         | ✅ Done        | v0.3.0  |
| Phase 4  | Merger, Postgres DB layer                           | ✅ Done        | v0.4.0  |
| Phase 5  | Decision engine workflows                           | ✅ Done        | v0.5.0  |
| Phase 6  | Notifier, Discord embeds, Postgres log              | ✅ Done        | v0.6.0  |
| Phase 7  | Orchestration, healthcheck, Docker                  | ✅ Done        | v0.7.0  |
| Phase 8  | Outcome tracker, winrate reporting                  | ✅ Done        | v0.8.0  |
| Polish   | Docker fixes, CI, tests, env extraction             | ✅ Done        | v0.8.1  |
| Phase 9  | Dashboard — analytics web UI                        | ✅ Done        | v0.9.0  |
| Phase 10 | Pipeline hardening: Vault, data quality, gate fixes | ✅ Done        | v0.10.0 |

### Architecture Notes

**Collectors are CUGA workflow files, not Python modules.**
They live in `workflows/collector-*.yaml` and are executed by the
CUGA runtime. There are no `collector_*.py` files at the repo root.
To reference collector behavior, read the YAML files directly.
Correct import for integration testing: use `merger.py` and `db.py`
as the Python-importable boundary — not the collectors themselves.

---

## 0. Global Guardrails (Read Me Before Generating Any Code)

### 0.1 Scope Guardrails

1. **Do not change the architecture.**
   The high‑level flow defined here is immutable. You may only implement, refactor, or extend *within* this structure.

2. **Do not modify CUGA core.**
   Files under `src/cuga/` (or equivalent) from the official `cuga-agent` repo are treated as a library and MUST NOT be edited except for configuration hooks if absolutely necessary.

3. **No new schemas without updating this file.**
   All domain models are defined in this spec (`Signal`, `Snapshot`, `PlaybookAlert`). Code MUST NOT introduce alternative or ad‑hoc schemas.

4. **LLM outputs must be strictly JSON where specified.**
   Decision agents may only output JSON structures that validate against `PlaybookAlert`. No free‑form prose.

5. **Resilience first.**
   - All external calls (MCPs, Redis, Postgres) MUST be wrapped with timeouts and retries.
   - Collector and decision workflows must be idempotent within their respective cron windows (15 min or 1 hour).

6. **Secrets and keys.**
   All sensitive values (API keys, passwords, tokens) MUST live in HashiCorp Vault
   at `secret/trade-alert`. The vault_env_loader.py module auto-injects them
   into os.environ at import time with retry + exponential backoff.
   .env contains **only non-secret tunables** (thresholds, URLs, feature flags).
   .env.secrets is the bootstrap input for vault-init.sh and MUST NOT
   be committed. No keys in code or YAML. The Vault dev token MUST match
   `VAULT_DEV_ROOT_TOKEN_ID` in docker-compose.yml.

### 0.2 AI‑Development Guardrails

When using Claude Opus 4.6 or GitHub Copilot:

- Always include:
  > "Use `CUGA‑Trading‑Alert‑System‑SPEC‑v1.3.md` as the single source of truth. Do not add new concepts or deviate from its architecture, schemas, or filenames."

- When generating or editing a file:
  1. Name the target file explicitly.
  2. Reference the relevant section of this spec.
  3. For workflows, say:
     > “Follow the CUGA YAML patterns from the official `cuga-agent` examples but with the tools and prompts from this spec.”

- Never let AI tools “auto‑refactor” across the whole repo. Limit them to the file or function you specify.

---

## 1. Project Overview

Production CUGA‑based trading alert system. **Timer‑driven (15‑minute / 1‑hour cron)** → 12 MCP servers → normalized ensemble signals → Claude Sonnet 4 decision agent → 7‑gate validation → **Discord trading playbook alerts**.

Output per alert:

- Trade direction and timeframe.
- Thesis (1–2 sentence causal explanation).
- Entry, stop, target, implied reward:risk.
- Sentiment context (retail vs institutional).
- Unusual activity (options flow/volume/insider activity).
- Macro regime (risk‑on/off, volatility level).
- Edge probability and confidence.

**Philosophy**

- Ensemble, not oracle: no single MCP is trusted alone.
- Probabilistic synthesis via normalized scores and confidences.
- Type‑safe (Pydantic) interfaces between all components.
- Minimal glue code; majority in YAML workflows and normalizers.

**Success Metrics**

- ≥12 actionable alerts per trading day.
- ≥65% realized winrate for alerts where `edge_probability ≥ 0.70`.

---

## 2. Immutable Architecture

```text
Docker Compose
  → MCP Stack (12)
  → Cron Trigger (every 15 min for 15m pipeline, hourly for 1h pipeline)
  → Parallel CUGA Collector Workflows (7)
  → Redis Snapshot Queues
  → CUGA Decision Workflows (15m & 1h) using Claude Sonnet
  → Discord MCP (rich embeds)
  → Postgres (alerts log)
```

Use the existing architecture diagram as the canonical visual reference. It MUST remain consistent with this description.

---

## 3. MCP Inventory & Integration Best Practices

All MCP services run in Docker, expose `/health`, and are wired into CUGA via its MCP client tooling.

| Port | Service Name       | Key Tools (examples)                                   | Role & Integration Notes                                                                                                                                      |
| ---- | ------------------ | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8001 | TradingView MCP    | `bollinger_scan`, `rsi_scan`                           | Primary TA: uses tradingview-ta (scraping, no API key). Rate-limited to ~10 req/min; use 8–10s inter-symbol delay, max 8 symbols per batch, 15 min cache TTL. |
| 8002 | Polygon MCP        | `unusual_activity`, `aggs`                             | US equities/ETFs: unusual options, volume spikes, aggregate bars. Use symbol batches and query only the screener subset.                                      |
| 8003 | Discord MCP        | `send_rich_embed`                                      | All user‑visible alerts; use a dedicated bot token and channel. Provide structured embed fields, not raw text blobs.                                          |
| 8004 | Finnhub MCP        | `sentiment`, `news_symbol`                             | News + social sentiment by ticker. Prefer their aggregate scores instead of raw headlines for the ensemble.                                                   |
| 8005 | ROT MCP            | `trending_tickers`, `options_flow`                     | Retail options intelligence from Reddit/social. Use their structured outputs (tickers, flow metrics) as signals; do not fetch raw posts.                      |
| 8008 | trading‑mcp server | `screen`, `insiders`                                   | Stock screening, fundamental filters, and insider trades. Use to create a daily/rolling candidate universe and as context, not as a final signal.             |
| 8009 | FRED bundle MCP    | `vix_level`, `yield_curve`                             | Macro regime: volatility, curve slope, risk‑on/off flags. Use in both collectors (macro snapshot) and decision prompts.                                       |
| 8010 | SpamShieldpro MCP  | `classify_text`                                        | Generic spam/bot filter. Apply to any raw text (if ever needed) before sentiment analysis; skip items classified as spam.                                     |
| 8006 | EDGAR MCP          | `form4_filings`, `material_events`                     | SEC EDGAR: Form 4 insider filings and 8-K material events. Feeds `insider_activity` clustering and `catalyst_event` signals via events collector.             |
| 8007 | YFinance MCP       | `short_interest`, `options_chain`, `earnings_calendar` | Yahoo Finance: short interest ratios, options chain snapshots, earnings dates. Feeds `short_interest` and `catalyst_event` signals.                           |
| 8011 | Alpaca MCP         | `intraday_bars`, `volume_profile`                      | Alpaca Markets: real-time intraday bars and volume acceleration. Complements Polygon for `volume_spike` signals.                                              |
| 8012 | TimesFM MCP        | `timesfm_forecast`                                     | Google TimesFM foundation model for short-horizon price forecasting. Feeds the `price_forecast` signal type via the forecast collector and normalizer.        |

**Integration Best Practices (all MCPs)**

- Use short, batched requests per tick (e.g., 20–50 symbols max).
- Respect any documented rate limits by:
    - Caching static data (e.g., fundamentals) daily.
    - Limiting high‑frequency calls (intraday) to TA/flow MCPs.
- Implement retries with backoff; log all MCP failures, but do not abort workflows if one MCP is unavailable—just lower confidence for that signal group.

---

## 4. Core Data Models (Pydantic v2)

**File:** `models.py` (import everywhere).

```python
import math
from typing import Dict, List, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator


class Signal(BaseModel):
    source: str
    type: Literal[
        "technical_trend",
        "volume_spike",
        "sentiment_bull",
        "sentiment_bear",
        "options_flow",
        "insider_activity",
        "relative_strength",
        "macro_risk_off",
        "catalyst_event",
        "short_interest",
        "price_forecast",
    ]
    score: float           # -3.0 (strong negative) to +3.0 (strong positive)
    confidence: float      # 0.0 (low) to 1.0 (high)
    reason: str
    raw: Dict = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not -3.0 <= v <= 3.0:
            raise ValueError(f"score must be between -3.0 and +3.0, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v


class Snapshot(BaseModel):
    symbol: str
    timeframe: Literal["5m", "15m", "1h", "4h", "1D"]
    timestamp: AwareDatetime  # timezone-aware datetime; serialised as ISO 8601 UTC
    signals: List[Signal]

    @field_validator("signals")
    @classmethod
    def validate_signals_non_empty(cls, v: list[Signal]) -> list[Signal]:
        if not v:
            raise ValueError("Snapshot must have at least one Signal")
        return v


class PlaybookAlert(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT", "WATCH"]
    edge_probability: float   # 0-1 inclusive
    confidence: float         # 0-1 inclusive
    timeframe: str            # e.g., "15m"
    thesis: str
    entry: Dict[str, float]   # keys: level, stop, target
    timeframe_rationale: str
    sentiment_context: str
    unusual_activity: List[str]
    macro_regime: str
    sources_agree: int        # number of independent signal types aligned

    @field_validator("edge_probability")
    @classmethod
    def validate_edge_probability(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"edge_probability must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("sources_agree")
    @classmethod
    def validate_sources_agree(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"sources_agree must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def validate_entry(self) -> "PlaybookAlert":
        required = {"level", "stop", "target"}
        missing = required - self.entry.keys()
        if missing:
            raise ValueError(f"entry missing required keys: {missing}")
        for k, val in self.entry.items():
            if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ValueError(f"entry[{k!r}] must be a finite number, got {val!r}")
        level = float(self.entry["level"])
        stop = float(self.entry["stop"])
        target = float(self.entry["target"])
        if self.direction == "LONG" and not (stop < level < target):
            raise ValueError(
                f"LONG entry requires stop < level < target, got "
                f"stop={stop}, level={level}, target={target}"
            )
        if self.direction == "SHORT" and not (target < level < stop):
            raise ValueError(
                f"SHORT entry requires target < level < stop, got "
                f"stop={stop}, level={level}, target={target}"
            )
        return self

    @model_validator(mode="after")
    def validate_edge_vs_confidence(self) -> "PlaybookAlert":
        # Inconsistent: model is highly confident in the edge but very uncertain overall.
        if self.edge_probability > 0.85 and self.confidence < 0.15:
            raise ValueError(
                "edge_probability > 0.85 with confidence < 0.15 is logically inconsistent"
            )
        return self


class TraceAnalysis(BaseModel):
    """Result of post-execution Langfuse trace analysis for self-healing."""

    trace_id: str
    is_healthy: bool
    issues: List[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    llm_calls: int = 0
    total_tokens: int = 0
    prompt_version: str | None = None
    timestamp: AwareDatetime | None = None
```

**Model Guardrails**

- Every `Snapshot` MUST contain at least one `Signal` (enforced by `validate_signals_non_empty`).
- Every `PlaybookAlert.entry` MUST contain keys `level`, `stop`, `target`, all finite numbers, with `stop < level < target` for LONG and `target < level < stop` for SHORT (enforced by the `validate_entry` model validator; WATCH skips the directional check).
- `PlaybookAlert` rejects logically inconsistent combinations where `edge_probability > 0.85` and `confidence < 0.15` (enforced by `validate_edge_vs_confidence`).
- Every alert MUST be a valid `PlaybookAlert` instance before sending to Discord or writing to Postgres.
- LLM JSON outputs MUST be validated against `PlaybookAlert` and rejected on failure (with logging).

---

## 5. Docker Compose & Runtime Topology

**File:** `docker-compose.prod.yml`

Key points:

- Redis and Postgres services as described in v1.1.
- 12 MCP services bound to ports 8001–8012 (8001–8005 original, 8006 EDGAR, 8007 YFinance, 8008 Trading, 8009 FRED, 8010 SpamShield, 8011 Alpaca, 8012 TimesFM).
- `cuga` service built from `docker/Dockerfile.cuga`, mounting:
    - `./workflows` → `/app/workflows`
    - `./normalizers` → `/app/normalizers`
    - `./models.py` → `/app/models.py`
    - `./logs` → `/app/logs`
- `cron` service running `crond` using the `crontab` file.

Cron schedule:

- Every 15 minutes: run the 15m orchestrator (collectors → merger → decision → notifier).
- Every hour: run the 1h orchestrator and `healthcheck.py`.
- Every 15 minutes: run the outcome tracker.

- Every orchestrator MUST end with a `pipeline-summary` step that logs a
  structured JSON object containing: timeframe, per-collector status,
  merger candidate count, alerts fired count, and trace health.

---

## 6. Directory Layout for `/trade-alert`

```
trade-alert/
  src/cuga/                # from upstream cuga-agent (do not modify)[1]
  models.py
  constants.py             # shared enums, thresholds, defaults
  log_config.py            # standard logger configuration
  metrics.py               # Prometheus counters / histograms
  db.py
  merger.py
  validate_and_filter.py
  notifier_and_logger.py
  outcome_tracker.py
  pipeline_runner.py
  healthcheck.py
  discord_bot.py
  vault_env_loader.py
  redis_client.py
  langfuse_client.py
  langfuse_datasets.py
  prompt_manager.py
  decision_helpers.py
  pipeline_tracing.py
  trace_analyzer.py
  alert_quality.py
  winrate_injector.py
  chart_gen.py
  dashboard_api.py
  dashboard.html
  eod_summary.py           # end-of-day Discord ops summary (scheduled via crontab)
  execution_mapper.py      # alert → broker order mapping
  execution_trigger.py     # gated execution trigger
  execution_webhook.py     # inbound execution webhook handler
  normalizers/
    __init__.py
    ta_normalizer.py
    flow_normalizer.py
    sentiment_normalizer.py
    macro_normalizer.py
    events_normalizer.py
    si_normalizer.py
    market_normalizer.py
    forecast_normalizer.py # TimesFM forecast → price_forecast signal
  workflows/
    collector-market.yaml
    collector-ta.yaml
    collector-flow.yaml
    collector-sentiment.yaml
    collector-macro.yaml
    collector-events.yaml
    collector-forecast.yaml
    decision-15m.yaml
    decision-1h.yaml
    notifier.yaml
    orchestrator-15m.yaml
    orchestrator-1h.yaml
    outcome-tracker.yaml
    state-summary.yaml
  docker/
    Dockerfile.cuga
    Dockerfile.mcp
    Dockerfile.timesfm
  docker-compose.prod.yml
  docker-compose.yml
  docker-compose.test.yml  # CI integration-test fixtures (Redis + Postgres only)
  Dockerfile
  schema.sql
  pyproject.toml           # CUGA-inherited; do not edit version / requires-python
  ruff.toml                # CUGA-inherited; do not edit select / line-length
  crontab
  logs/
  data/
    postgres/              # volume
  deployment/
    vault-entrypoint.sh
    vault-config.hcl
    deploy-local.sh
    deploy-local-postgres.sh
  scripts/
    mcp_server.py
    mcp_servers/
    seed_langfuse_prompts.py
    vault-init.sh
  docs/
    design.html            # design prototype, not a runtime artifact
  tests/
    unit/                  # trade-alert unit tests
    integration/           # trade-alert integration tests (need Redis/Postgres)
    system/                # end-to-end system tests
  CHANGELOG.md
  SSOT.md (this file, or symlink to it)
```

**Notes on the directory layout**

- A handful of unit tests in `tests/unit/` (e.g., `test_llm_override.py`, `test_plan_controller_prompt.py`, `test_variables_manager_*`) actually exercise CUGA library internals under `src/cuga/`, not trade-alert code. They are skipped in `trade-alert-tests.yml` via `--ignore` and should be relocated to CUGA's own test tree in a follow-up; the trade-alert repo does **not** host any phantom `variables_manager.py`, `forecast_gate.py`, `llm_override.py`, or `plan_controller_prompt.py` source modules.

---

## 7. Normalizers (MCP → Snapshot)

**Directory:** `normalizers/`

Each normalizer MUST:

- Define a single function:
    
    ```python
    def normalize(raw_results: dict, *, timeframe: str) -> list[Snapshot]:
        ...
    ```
    
- Use the `Signal` and `Snapshot` models from `models.py`.
- Not call MCPs directly (that happens in workflows); only transform results passed from CUGA.

**Mapping Guidelines (non‑negotiable where specified)**

- **TradingView (TA) → `technical_trend` signals:**
    - Use their numerical rating (e.g., −3 strong sell, +3 strong buy) directly as `score`.
    - Map “BB squeeze” and “trend change” patterns into `reason`.
- **Polygon (Flow) → `volume_spike` signals:**
    - Compute `volume_multiple = current_volume / avg_20d_volume`.
    - Map:
        - `1.5 ≤ multiple < 3` → `score = 1.0`
        - `3 ≤ multiple < 5` → `score = 2.5`
        - `multiple ≥ 5` → `score = 3.0`
    - **Important:** The `grouped_daily` endpoint returns only one day of data.
      `avg_volume` MUST be computed as the median volume across all tickers
      returned that day (relative volume rank), NOT set equal to the symbol's
      own volume. Alternatively, use the `aggs` tool for a 20-day lookback
      on the top N candidates.
- **trading-mcp (Market) → `technical_trend` signals:**
    - 24h price change thresholds: `|change| >= 2%` → score ±1.5 (conf 0.65);
      `|change| >= 5%` → score ±2.5 (conf 0.8).
    - Insider activity matching MUST be case-insensitive with aliases:
      "buying"/"purchase" → bull; "selling"/"sale"/"disposition" → bear.
- **ROT → `options_flow` signals:**
    - Large sweeps (≥500 contracts or ≥$1M premium) → score ±2.5 (conf 0.85).
    - Medium sweeps (≥200 contracts or ≥$500K) → score ±2.0 (conf 0.75).
    - Small sweeps (≥50 contracts or ≥$100K) → score ±1.0 (conf 0.60).
    - Put sweeps negate score (bearish). One `options_flow` signal per symbol.
- **trading-mcp (Market) → `insider_activity` signals:**
    - Insider buying → `insider_activity` score +1.5 (conf 0.70).
    - Insider selling → `insider_activity` score −1.5 (conf 0.70).
- **Polygon (Market) → `relative_strength` signals:**
    - RS = symbol % change − SPY % change.
    - Emit when |RS| ≥ 2.0%. Score = clamp(RS, −3.0, +3.0). Confidence = min(|RS|/10, 1.0).
- **Finnhub + ROT → sentiment signals:**
    - If Finnhub sentiment score is on −1..+1:
        - `score = clamp(sentiment * 2.0, -2.0, +2.0)` with `sentiment_bull` or `sentiment_bear`.
    - ROT’s “strong bullish” / “strong bearish” flags can map to ±2.5.
- **FRED bundle → `macro_risk_off` signals:**
    - If VIX > threshold or curve inverted beyond threshold, add `macro_risk_off` with positive `score` for risk-off (i.e., “negative for risk‑on trades”).- **EDGAR MCP → `catalyst_event` signals (via events_normalizer):**
    - Form 4 insider filings: cluster multiple filings for the same symbol within 5 days.
      If ≥ 3 insiders buy → score +2.0 (conf 0.80). Single filing → score ±1.0 (conf 0.60).
    - 8-K material events (e.g., acquisition, guidance revision): score ±2.0 (conf 0.75) based on event type.
- **YFinance MCP → `short_interest` signals (via si_normalizer):**
    - Short interest ratio thresholds:
        - SI% ≥ 25% → score +2.5 (conf 0.85) — extreme squeeze potential.
        - SI% ≥ 15% → score +2.0 (conf 0.75) — elevated squeeze potential.
        - SI% ≥ 10% → score +1.0 (conf 0.60) — notable short interest.
    - YFinance also feeds earnings calendar data to `catalyst_event` via events_normalizer.
- **Alpaca MCP → complements `volume_spike` signals:**
    - Intraday volume acceleration detected via real-time bars.
    - Used by collector-events.yaml alongside Polygon for confirmation volume signals.
If a normalizer cannot confidently determine a signal, it SHOULD omit it rather than fabricate.

---

## 8. Collector Workflows (CUGA YAML)

Each collector follows the template from v1.1, but now with additional best‑practice notes:

- **collector‑market.yaml**
    - Build equity universe:
        - `universe:equities` (top gainers/losers, volume leaders from trading‑mcp + Polygon).
    - Write array of symbols to Redis key.
    - Normalize price changes → `technical_trend`, insider activity → `insider_activity`, relative strength vs SPY → `relative_strength`.
- **collector‑ta.yaml**
    - Read universes from Redis.
    - Call TradingView + trading‑mcp on those symbols/timeframes.
    - Pass raw results to `ta_normalizer.normalize`.
    - Write snapshots to `snapshots:15m` and `snapshots:1h`.
- **collector‑flow.yaml**
    - Call Polygon for equity universe volume data.
    - Pass to `flow_normalizer.normalize`.
- **collector‑sentiment.yaml**
    - For any raw text bodies (if present), call SpamShieldpro `classify_text`; drop results marked as spam.
    - Call Finnhub + ROT for sentiment and options flow.
    - Route ROT options flow data as `rot_options_flow` for `options_flow` signal production.
    - Pass to `sentiment_normalizer.normalize`.
- **collector‑macro.yaml**
    - Call FRED bundle; pass to `macro_normalizer.normalize`.
    - Either:
        - Emit per‑symbol snapshots with macro signals, or
        - Emit one global snapshot object keyed by a dummy symbol (e.g., `__GLOBAL_MACRO__`) that the decision engine can consume.
- **collector‑events.yaml**
    - Call EDGAR MCP for Form 4 insider filings and 8-K material events.
    - Call YFinance MCP for short interest ratios, options chain, and earnings calendar.
    - Call Alpaca MCP for intraday volume acceleration.
    - Pass EDGAR + YFinance results to `events_normalizer.normalize` → `catalyst_event` signals.
    - Pass YFinance short interest data to `si_normalizer.normalize` → `short_interest` signals.
    - Write snapshots to `snapshots:15m` and `snapshots:1h`.

Collectors MUST:

- Use parallel tool calls where available (to reduce latency).
- Limit requests to the relevant universes (avoid scanning entire markets).
- Handle partial failures by skipping broken MCPs without failing the workflow.

---

## 9. Snapshot Merging & Candidate Selection

**File:** `merger.py`

Responsibilities:

- For a given timeframe (e.g., `15m`), read all entries from `snapshots:15m` in Redis.
- Group by `symbol + timeframe`.
- Merge signals from multiple sources into a single `Snapshot` per symbol:
    - Concatenate `signals` lists.
    - Deduplicate identical signals (same `source` and `type`) by highest absolute `score`.

Candidate selection:

- Compute simple aggregate metrics, e.g., sum of absolute scores per symbol.
- Keep only the top N symbols (default N=20) by aggregate strength to pass to the decision workflow, to control LLM context.

---

## 10. Decision Engine (Ensemble Reasoning)

### 10.1 General

Decision workflows are where the ensemble is evaluated. They MUST:

- Use Claude Sonnet 4 (`claude-sonnet-4-20250514`) as `llm_model`.
- Accept merged snapshots + macro regime context.
- Produce an array of `PlaybookAlert` JSON objects or an empty array.

### 10.2 decision‑15m.yaml

The v1.1 decision prompt remains, but in v1.3:

- Add explicit requirement to **return the exact `PlaybookAlert` schema** including `sources_agree`.
- Clarify how `edge_probability` is conceptually computed (e.g., from alignment and strength) while still allowing Sonnet to reason.

Key logic to preserve:

- **Alignment**: count of independent signal families whose weighted mean score points in the same direction.
    Families are: `trend`, `volume`, `sentiment`, `flow`, `events`, `macro`, `positioning` (7 total).
- **Gate**:
    - `edge_probability ≥ 0.70`
        - `sources_agree ≥ 4` (requires at least 4/7 independent families aligned)
        - If confidence is very high (`confidence ≥ 0.85`), require stronger alignment:
            `sources_agree ≥ 5`
    - `average confidence ≥ 0.75`

### 10.3 decision‑1h.yaml

Same as 15m, but:

- Use snapshots from `snapshots:1h`.
- Optionally require `edge_probability ≥ 0.75` to account for longer holding periods.
- Macro regime may weigh more heavily (strong risk‑off can veto otherwise good technical setups).

---

## 11. Discord Notifier & Output

**File:** `notifier.yaml` + Python `notifier_and_logger.py`.

- `notifier_and_logger.py`:
    - Parse `alerts_json` from decision workflow.
    - Validate each against `PlaybookAlert`.
    - Compute reward:risk (R:R).
    - Call Discord MCP `send_rich_embed` with a structured embed matching the spec below.
    - Insert alerts into Postgres using `db.insert_alert`.

**Embed Logical Layout:**

```
🚨 {symbol} {direction} | Edge: {edge_probability as %} | Conf: {confidence as %}

🎯 Trade Playbook
- Thesis: {thesis}
- Entry: ${entry.level} | Stop: ${entry.stop} | Target: ${entry.target} (R:R {rr})

📊 Context
- Timeframe: {timeframe} – {timeframe_rationale}
- Sentiment: {sentiment_context}
- Unusual: {joined unusual_activity}
- Macro: {macro_regime}
- Sources: {sources_agree}/7 aligned
```

Guardrail: Only one embed per alert; no additional commentary.

---

## 12. Postgres Schema & Analytics

**File:** `schema.sql`

The `alerts` table includes `id` (SERIAL), `created_at`, `updated_at` (auto-set on outcome resolution), and all `PlaybookAlert` fields stored as native columns or JSONB.

Analytics to plan for later (not required in v1.3 implementation, but guiding):

- Queries that compute:
    - Winrate by `edge_probability` bucket.
    - Average R:R and realized R:R.
    - Alert frequency over time.

---

## 13. Health & Monitoring

**File:** `healthcheck.py` (repo root)

Behavior:

- Check `/health` on every MCP (uses env-var overrides for each MCP URL, same as `pipeline_runner.py`).
- Check Redis (`PING`) and a minimal Postgres query.
- Log results to `logs/health.jsonl`.
- If more than one critical service is unhealthy, send a diagnostic message to a separate Discord channel via Discord MCP or webhook, clearly labeled as a system alert.

---

## 14. AI Development Workflow (VS Code + Claude Opus + Copilot)

When working phase‑by‑phase:

1. **Open SSOT in VS Code and pin it.**
2. For each phase, use prompts of the form:
    - "Claude, using `CUGA‑Trading‑Alert‑System‑SPEC‑v1.3.md` as SSOT, generate the file `normalizers/ta_normalizer.py` implementing the normalizer contract in section 8. Validate that the function `normalize` returns a list of `Snapshot` models."
3. For Copilot Agent:
    - “Read `SSOT.md` in the root of this repo. For Phase 3 (collectors), help me fill in `workflows/collector-sentiment.yaml` exactly as described there. Do not change any other files.”
4. After each file is generated:
    - Run `mypy`/`pytest` (when available) and any CUGA built‑in workflow validation helpers.
