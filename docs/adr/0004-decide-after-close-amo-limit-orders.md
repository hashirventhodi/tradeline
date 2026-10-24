---
status: accepted
date: 2026-10-21
amended: 2026-10-24
---
# Decisions run after the close; entries are after-market limit orders

The daily Run executes at about 17:00 IST on the completed day's data. Entry Orders are placed as Kite after-market orders (AMO) with limit prices, so they participate in the next day's opening without the system needing to be awake and authenticated at 09:15, and the Operator's Approvals happen in the evening rather than in the opening rush. Deciding intraday on partial data and racing the open were rejected as more fragile for no measurable benefit at a 1 to 15 day horizon.

## Amendment (2026-10-24, #72): the target is a resting limit sell

Exits are still evaluated on the completed bar and placed as after-market Orders for the next open, with one exception. When a Position opens, a limit sell at the Plan's target is placed and rests with the broker for the life of the Position; it fills at the target on the first day the high is strictly above it, at the open if the open gaps above it. Each Run re-places it as an after-market limit sell, so at most one sell Order is open per Position, and a stop cancels it before its own sell is placed. Nothing about it needs the system awake intraday: it is one standing Order per Position, placed with the entry and cancelled with the Position. Without it, every target exit would fill at the next open, below the target whenever the open is, while stops are modelled faithfully: a conservative but asymmetric handicap, possibly the difference at a Sharpe of 1.0. Stops stay as they were, since a resting stop-loss order would be a market Order on trigger, which risk-controls allows only through Flatten.

## Amendment (2026-10-24, #85): the target keeps the Plan's reward to risk on the tick

Pricing rounds the stop down to the tick and sets the target at the Plan's own multiple r of the rounded risk, rounded up, so the reward to risk on the prices actually traded is never under r and the Risk Engine's minimum is never failed by rounding. Rounding both prices down, as #72 built it, put a Plan at r = 1.5 just under the 1.5 minimum, and the acceptance run's Risk Engine Blocked 2,020 of 2,058 such Plans (#85). The cost is the target's distance past the Strategy's own: the stop's rounding, under one tick, moves it by under r ticks, and its own rounding by under one more, so under r + 1 ticks in all, about 4 at r = 3, against a risk of k ATRs. Rounding only the target up was rejected: with the stop rounding down it does not hold reward to risk at r. The Operator signed this off on #85 after the family's Holdout was consumed, so the sign-off records the decision rather than gating it.

