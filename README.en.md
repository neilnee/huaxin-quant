# Huaxin Quant

English | [中文](README.md)

Huaxin Quant is a multi-model stock discovery and tracking system for the A-share market. It combines fundamental screening, VCP price-volume structure detection, Bloom cross-day signal tracking, next-day price/volume setup plans, valuation analysis, and position bookkeeping into a reproducible script-driven workflow.

The system is built around deterministic scripts: `instructions/` defines model rules and operating constraints, `strategies/` stores tunable parameters, and `scripts/` handles data fetching, caching, calculation, output, and state management. LLMs are used only for explanations and report enrichment, not for core structure or buy-signal decisions.

> This project is for research and review only. It is not investment advice.

## Core Strategy

### Model 1: Pool Screening

- Screens the full A-share universe by fundamental quality and removes ST stocks, companies below the market-cap floor, and names that fail basic quality thresholds.
- API-side filtering focuses on revenue growth, net profit growth, gross margin, and R&D intensity.
- Local hard filters cover listing age, profit size, operating cash-flow quality, leverage, and gross-margin floors.
- Industry filtering is blacklist-based, excluding financials, real estate, heavy cyclicals, utilities, low-elasticity consumer areas, healthcare, and other directions outside the current strategy preference.
- Soft tags affect ranking and manual review only; they do not remove stocks that pass hard filters.

### Model 2: Quant Structure And Setups

- Detects VCP stages in the Model 1 pool: `VCP_EARLY`, `VCP_FORMING`, `VCP_MATURE`, and `VCP_TIGHT`.
- VCP quality considers contraction count, narrowing pullback ranges, volume pattern, pivot distance, trend state, and structure validity.
- Three setup types:
  - `PULLBACK_BUY`: low-volume pullback within the structure; early and light-position oriented.
  - `BREAKOUT_BUY`: pivot breakout participation; designed to avoid missing direct breakouts.
  - `RETEST_BUY`: post-breakout retest confirmation; highest certainty, but less frequent.
- Setup scoring combines structure base score, action type score, action quality score, and risk adjustment, then maps to A/B/C/D quality levels.
- Risk flags can downgrade or block setups, including downtrend, overheating, long upper shadows, and volume stalling.

### Model 3: Valuation

- Performs business-line breakdown and valuation anchoring for selected candidates.
- Scripts handle deterministic DCF, PE, and related calculations; LLMs may help explain business lines and generate readable reports.
- Outputs valuation reports, valuation ranges, and ranking indexes for later priority and sizing decisions.

### Bloom Signal Layer

- Consumes Model 2 JSON outputs and maintains a cross-day state pool and lifecycle events.
- Maps Model 2 structures into states such as `EARLY`, `FORMING`, `MATURE`, `TRIGGERED`, `RISK_BLOCKED`, `COOLDOWN`, and `EXIT`.
- The watch list prioritizes triggered setups first, then structure stage, then structure score.
- Bloom reports show structure, setup, risk, current VCP contraction group, and LLM-generated observation notes.

### Signal Plan

- Consumes Model 2 JSON and generates next-session executable price/volume setup plans.
- Three plan types: `PULLBACK` (low-volume pullback), `BREAKOUT` (pivot breakout), `RETEST` (post-breakout retest).
- Outputs concrete trigger price ranges, A/B-class volume thresholds, and invalidation levels — no abstract formulas.
- Distinguishes first-time triggers (`NEW`) from post-trigger continuation (`FOLLOW`).

### Model 4: Tracker

- Orchestrates Bloom + Signal Plan and produces a consolidated daily report ("花期策览").
- Five-section report: overview → focus watch → setup plans → active pool → field reference.
- `scripts/daily.py` for one-command daily pipeline (Model 1 → 2 → 4), `scripts/monitor.py` for real-time progress.

### Position Management

- Maintains local trade records, position snapshots, and rebuilt ledgers.
- Supports initialization, trade imports, manual trade entries, and as-of-date position reconstruction.
- The position module records execution and review data; it does not make trading decisions by itself.

## Development Environment

### Requirements

- Python 3.10+
- macOS / Linux shell environment
- Python packages listed in `requirements.txt`
- Access to financial data APIs or local data tools
- Optional: DeepSeek-compatible API for LLM-generated Bloom and valuation commentary

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Initialize runtime directories:

```bash
python3 scripts/init_runtime.py
```

Check script syntax:

```bash
python3 -m py_compile scripts/*.py
```

### Environment Variables

Copy the example file:

```bash
cp .env.example .env
```

Common variables:

```text
MX_APIKEY=
HUAXIN_XUANGU_SCRIPT=/path/to/mx_xuangu.py
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

- `MX_APIKEY`: primary market-data key used by Model 2, Bloom/Tracker, and related data fetches.
- `HUAXIN_XUANGU_SCRIPT`: local stock-screening tool used by Model 1.
- `DEEPSEEK_*`: optional variables for LLM commentary and report enrichment.

## Common Commands

```bash
# Model 1: fundamental candidate pool
python3 scripts/run_pool.py
python3 scripts/run_pool.py --skip-fetch

# Model 2: VCP structure and setups
python3 scripts/quant_filter.py
python3 scripts/quant_filter.py --date 260707
python3 scripts/quant_filter.py --code 300604 --name 长川科技

# Bloom: cross-day signal pool and daily report
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260707

# Signal Plan: next-day price/volume setup plans
python3 scripts/signal_plan.py
python3 scripts/signal_plan.py --date 260707

# Tracker: unified orchestrator + consolidated daily report
python3 scripts/tracker.py
python3 scripts/daily.py &        # full pipeline, background
python3 scripts/monitor.py         # real-time progress, foreground

# Model 3: valuation
python3 scripts/valuate.py
python3 scripts/valuate.py --code 300442

# Position management
python3 scripts/position.py init
python3 scripts/position.py rebuild --as-of 2026-07-06
```

## Project Structure

```text
instructions/      Model rule cards and operating instructions
strategies/        Strategy parameter JSON files
scripts/           Pipeline scripts and shared utilities
scripts/data/      Market-data and data-source adapters
WORKFLOW.md        Daily operating workflow
DESIGN.md          System design notes
AGENTS.md          Codex engineering instructions
CLAUDE.md          Claude engineering instructions
```

Runtime directories are not committed by default:

```text
cache/             Local daily-bar, screening, and valuation caches
pool/              Model 1 outputs
quant/             Model 2 outputs
bloom/             Bloom reports, state, and events
signal_plan/       Signal Plan setup plans and JSON
tracker/           Consolidated daily report
reports/           Valuation and other reports
position/          Local position ledger
dev_logs/          Local development review logs
.tmp/              Temporary scripts and files
```

## Core Modules

| Module | Entry Script | Purpose |
|--------|--------------|---------|
| Pool | `scripts/run_pool.py` | Fetch and process the fundamental candidate pool |
| Quant | `scripts/quant_filter.py` | Detect VCP structures, risks, and setups |
| Bloom | `scripts/bloom.py` | Maintain the cross-day watch pool and generate reports |
| Signal Plan | `scripts/signal_plan.py` | Generate next-day price/volume setup plans |
| Tracker | `scripts/tracker.py` | Orchestrate Bloom + Plan, produce consolidated report |
| Valuation | `scripts/valuate.py` | Generate valuation reports and ranking outputs |
| Daily | `scripts/daily.py` | One-command full pipeline launcher |
| Position | `scripts/position.py` | Manage trades and position ledgers |
| Shared Data | `scripts/shared.py` | Daily-bar cache, date semantics, and data-source fallback |

Detailed strategy documents:

- [Model 1 Pool](instructions/01-pool.md)
- [Model 2 Quant](instructions/02-quant.md)
- [Model 3 Valuation](instructions/03-valuation.md)
- [Bloom Signal Layer](instructions/signal-bloom.md)
- [Signal Plan](instructions/signal-plan.md)
- [Tracker](instructions/04-tracker.md)
- [Position Management](instructions/signal-position.md)
- [Trading Strategy](instructions/trading-strategy.md)
