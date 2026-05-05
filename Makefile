.PHONY: help install test validate run dashboard notebook docs clean all

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	pip install -r requirements.txt

test:  ## Run unit tests
	python -m pytest tests/ -v

validate:  ## Run semantic validation against the dataset
	python validate.py

run:  ## Run the full pipeline end-to-end
	python run_analysis.py

dashboard:  ## Launch the Streamlit dashboard
	streamlit run dashboard.py

notebook:  ## Open the narrative notebook
	jupyter lab transcript_intelligence.ipynb

docs:  ## Build the static HTML documentation site (docs/html/)
	python build_docs.py

clean:  ## Remove generated outputs and caches
	rm -rf output/* __pycache__ .pytest_cache src/__pycache__ tests/__pycache__
	find . -name "*.pyc" -delete
	touch output/.gitkeep

all: install test validate run  ## install + test + validate + run
