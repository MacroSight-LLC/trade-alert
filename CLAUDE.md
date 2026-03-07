# CUGA‑Trading‑Alert‑System‑SPEC-v1.2.md
**Single Source of Truth | Version 1.2 | March 5, 2026**

> This document is the authoritative specification for the `/trade-alert` repository.
> All AI tools (Claude Opus 4.6 in VS Code, GitHub Copilot, Copilot Agents, etc.) MUST treat this file as the **single source of truth** for architecture, naming, schemas, and workflows.

---

## 0. Global Guardrails (Read Me Before Generating Any Code)

### 0.1 Scope Guardrails

1. **Do not change the architecture.**
   The high‑level flow defined here is immutable. You may only implement, refactor, or extend *within* this structure.

2. **Do not modify CUGA core.**
   Files under `src/cuga/` (or equivalent) from the official `cuga-agent` repo are treated as a library and MUST NOT be edited except for configuration hooks if absolutely necessary.[web:76][web:81]

3. **No new schemas without updating this file.**
   All domain models are defined in this spec (`Signal`, `Snapshot`, `PlaybookAlert`). Code MUST NOT introduce alternative or ad‑hoc schemas.

4. **LLM outputs must be strictly JSON where specified.**
   Decision agents may only output JSON structures that validate against `PlaybookAlert`. No free‑form prose.

5. **Resilience first.**
   - All external calls (MCPs, Redis, Postgres) MUST be wrapped with timeouts and retries.
   - Collector and decision workflows must be idempotent in a 5‑minute window.

6. **Secrets and keys.**
   All sensitive values live only in `.env` and Docker environment variables. No keys in code or YAML.

### 0.2 AI‑Development Guardrails

When using Claude Opus 4.6 or GitHub Copilot:

- Always include:
  > “Use `CUGA‑Trading‑Alert‑System‑SPEC‑v1.2.md` as the single source of truth. Do not add new concepts or deviate from its architecture, schemas, or filenames.”

- When generating or editing a file:
  1. Name the target file explicitly.
  2. Reference the relevant section of this spec.
  3. For workflows, say:
     > “Follow the CUGA YAML patterns from the official `cuga-agent` examples but with the tools and prompts from this spec.”[web:81]

- Never let AI tools “auto‑refactor” across the whole repo. Limit them to the file or function you specify.

---

## 1. Project Overview

Production CUGA‑based trading alert system. **Timer‑driven (5‑minute cron)** → 10 MCP servers → normalized ensemble signals → Claude 3.5 Sonnet decision agent → **Discord trading playbook alerts**.

Output per alert:

- Trade direction and timeframe.
- Thesis (1–2 sentence causal explanation).
- Entry, stop, target, implied reward:risk.
- Sentiment context (retail vs institutional).
- Unusual activity (options/volume/orderbook).
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
  → MCP Stack (10)
  → Cron Trigger (every 5 minutes)
  → Parallel CUGA Collector Workflows (5)
  → Redis Snapshot Queues
  → CUGA Decision Workflows (15m & 1h) using Claude Sonnet
  → Discord MCP (rich embeds)
  → Postgres (alerts log)
```

Use the existing architecture diagram as the canonical visual reference. It MUST remain consistent with this description.

---

## 3. MCP Inventory & Integration Best Practices

All MCP services run in Docker, expose `/health`, and are wired into CUGA via its MCP client tooling.[web:81]

| Port | Service Name | Key Tools (examples) | Role & Integration Notes |
| --- | --- | --- | --- |
| 8001 | TradingView MCP | `bollinger_scan`, `rsi_scan` | Primary TA: use for BB squeezes, overbought/oversold, and multi‑timeframe trends. Batch symbols/timeframes to respect rate limits. |
| 8002 | Polygon MCP | `unusual_activity`, `aggs` | US equities/ETFs: unusual options, volume spikes, aggregate bars. Use symbol batches and query only the screener subset. |
| 8003 | Discord MCP | `send_rich_embed` | All user‑visible alerts; use a dedicated bot token and channel. Provide structured embed fields, not raw text blobs. |
| 8004 | Finnhub MCP | `sentiment`, `news_symbol` | News + social sentiment by ticker. Prefer their aggregate scores instead of raw headlines for the ensemble. |
| 8005 | ROT MCP | `trending_tickers`, `options_flow` | Retail options intelligence from Reddit/social. Use their structured outputs (tickers, flow metrics) as signals; do not fetch raw posts. |
| 8006 | crypto‑orderbook MCP | `imbalance`, `depth` | Order book structure: bid/ask imbalance near current price. Use this only for symbols marked as crypto. |
| 8007 | CoinGecko MCP | `top_gainers`, `dominance` | Crypto universe and broad market state. Use to build the crypto symbol list and detect sector rotations. |
| 8008 | trading‑mcp server | `screen`, `insiders` | Stock screening, fundamental filters, and insider trades. Use to create a daily/rolling candidate universe and as context, not as a final signal. |
| 8009 | FRED bundle MCP | `vix_level`, `yield_curve` | Macro regime: volatility, curve slope, risk‑on/off flags. Use in both collectors (macro snapshot) and decision prompts. |
| 8010 | SpamShieldpro MCP | `classify_text` | Generic spam/bot filter. Apply to any raw text (if ever needed) before sentiment analysis; skip items classified as spam. |

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
from pydantic import BaseModel, field_validator
from typing import List, Literal, Dict

class Signal(BaseModel):
    source: str
    type: Literal[
        'technical_trend',
        'volume_spike',
        'sentiment_bull',
        'sentiment_bear',
        'order_imbalance_long',
        'order_imbalance_short',
        'macro_risk_off'
    ]
    score: float          # -3.0 (strong negative) to +3.0 (strong positive)
    confidence: float     # 0.0 (low) to 1.0 (high)
    reason: str
    raw: Dict = {}

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        assert -3.0 <= v <= 3.0
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        assert 0.0 <= v <= 1.0
        return v

class Snapshot(BaseModel):
    symbol: str
    timeframe: Literal['5m', '15m', '1h', '4h', '1D']
    timestamp: str         # ISO 8601 UTC
    signals: List[Signal]

class PlaybookAlert(BaseModel):
    symbol: str
    direction: Literal['LONG', 'SHORT', 'WATCH']
    edge_probability: float  # 0-1 inclusive
    confidence: float        # 0-1 inclusive
    timeframe: str           # e.g., "15m"
    thesis: str
    entry: Dict[str, float]  # keys: level, stop, target
    timeframe_rationale: str
    sentiment_context: str
    unusual_activity: List[str]
    macro_regime: str
    sources_agree: int       # number of independent signal types aligned
```

**Model Guardrails**

- Every `Snapshot` MUST contain at least one `Signal`.
- Every alert MUST be a valid `PlaybookAlert` instance before sending to Discord or writing to Postgres.
- LLM JSON outputs MUST be validated against `PlaybookAlert` and rejected on failure (with logging).

---

## 5. Docker Compose & Runtime Topology

**File:** `docker-compose.prod.yml`

Key points:

- Redis and Postgres services as described in v1.1.
- 10 MCP services bound to ports 8001–8010.
- `cuga` service built from `docker/Dockerfile.cuga`, mounting:
    - `./workflows` → `/app/workflows`
    - `./normalizers` → `/app/normalizers`
    - `./models.py` → `/app/models.py`
    - `./logs` → `/app/logs`
- `cron` service running `crond` using the `crontab` file.

Cron schedule remains:

- Every 5 minutes: run all collector workflows then decision workflows.
- Every hour: run `healthcheck.py`.

---

## 6. Directory Layout for `/trade-alert`

```
trade-alert/
  src/cuga/                # from upstream cuga-agent (do not modify)[1]
  models.py
  normalizers/
    __init__.py
    ta_normalizer.py
    flow_normalizer.py
    sentiment_normalizer.py
    market_normalizer.py
    macro_normalizer.py
  workflows/
    collector-market.yaml
    collector-ta.yaml
    collector-flow.yaml
    collector-sentiment.yaml
    collector-macro.yaml
    decision-15m.yaml
    decision-1h.yaml
    notifier.yaml
    healthcheck.py
  docker/
    Dockerfile.cuga
  docker-compose.prod.yml
  schema.sql
  logs/
  data/
    postgres/              # volume
  crontab
  SSOT.md (this file, or symlink to it)
```

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
- **crypto‑orderbook → `order_imbalance_long` / `order_imbalance_short`:**
    - Compute bid vs ask depth near top levels.
    - Positive imbalance (bids dominate) → long signal; negative → short signal.
    - Normalize to −3..+3 based on % imbalance.
- **Finnhub + ROT → sentiment signals:**
    - If Finnhub sentiment score is on −1..+1:
        - `score = clamp(sentiment * 2.0, -2.0, +2.0)` with `sentiment_bull` or `sentiment_bear`.
    - ROT’s “strong bullish” / “strong bearish” flags can map to ±2.5.
- **FRED bundle → `macro_risk_off` signals:**
    - If VIX > threshold or curve inverted beyond threshold, add `macro_risk_off` with positive `score` for risk-off (i.e., “negative for risk‑on trades”).

If a normalizer cannot confidently determine a signal, it SHOULD omit it rather than fabricate.

---

## 8. Collector Workflows (CUGA YAML)

Each collector follows the template from v1.1, but now with additional best‑practice notes:

- **collector‑market.yaml**
    - Build distinct universes:
        - `universe:equities` (top gainers/losers, volume leaders from trading‑mcp + Polygon).
        - `universe:crypto` (top gainers/losers, high dominance from CoinGecko).
    - Write arrays of symbols to Redis keys.
- **collector‑ta.yaml**
    - Read universes from Redis.
    - Call TradingView + trading‑mcp on those symbols/timeframes.
    - Pass raw results to `ta_normalizer.normalize`.
    - Write snapshots to `snapshots:15m` and `snapshots:1h`.
- **collector‑flow.yaml**
    - Call Polygon and crypto‑orderbook for the same universes.
    - Pass to `flow_normalizer.normalize`.
- **collector‑sentiment.yaml**
    - For any raw text bodies (if present), call SpamShieldpro `classify_text`; drop results marked as spam.
    - Call Finnhub + ROT for sentiment and options flow.
    - Pass to `sentiment_normalizer.normalize`.
- **collector‑macro.yaml**
    - Call FRED bundle; pass to `macro_normalizer.normalize`.
    - Either:
        - Emit per‑symbol snapshots with macro signals, or
        - Emit one global snapshot object keyed by a dummy symbol (e.g., `__GLOBAL_MACRO__`) that the decision engine can consume.

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

- Use Claude 3.5 Sonnet as `llm_model`.
- Accept merged snapshots + macro regime context.
- Produce an array of `PlaybookAlert` JSON objects or an empty array.

### 10.2 decision‑15m.yaml

The v1.1 decision prompt remains, but in v1.2:

- Add explicit requirement to **return the exact `PlaybookAlert` schema** including `sources_agree`.
- Clarify how `edge_probability` is conceptually computed (e.g., from alignment and strength) while still allowing Sonnet to reason.

Key logic to preserve:

- **Alignment**: count of independent signal groups (trend, volume, sentiment, flow, macro) whose weighted mean score points in the same direction.
- **Gate**:
    - `edge_probability ≥ 0.70`
    - `alignment_score ≥ 3`
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
- Sources: {sources_agree}/10 aligned
```

Guardrail: Only one embed per alert; no additional commentary.

---

## 12. Postgres Schema & Analytics

**File:** `schema.sql` (unchanged from v1.1).

Analytics to plan for later (not required in v1.2 implementation, but guiding):

- Queries that compute:
    - Winrate by `edge_probability` bucket.
    - Average R:R and realized R:R.
    - Alert frequency over time.

---

## 13. Health & Monitoring

**File:** `workflows/healthcheck.py`

Behavior:

- Check `/health` on every MCP.
- Check Redis (`PING`) and a minimal Postgres query.
- Log results to `logs/health.jsonl`.
- If more than one critical service is unhealthy, send a diagnostic message to a separate Discord channel via Discord MCP or webhook, clearly labeled as a system alert.

---

## 14. AI Development Workflow (VS Code + Claude Opus + Copilot)

When working phase‑by‑phase:

1. **Open SSOT in VS Code and pin it.**
2. For each phase, use prompts of the form:
    - “Claude, using `CUGA‑Trading‑Alert‑System‑SPEC‑v1.2.md` as SSOT, generate the file `normalizers/ta_normalizer.py` implementing the normalizer contract in section 8. Validate that the function `normalize` returns a list of `Snapshot` models.”
3. For Copilot Agent:
    - “Read `SSOT.md` in the root of this repo. For Phase 3 (collectors), help me fill in `workflows/collector-sentiment.yaml` exactly as described there. Do not change any other files.”
4. After each file is generated:
    - Run `mypy`/`pytest` (when available) and any CUGA built‑in workflow validation helpers.
