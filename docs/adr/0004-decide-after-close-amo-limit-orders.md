---
status: accepted
date: 2026-10-21
amended: 2026-10-24
---
# Decisions run after the close; entries are after-market limit orders

The daily Run executes at about 17:00 IST on the completed day's data. Entry Orders are placed as Kite after-market orders (AMO) with limit prices, so they participate in the next day's opening without the system needing to be awake and authenticated at 09:15, and the Operator's Approvals happen in the evening rather than in the opening rush. Deciding intraday on partial data and racing the open were rejected as more fragile for no measurable benefit at a 1 to 15 day horizon.

## Amendment (2026-10-24, #72): the target is a resting limit sell

Exits are still evaluated on the completed bar and placed as after-market Orders for the next open, with one exception. When a Position opens, a limit sell at the Plan's target is placed and rests with the broker for the life of the Position; it fills at the target on the first day the high is strictly above it, at the open if the open gaps above it. Each Run re-places it as an after-market limit sell, so at most one sell Order is open per Position, and a stop cancels it before its own sell is placed. Nothing about it needs the system awake intraday: it is one standing Order per Position, placed with the entry and cancelled with the Position. Without it, every target exit would fill at the next open, below the target whenever the open is, while stops are modelled faithfully: a conservative but asymmetric handicap, possibly the difference at a Sharpe of 1.0. Stops stay as they were, since a resting stop-loss order would be a market Order on trigger, which risk-controls allows only through Flatten.
