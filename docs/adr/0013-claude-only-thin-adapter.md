---
status: accepted
date: 2026-10-21
---
# One model provider behind a thin adapter

The AI layer uses Anthropic's API only: Claude Haiku 5.5 for bulk news and filing classification, Claude Opus 5.5 for Thesis and Critique, with prompt caching and the Batch API for historical labelling. All calls go through a small adapter that returns schema-validated structured output, so a second provider can be added later without touching the agents. Running the Skeptic on a second provider's model to reduce shared failure modes was considered and deferred: a second account and bill are not justified until the AI Promotion Gate shows the layer is worth keeping at all.
