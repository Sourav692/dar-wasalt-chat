.PHONY: install test lint run extract index eval

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

extract:
	uv run python -m crawler.extract

index:
	uv run python -m ingest.build_index

eval:
	uv run python -m evals.retrieval_eval
