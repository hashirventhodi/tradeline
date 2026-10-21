---
status: accepted
date: 2026-10-21
---
# Paper trading is an internal Paper Broker, not a broker sandbox

Zerodha Kite Connect has no paper or sandbox environment. Paper trading therefore runs through our own Paper Broker, which fills Orders against real Kite quotes using a conservative fill model (an after-market limit order fills only if the opening price trades through the limit), slippage from half the quoted spread plus an impact term from traded value, and the full Indian delivery cost stack (STT both sides, stamp duty on buys, exchange and SEBI charges, GST, DP charge on sells). The Paper Broker implements the same Broker Adapter interface as the Kite Broker so that promotion to live changes one configuration value.
