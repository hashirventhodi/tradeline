.PHONY: db db-down migrate test typecheck lint check hooks

db:            ## Start the local Postgres
	docker compose up -d --wait postgres

db-down:       ## Stop the local Postgres (data volume is kept)
	docker compose down

migrate:       ## Apply pending migrations to DATABASE_URL
	uv run tradeline migrate

test:          ## Run the test suite against a throwaway database
	uv run pytest

typecheck:
	uv run mypy

lint:
	uv run ruff check . && uv run ruff format --check .

check: lint typecheck test

hooks:         ## Use the repo's git hooks (.githooks: the commit message's shape)
	git config core.hooksPath .githooks
