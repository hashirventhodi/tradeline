# Tradeline

A long-only swing trading platform for NSE cash equities. A deterministic, versioned quant strategy proposes trades; an AI layer may only veto or shrink them; a code-only risk engine has the final word; and live execution stays off until measurable gates pass.

**Status: planning complete, no code yet.** This repository currently holds the design: vocabulary, decisions, architecture, phases with acceptance criteria, risk controls, and the research that shaped them.

## Read this first

This system has no demonstrated edge. The baseline strategy is a well-known pullback rule, the AI layer is unproven, and most retail swing strategies do not clear the roughly 0.3 percent round-trip cost of Indian delivery trading. The project exists to find out, with real costs, out-of-sample discipline, and a long paper record, whether either component earns its keep. Failing a gate is an expected and documented outcome. Nothing here is financial advice.

## Documents

| File | What it holds |
|---|---|
| [GLOSSARY.md](GLOSSARY.md) | The canonical vocabulary. "Signal" is banned. |
| [docs/architecture.md](docs/architecture.md) | Components, the daily run, the decision pipeline, authorization boundaries, data model, deployment |
| [docs/phases.md](docs/phases.md) | Six build phases, each with checkable acceptance criteria |
| [docs/risk-controls.md](docs/risk-controls.md) | Limits and defaults, sizing, cost model, Halt and Flatten, gates |
| [docs/research.md](docs/research.md) | What has been demonstrated, what is feasible, India-specific facts, costs, sources |
| [docs/adr/](docs/adr/) | Fifteen architecture decision records, each with the alternatives rejected |

## The design in ten lines

1. Market: NSE cash equities, long-only, holding one to fifteen days. No F&O, no intraday, no other markets in year one.
2. Universe: top 200 by trailing six-month traded value, recomputed monthly from official NSE files, point-in-time to avoid survivorship bias.
3. Strategy: pullback to the 20-day average in stocks above the 200-day average, versioned, immutable once run on the holdout.
4. Decisions after the close at 17:00 IST; entries as after-market limit orders through Zerodha Kite Connect.
5. Paper trading through an internal Paper Broker with real quotes, modelled slippage, and the full Indian cost stack, because Zerodha has no sandbox.
6. AI layer (Claude Haiku 5.5 filtering, Claude Opus 5.5 thesis and skeptic) starts in shadow mode and, if promoted, can only veto or reduce size.
7. Risk engine in code: 1 percent risk per trade, 8 positions, 15 percent per position, 30 percent per sector, no leverage, 3 percent daily halt, 10 percent drawdown halt. Unbypassable.
8. Halt cancels pending orders and blocks entries while exits keep running. Flatten is a separate, confirmed command.
9. Validation: walk-forward plus a locked two-year holdout run once per strategy version. Gates from backtest to paper to live to autonomous are configured and enforced by the system.
10. Infrastructure: Python 3.12 and FastAPI, Postgres, React dashboard served as static files, one EC2 instance in AWS Mumbai with a static IP as SEBI requires, Terraform, GitHub Actions, SSM for secrets, Telegram for the operator.

## Operating cost

Roughly ₹4,000 to ₹6,000 a month: Kite Connect ₹500, a small AWS instance with static IP and backups, and Anthropic API usage at around ten candidates a day. Details in [docs/research.md](docs/research.md).

## Not yet decided

Nothing material. Small technical choices (table partitioning scheme, exact slippage coefficients, test fixture format) will be made in the phase that needs them and recorded as ADRs only if they meet the bar in [docs/adr](docs/adr/).
