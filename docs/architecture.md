# Architecture

Status: planned, nothing built. Vocabulary is defined in [GLOSSARY.md](../GLOSSARY.md); decisions and their reasons are in [docs/adr](adr/).

## Shape in one paragraph

A single Python service with a scheduler runs one Run per trading day after the close. It refreshes the Universe, pulls the day's candles and announcements, lets the Strategy screen for Candidates and write Plans, runs the AI layer in Shadow Mode (or, once promoted, as a veto-and-shrink step), passes every Plan through the Risk Engine, records Verdicts, and hands Passed Plans to the active Broker Adapter: the Paper Broker by default, the Kite Broker only when live mode is on and an Approval exists. Everything is written to an append-only Ledger in Postgres. A React dashboard and a Telegram bot are the Operator's two windows into it.

## Components

```
┌──────────────────────────────── scheduler (APScheduler, IST) ────────────────────────────────┐
│                                                                                              │
│  data/                 universe/            strategy/            ai/              risk/      │
│  ├ kite_candles        ├ universe_rule      ├ pullback_v1        ├ news_filter    ├ limits   │
│  ├ nse_eod (bhavcopy)  └ sector_map         ├ screen → Candidate ├ thesis         ├ engine   │
│  ├ nse_corp_actions                         └ plan   → Plan      ├ skeptic        └ pass     │
│  ├ nse_bse_announce                                              └ shadow_log               │
│  ├ rss_news                                                                                  │
│  └ adjust (own CA adjustment)                                                                │
│                                                                                              │
│  engine/  one event loop; clock + data source are swapped: backtest | paper | live           │
│                                                                                              │
│  broker/                 ledger/                 ops/                                        │
│  ├ adapter (interface)   ├ postgres tables       ├ telegram (alerts, approvals, login link)  │
│  ├ paper_broker          └ append-only writer    ├ health endpoint + heartbeat               │
│  └ kite_broker (flagged)                         └ halt / flatten                            │
│                                                                                              │
│  api/  FastAPI: JSON for the dashboard, serves the built SPA, auth (passkey / TOTP)          │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
          dashboard/  Vite + React + TypeScript, built to static files
```

One repository, two packages: `tradeline` (Python) and `dashboard` (TypeScript). One Docker Compose file: `app`, `postgres`, and a nightly `backup` job.

## Daily Run (paper or live)

Times are IST. Market hours are 09:15 to 15:30.

| Time | Step | Notes |
|---|---|---|
| 08:30 | Session Login prompt | Telegram link to the Operator. No login by 16:30 means Data-Only Mode for the Run. |
| 09:00 | Pre-open check | Pending after-market orders from yesterday are confirmed as accepted or cancelled. |
| 15:45 | Ingest | Kite candles for the Universe, NSE end-of-day file, corporate actions, NSE and BSE announcements, RSS since the last Run. |
| 16:30 | Universe refresh | Monthly on the first trading day; otherwise reuse. Sector map refreshed with it. |
| 17:00 | Run | Screen, Plan, AI layer, Risk Engine, Verdicts, exits for open Positions, Orders to the Broker Adapter. |
| 17:15 | Approvals (live only) | Each Order to the Operator via Telegram and dashboard. Unapproved Orders expire at 08:45 next day. |
| 17:30 | Summary | Telegram digest: Verdicts, Positions, P&L, Limit headroom, anything Blocked and why. |

Exits (stop hit, target hit, time stop) are evaluated in the Run on the day's completed bar and placed as after-market orders for the next open. Intraday stop monitoring is deliberately out of scope for increment one; the Plan's stop distance and the sizing account for overnight gap risk.

## Decision pipeline for one Candidate

```
Universe ──► Strategy.screen ──► Candidate ──► Strategy.plan ──► Plan
                                                                  │
                        ┌─────────────────────────────────────────┤
                        ▼                                         ▼
               ai.thesis (Opus)                          ai.base_rates (code)
                        │                                         │
                        └──────────────► ai.skeptic (Opus) ◄──────┘
                                                │
                                        Critique: keep | shrink | veto
                                                │
                       Shadow Mode: logged only ─┤─ Active: applied to Plan (size down or SKIP)
                                                ▼
                                          Risk Engine ──► Pass | Block
                                                │
                                             Verdict
                                                │
                                   BUY with Pass ──► Broker Adapter
```

Rules that hold in every mode:

- The Strategy is the only component that creates Candidates and Plans.
- The AI layer can lower size or turn BUY into SKIP. It cannot raise size, loosen a stop, or create a Plan.
- The Risk Engine sees the final Plan and is the only issuer of a Pass. A Pass is bound to the exact Plan content and the Run id; any change invalidates it.
- The Broker Adapter refuses any Order without a valid Pass. In live mode it also requires an Approval.
- Every step writes to the Ledger before the next step begins.

## Authorization boundaries

| Capability | Who holds it |
|---|---|
| Broker credentials | Kite Broker only, read from SSM at startup; never logged, never in the Ledger |
| Place a live Order | Kite Broker, only with Pass plus Approval plus live flag on |
| Flip live flag | Operator, by editing server configuration; not reachable from dashboard or Telegram |
| Halt | Operator (dashboard, Telegram) or Risk Engine (Limit breach) |
| Flatten | Operator only, typed confirmation |
| Change Limits or Strategy parameters | Operator, through a versioned configuration change recorded in the Ledger |
| Change prompts | Operator, by committing new prompt files; the Run records the prompt version it used |

## Data model (first cut)

Postgres, plain tables, partitioned where volume justifies it. Append-only tables are enforced by revoking UPDATE and DELETE from the application role.

- `candles_daily`, `candles_minute` (partitioned by month): exactly as received from Kite, never modified. Kite's daily bars turned out to be adjusted by Kite's own rules (see docs/research.md), so the adjusted series is computed from `eod_bhavcopy` and Kite's bars serve as the cross-check; ADR 0006's wording is to be revised.
- `corporate_actions`: from NSE, with the adjustment factor we compute.
- `eod_bhavcopy`: NSE end-of-day file rows, source for the Universe Rule.
- `universe_membership`: symbol, effective month, rank, traded value. Point-in-time.
- `sectors`: NSE industry classification, versioned by date.
- `announcements`, `news_items`: raw text, source, timestamp, symbols matched.
- `strategies`: name, version, parameters, created, holdout_run_at.
- `runs`, `candidates`, `plans`, `theses`, `critiques`, `verdicts`, `passes`, `blocks`: append-only, all keyed by `run_id`.
- `orders`, `fills`, `positions`, `approvals`: append-only event rows; the current Position state is a view.
- `halts`, `config_changes`, `prompt_versions`: append-only.
- `gate_evaluations`: every Gate check, inputs and result.
- There are no separate backtest tables. Every Ledger row carries the Engine Mode and, when it has one, a backtest id, so the Paper Gate's comparison is a filter (ADR 0019). `protocols`, `holdout_attempts`, `universe_rule_validity` and `universe_month_builds` arrive with Phase 2 (#72).

## Engine modes

| Mode | Clock | Data source | Broker Adapter | Writes Ledger |
|---|---|---|---|---|
| backtest | simulated, steps one day | historical tables | Paper Broker with historical opens | yes, tagged backtest |
| paper | wall clock, scheduled | live Kite plus historical | Paper Broker with live quotes | yes |
| live | wall clock, scheduled | live Kite plus historical | Kite Broker | yes |

The Strategy, Risk Engine, sizing, and cost model are identical objects in all three. This is the point of ADR 0002.

## AI layer

- `news_filter` (Claude Haiku 5.5): for each announcement and headline, which Universe symbols it concerns and an event type. Batch API for historical labelling.
- `thesis` (Claude Opus 5.5): given the Plan, recent price context, and the filtered news for the symbol, write the case and list the evidence. Structured output, schema enforced.
- `skeptic` (Claude Opus 5.5, separate prompt lineage): given the Thesis, the Plan, and the Base Rates for this setup, return keep / shrink / veto with reasons and a confidence. Never sees the thesis writer's confidence.
- Every call records model id, prompt version, token usage, latency, and the full structured response in the Ledger.
- Expected cost: around 15K input and 2K output tokens on Opus per Candidate for thesis plus critique, under ₹10 per Candidate at current prices; Haiku filtering is negligible. Ten Candidates a day lands near ₹2,000 a month.

## Observability

Structured JSON logs to stdout, collected by Docker. A `/health` endpoint reports last successful Run, last Session Login, Postgres reachability, and Halt state; a free external uptime service pings it. Telegram carries alerts (Run failed, Limit breached, Halt, login missing) and the daily digest. Sentry free tier for exceptions. No metrics stack in increment one; the Ledger is the metrics source and the dashboard reads it.

## Deployment

Terraform in `infra/`: VPC defaults, one EC2 instance in ap-south-1 (t4g.small class), Elastic IP, security group allowing SSH from the Operator's IP and HTTPS, instance role with read access to the SSM parameter path and write access to the backup bucket, S3 bucket with lifecycle rules. Docker Compose on the instance. GitHub Actions: tests on every push, image build and SSH deploy on tag. The Elastic IP is the address whitelisted with Zerodha.

## Out of scope for increment one

Intraday stop monitoring, F&O, short selling, multiple Strategies running concurrently, multiple Operators, metrics dashboards beyond the Ledger-backed UI, any form of automated parameter change.
