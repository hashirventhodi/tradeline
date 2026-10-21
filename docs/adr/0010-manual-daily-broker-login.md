---
status: accepted
date: 2026-10-21
---
# The daily Kite Session Login is manual, via a Telegram link

Kite Connect access tokens expire daily and require an interactive login with a second factor. The system sends the Operator a login link before the Run; the Operator completes it and the callback captures the token. If the login is missed, the day runs in Data-Only Mode and places no Orders. Automating the login by storing the TOTP secret on the server was rejected: it puts broker credentials on the box, and it sits in a grey area of the broker's terms. Uptime is traded for a smaller credential footprint and a clear line of responsibility.
