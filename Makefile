.PHONY: help install install-dev test cov lint format type-check validate run api dev notebook docs docker-build docker-run clean all

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

validate:  ## Semantic validation against the dataset
	python validate.py

run:  ## Run the full batch pipeline → output/
	python run_analysis.py

api:  ## Run the FastAPI server (production: 4 workers)
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4

dev:  ## Run the FastAPI server with auto-reload (dev)
	uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

notebook:  ## Open the narrative notebook
	jupyter lab transcript_intelligence.ipynb

docs:  ## Build the static HTML documentation site
	python build_docs.py

docker-build:  ## Build the Docker image
	docker build -t transcript-intelligence:latest .

docker-run:  ## Run the API in Docker
	docker run --rm -p 8000:8000 -v $(PWD)/../interview-assignment:/interview-assignment:ro transcript-intelligence:latest

clean:  ## Remove generated outputs and caches
	rm -rf output/* htmlcov coverage.xml .coverage
	rm -rf __pycache__ .pytest_cache .ruff_cache .mypy_cache
	rm -rf src/__pycache__ api/__pycache__ tests/__pycache__
	find . -name "*.pyc" -delete
	touch output/.gitkeep

all: lint type-check test validate  ## lint + type-check + test + validate
