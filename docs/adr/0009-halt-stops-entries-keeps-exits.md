---
status: accepted
date: 2026-10-21
---
# Halt cancels pending orders and blocks entries; exits keep running

When Halt is triggered, by the Operator or by a Limit breach, all pending Orders are cancelled, no new entries are placed, and open Positions continue to be managed by their existing stops and time exits. Flatten (close everything at market) is a separate command that requires a typed confirmation. Selling everything at market in the moment a limit trips is often the worst fill of the week, and freezing completely leaves Positions unmanaged. Halt is the default; Flatten is the Operator's deliberate choice.
