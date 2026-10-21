---
status: accepted
date: 2026-10-21
---
# Price history comes from Kite and NSE only, with our own corporate-action adjustment

Kite Connect candles are stored raw and unadjusted. Splits, bonuses, and other corporate actions are pulled from NSE's official lists and the adjusted series is computed by us, deterministically and auditably. Yahoo Finance's adjusted .NS series was rejected despite being faster to start with: it is unofficial, its adjustment conventions differ from the broker's, and two sources with different conventions make backtest and paper disagree in ways that are hard to trace. Paid vendor history was rejected on budget.
