---
status: accepted
date: 2026-10-21
---
# The final two years are a locked Holdout, run once per Strategy version

Validation is Walk-Forward on the earlier history. The most recent two years are locked: a Strategy version runs on them exactly once, the result is written to the Ledger permanently, and running again requires a new Strategy version. Once a holdout has been looked at it is no longer out of sample; this rule makes that cost explicit and prevents the quiet iteration that turns a holdout into a training set.
