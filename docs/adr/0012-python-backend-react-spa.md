---
status: accepted
date: 2026-10-21
---
# Python backend; TypeScript dashboard as a static SPA served by FastAPI

The backend is Python 3.12 (uv, FastAPI, Postgres) because the broker ships an official Python client and the quant ecosystem is Python. The dashboard is a Vite and React single-page app built to static files that the API container serves, so there is one runtime, one container, and no Node process in production. Next.js in its own container was rejected for the extra runtime and inter-service auth; an HTMX server-rendered UI was rejected by the Operator in favour of a richer TypeScript dashboard.
