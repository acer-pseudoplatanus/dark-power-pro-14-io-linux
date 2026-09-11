.PHONY: help install test lint typecheck coverage check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install package + dev tools
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install -e .

test: ## Run the test suite (offline, no device needed)
	.venv/bin/python -m pytest tests/

lint: ## Ruff lint + format check
	.venv/bin/ruff check bqio/ tests/
	.venv/bin/ruff format --check bqio/ tests/

typecheck: ## mypy strict
	.venv/bin/mypy bqio/

coverage: ## Test with coverage report (gate: 80%)
	.venv/bin/python -m pytest tests/ --cov=bqio --cov-report=term-missing

check: lint typecheck coverage ## Full quality gate (CI parity)

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build *.egg-info
