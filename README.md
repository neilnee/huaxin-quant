# Huaxin Quant

Huaxin Quant is a multi-stage stock screening and tracking workflow for identifying companies that pass fundamental filters and are developing actionable price-volume patterns.

The project is script-driven. Markdown files under `instructions/` define model rules and operating constraints; Python scripts under `scripts/` execute the deterministic parts of the workflow.

## Pipeline

```text
Pool screening
  -> Quant pattern filtering
  -> Bloom state tracking
  -> Daily review
  -> Optional valuation and timing tracker
```

Core stages:

- Model 1 Pool: fundamental and industry screening.
- Model 2 Quant: VCP/P2/P3 price-volume state detection.
- Bloom: cross-day state persistence for candidates in formation.
- Daily Review: concise review layer for human follow-up.
- Model 3 Valuation: optional valuation reports and ranking index.
- Model 4 Tracker: optional timing signals for selected names.

## Repository Layout

```text
instructions/      model rule cards
scripts/           executable pipeline scripts
WORKFLOW.md        daily operating workflow
AGENTS.md          Codex project guidance
CLAUDE.md          Claude project guidance
```

Runtime outputs are intentionally not committed:

```text
cache/
pool/
quant/
bloom/
reports/
signals/
refer/
tmp/
```

## Requirements

- Python 3.10+
- Packages in `requirements.txt`
- Access to the financial data tools used by your environment
- Optional DeepSeek-compatible API for LLM review output

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create local environment variables:

```bash
cp .env.example .env
```

Then fill in the values required by your local data provider.

## Environment

Common variables:

```text
MX_APIKEY=...
HUAXIN_XUANGU_SCRIPT=/path/to/mx_xuangu.py
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`MX_APIKEY` and `HUAXIN_XUANGU_SCRIPT` are required for live Model 1 data fetching. `DEEPSEEK_*` variables are optional unless you enable LLM review generation.

## Quick Start

Check script syntax:

```bash
python3 -m py_compile scripts/*.py
```

Run Model 1:

```bash
python3 scripts/run_pool.py
```

Run Model 2 with the latest pool file:

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

Generate Bloom state and daily review:

```bash
python3 scripts/daily_review.py --date <YYMMDD>
```

Run a single-stock quant check:

```bash
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

See `WORKFLOW.md` for the daily operating sequence.

## Data And Secrets

Do not commit `.env` or runtime outputs. The repository only stores source code, rule cards, and project documentation.

Before publishing or sharing a fork, scan for local paths, credentials, runtime CSV/JSON files, and private research notes.

