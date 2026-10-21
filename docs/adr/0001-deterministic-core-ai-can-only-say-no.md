---
status: accepted
date: 2026-10-21
---
# The quant core is deterministic; the AI layer can only veto or shrink

The original brief imagined five collaborating AI agents reaching a consensus to trade. We decided instead that a versioned, deterministic Strategy originates every Candidate and Plan, a code-only Risk Engine has the final word, and the AI layer (Thesis plus Skeptic) may only downgrade a BUY to SKIP or reduce size within the Limits. It can never originate a trade, increase size, or loosen a stop, and it starts in Shadow Mode with no effect at all.

## Considered options

- **Multi-agent consensus with an AI risk manager.** Rejected: no published evidence of out-of-sample profitability, documented lookahead contamination in LLM backtests, live contests in 2025 and 2026 where most frontier models lost money, and no way to make a prompt unbypassable.
- **No AI at all.** Viable, and the fallback if the AI Promotion Gate never passes. Rejected as the default because news and filing interpretation is the one task where language models add something code cannot, and the asymmetric design caps the downside.

## Consequences

The AI's contribution is measurable as the difference in Expectancy between trades it would have vetoed and trades it would have kept. If that difference is not significant over 200 shadow decisions, the layer stays off and the system is a plain quant strategy.
