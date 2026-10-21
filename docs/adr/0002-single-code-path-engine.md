---
status: accepted
date: 2026-10-21
---
# One event-driven engine runs backtest, paper, and live

We build our own event-driven engine rather than using vectorbt, backtrader, or similar. The Strategy, Risk Engine, sizing, and cost model are the same classes in every mode; only the clock and the data source change. Backtesting libraries would require the Strategy to be written twice, once in the library's idioms and once for production, and that duplication is the main cause of backtest-to-live divergence. A research library may still be used to scan ideas, but nothing advances to a Gate without running on this engine.
