---
status: accepted
date: 2026-10-24
---
# One set of Ledger tables, tagged by Engine Mode

`docs/architecture.md` first drew separate `backtest_runs` and `backtest_trades` beside the live tables, with a test to keep the schemas identical. We decided instead that there is one set of append-only Ledger tables (`runs`, `candidates`, `plans`, `passes`, `blocks`, `verdicts`, `orders`, `fills`, `positions`, and the rest), and every row carries the Engine Mode (backtest, paper, live) and, when it has one, a backtest id. ADR 0002 wants the Strategy, Risk Engine, sizing and cost model to run with no code branches on mode; a Ledger writer that picks a table by mode is such a branch, and two schemas kept identical by a test drift the first time the test is skipped. The Paper Gate's comparison of paper with backtest Expectancy becomes a filter on one table rather than a join across pairs. The live Ledger therefore holds backtest volume (a full Protocol writes hundreds of runs), so the tables are partitioned by mode, and a CHECK constraint requires a backtest id exactly when the mode is backtest. Paper dry-run rows (#72) carry a marker so the Paper Gate never counts them.

## Considered options

- **Separate `backtest_*` tables with a schema-equality test.** Rejected: a mode branch in the writer, and the test is the only thing holding the schemas together.
- **Backtest results in their own tables, the live Ledger shape decided in Phase 3.** Rejected: the backtests would be re-run into the Ledger later, and the mode-independence acceptance line could not be tested in Phase 2.
