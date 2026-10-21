---
status: accepted
date: 2026-10-21
---
# Promotion between stages is by configured, enforced Gates

A Strategy moves backtest to paper only when the Backtest Gate passes (out-of-sample Sharpe above 1.0 after full costs, max drawdown under 15%, at least 150 trades, profit factor above 1.3). Paper to live needs the Paper Gate (90 trading days, paper Expectancy within 30% of backtest, slippage within the model, zero Limit breaches). Live with Approvals to live without needs the Autonomy Gate (90 further days, same checks). The AI layer leaves Shadow Mode only on the AI Promotion Gate (at least 200 shadow decisions, vetoed trades show significantly lower Expectancy than kept trades, stated confidence is calibrated). The thresholds are configuration, the enforcement is code, and the Operator cannot promote by hand. The gates exist because the system has no demonstrated edge; they are how we find out whether it has one.
