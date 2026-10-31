.PHONY: install test lint run

install:
	uv sync
	uv run playwright install chromium

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

run:
	uv run uvicorn app.main:app --reload --port 8000
