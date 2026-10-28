.PHONY: help
default: help

PYTHON ?= python3
export PYTHONPATH := src

help: ## Display this help screen
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
install: ## Install dependencies
	$(PYTHON) -m pip install -e ".[dev]"
build: ## Build the library version
	$(PYTHON) -m build
serve: ## Run the web app on http://127.0.0.1:8000
	$(PYTHON) -m implicated.web --reload
seed: ## Fill the local library with demo data
	$(PYTHON) -m implicated.seed
import: ## Import artists, albums, songs and covers from info/
	$(PYTHON) -m implicated.importer info
test: ## Run the test suite
	$(PYTHON) -m pytest --cov
lint: ## Apply the linter
	ruff check --fix
