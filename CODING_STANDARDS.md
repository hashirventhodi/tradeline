# Coding standards

Judgement calls the reviewer enforces. A rule a linter, type checker, hook, or test can enforce belongs there instead.

## Guards follow the filter

When code narrows a collection (filters it, splits it, drops a class of rows), every guard downstream — emptiness, presence, a value expected to be set — checks the narrowed collection that is actually used, and every predicate states the whole class it removes.

_Why:_ #34's first version refused an empty range before setting Pre-listing Candles aside, so a range holding only them divided by zero. Its predicate tested the date alone, so a day with an NSE row and no candle would have counted as a Pre-listing Candle and failed on its missing close. Both passed TDD and were caught in review.

## Deferred consequences name their issue

When an ADR or a research note leaves an action for later ("moves once …", "deferred to Phase 2", "re-run after …"), the same diff names what tracks it: an issue, or a phase's acceptance criterion.

_Why:_ ADR 0017 was accepted saying `tradeline.toml` moves to its version "only once those months are stored", and nothing tracked that. The configured rule kept barring NESTLEIND for 87 months until #18's fact-finding stumbled on it mid-grilling.

## Runtime figures state how they were measured

A runtime or cost figure in `docs/` says what it covered and whether it ran alone or alongside other work. Two figures for the same thing are reconciled where they appear.

_Why:_ #18's grilling set a five-minute target from "15 to 25 seconds a month, about 40 minutes", measured with six months running in parallel. Alone it was about 4 seconds a month. Review caught the two figures disagreeing.
