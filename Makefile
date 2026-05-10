.PHONY: help install install-dev test cov lint format type-check validate validate-edge gen-synthetic run run-streaming migrate migrate-status api dev admin notebook docs slides docker-build docker-run start-all stop-all compose-up compose-down compose-full-up compose-full-down smoke-test clean all

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	pip install -e .

install-dev:  ## Install dev dependencies (lint, test, type-check)
	pip install -e ".[dev]"
	pre-commit install || true

test:  ## Run unit + integration tests
	pytest

cov:  ## Test with HTML coverage report
	pytest --cov-report=html
	@echo "Open htmlcov/index.html"

lint:  ## Lint and format-check with ruff
	ruff check .
	ruff format --check .

format:  ## Auto-format with ruff
	ruff check --fix .
	ruff format .

type-check:  ## mypy type check
	mypy src api

validate:  ## Semantic validation against the development dataset
	python validate.py

validate-edge:  ## Semantic validation against development dataset + synthetic edge cases
	python validate.py --extra-dataset tests/fixtures/synthetic

gen-synthetic:  ## (Re)generate the synthetic edge-case fixtures
	python tests/fixtures/synthetic/gen_synthetic.py

run:  ## Run the full batch pipeline → output/ (in-memory; ≤ ~100k records)
	python run_analysis.py

run-streaming:  ## Streaming pipeline (production-volume safe; O(batch_size) memory)
	python run_analysis.py --streaming --batch-size 1000

migrate:  ## Apply alembic migrations to the bootstrap-configured DB
	alembic upgrade head

migrate-status:  ## Show current alembic revision + pending migrations
	alembic current
	alembic history

api:  ## Run the public API + dashboard (production: 4 workers)
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4

dev:  ## Run the public API with auto-reload (dev)
	uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

admin:  ## Run the admin panel on its own port (default 8001)
	uvicorn api.admin_app:app --reload --host 127.0.0.1 --port $${ADMIN_PORT:-8001}

notebook:  ## Open the narrative notebook
	jupyter lab transcript_intelligence.ipynb

load-test:  ## Run a 30s load test against a running API on :8000
	python -m tests.load.run_load_test --duration 30 --vus 20

load-test-quick:  ## Quick smoke load test (10s, 5 VUs)
	python -m tests.load.run_load_test --duration 10 --vus 5

docs:  ## Build the static HTML documentation site
	python build_docs.py

slides:  ## Open the HTML presentation in the default browser
	@open docs/presentation.html 2>/dev/null || xdg-open docs/presentation.html 2>/dev/null || \
	  echo "Open: file://$$(pwd)/docs/presentation.html"

docker-build:  ## Build the Docker image
	docker build -t transcript-intelligence:latest .

docker-run:  ## Run the API in a single Docker container
	docker run --rm -p 8000:8000 -v $(PWD)/../interview-assignment:/interview-assignment:ro transcript-intelligence:latest

start-all:  ## Run pre-flight checks + start ALL services (API + Jupyter + docs)
	./bin/start-all.sh

stop-all:  ## Stop any lingering services started outside the start-all trap
	./bin/stop-all.sh

compose-up:  ## Bring up the docker-compose stack (API container)
	docker compose up --build -d

compose-down:  ## Tear down the docker-compose stack
	docker compose down

compose-full-up:  ## Full stack: Postgres + Redis + Kafka + API + admin
	docker compose -f deploy/compose-full.yml up --build -d

compose-full-down:  ## Tear down the full-stack compose
	docker compose -f deploy/compose-full.yml down -v

smoke-test:  ## End-to-end smoke test (hits every public + admin endpoint)
	BASE_URL=$${BASE_URL:-http://127.0.0.1:8000} \
	ADMIN_URL=$${ADMIN_URL:-http://127.0.0.1:8001} \
	python -m tests.smoke_test

clean:  ## Remove generated outputs and caches
	rm -rf output/* htmlcov coverage.xml .coverage
	rm -rf __pycache__ .pytest_cache .ruff_cache .mypy_cache
	rm -rf src/__pycache__ api/__pycache__ tests/__pycache__
	find . -name "*.pyc" -delete
	touch output/.gitkeep

all: lint type-check test validate  ## lint + type-check + test + validate
