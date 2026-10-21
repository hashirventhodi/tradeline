---
status: accepted
date: 2026-10-21
---
# Decisions run after the close; entries are after-market limit orders

The daily Run executes at about 17:00 IST on the completed day's data. Entry Orders are placed as Kite after-market orders (AMO) with limit prices, so they participate in the next day's opening without the system needing to be awake and authenticated at 09:15, and the Operator's Approvals happen in the evening rather than in the opening rush. Deciding intraday on partial data and racing the open were rejected as more fragile for no measurable benefit at a 1 to 15 day horizon.
