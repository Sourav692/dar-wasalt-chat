.PHONY: install test lint run crawl extract index eval judge redteam docker-build docker-run

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

# Polite crawl (robots.txt, 5 s delay); writes data/raw/. Wasalt pages are saved by hand.
crawl:
	uv run python -m crawler.crawl

extract:
	uv run python -m crawler.extract

index:
	uv run python -m ingest.build_index

eval:
	uv run python -m evals.retrieval_eval

# Live model calls; need OPENROUTER_API_KEY. redteam's HTTP cases need `make run` first.
judge:
	uv run python -m evals.judge_eval

redteam:
	uv run python -m evals.redteam --url http://localhost:8000

# The image bakes in ./index and the embedding model. Port 8080 locally; Cloud Run sets $PORT.
docker-build:
	docker build -t dar-wasalt-chat:local .

docker-run: docker-build
	docker run --rm -p 8080:8000 --env-file .env dar-wasalt-chat:local
