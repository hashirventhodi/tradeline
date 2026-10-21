# Tradeline

A long-only swing trading platform for NSE cash equities. A deterministic, versioned quant strategy proposes trades; an AI layer may only veto or shrink them; a code-only risk engine has the final word; and live execution stays off until measurable gates pass.

**Status: Phase 1 in progress.** The design is complete (vocabulary, decisions, architecture, phases with acceptance criteria, risk controls, research) and the walking skeleton is in place: package, migrations, transport, secrets, logging, test harness. `ingest eod` loads NSE end-of-day files and the instrument master (NSE symbol, ISIN, series, listing date, Kite instrument token); `login` performs the daily Kite Session Login and `ingest candles --interval day` loads daily candles from Kite at its rate limit, exactly as returned (Kite's bars are already adjusted by Kite's rules; see [docs/research.md](docs/research.md)). `ingest corporate-actions` loads NSE's official corporate-action list with the split and bonus factors we derive from it, and the query layer serves an adjusted daily series computed on read from NSE's end-of-day rows, as of any date, matching Kite's adjusted chart to the paisa across a bonus. `universe` applies the Universe Rule stated in `tradeline.toml` to a month from the loaded end-of-day files alone, recording the rule version and the ranked membership. `ingest news` loads the RSS feeds listed in `tradeline.toml`, storing each story once and matching it to symbols only on an exact symbol or company-name mention. `ingest announcements` loads each day's NSE and BSE corporate announcements with their official (dissemination) timestamps, matched to NSE symbols only where the match is certain, and the query layer returns them for a symbol as of any moment. `sectors` loads NSE's sector classification (from the NIFTY Total Market constituent list) dated by the day it is loaded, and `verify-universe` reports the Universe's overlap with NSE's current NIFTY 200, both difference lists, and any member without a sector. Nothing trades.

## Getting started

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env            # fill in the Kite values; DATABASE_URL already points at Compose
make db                         # docker compose up -d --wait postgres (host port 55432)
make migrate                    # uv run tradeline migrate
uv run tradeline ingest eod --from 2024-07-01 --to 2024-07-31   # NSE end-of-day files + instrument master
uv run tradeline login                                          # daily: prints the Kite login URL, stores the access token in .env
uv run tradeline ingest candles --interval day --symbol INFY --from 2024-07-01 --to 2024-07-31
uv run tradeline ingest corporate-actions --from 2024-07-01 --to 2024-12-31   # NSE's list, with our factors
uv run tradeline universe --month 2024-08                       # needs 126 trading days of end-of-day files before the month
uv run tradeline ingest candles --interval minute --from 2024-07-01 --to 2024-07-31   # the current month's Universe; --universe-month YYYY-MM for another
uv run tradeline sectors                                        # NSE's sector classification, in force from today
uv run tradeline verify-universe                                # this month's Universe against the NIFTY 200 (a report)
uv run tradeline ingest news   # every feed in [news.feeds]; --feed NAME for one
uv run tradeline ingest announcements --from 2024-07-23   # NSE and BSE filings, matched to symbols
make check                      # ruff, mypy, pytest against a throwaway database
```

Settings that are not secret live in `tradeline.toml`. Secrets come from `.env` in development and the process environment in production; see `src/tradeline/secrets.py`. Tests run under `TRADELINE_ENV=test`, where the live transport refuses to be constructed and `pytest-socket` blocks every non-loopback socket; external responses come from fixtures recorded with `RecordingTransport` and served by `PlaybackTransport` (`src/tradeline/transport.py`). Every `ingest` subcommand, `sectors`, and `verify-universe` take `--record-fixtures DIR` to capture live responses and `--play-fixtures DIR` to replay them (`login` only plays back: a recording would hold the API key and a live request token). The committed fixtures under `tests/fixtures/` are real responses trimmed to a few symbols, except the login token exchange, which is hand-written with placeholder credentials because a real one would hold account details. Reads go through `src/tradeline/queries.py`, the only read path for later phases and for tests.

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
