# Coding standards

Judgement calls the reviewer enforces. A rule a linter, type checker, hook, or test can enforce belongs there instead.

## Guards follow the filter

When code narrows a collection (filters it, splits it, drops a class of rows), every guard downstream — emptiness, presence, a value expected to be set — checks the narrowed collection that is actually used, and every predicate states the whole class it removes.

_Why:_ #34's first version refused an empty range before setting Pre-listing Candles aside, so a range holding only them divided by zero. Its predicate tested the date alone, so a day with an NSE row and no candle would have counted as a Pre-listing Candle and failed on its missing close. Both passed TDD and were caught in review.
