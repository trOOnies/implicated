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
serve: ## Run the web app on http://127.0.0.1:8000 (never imports on its own)
	$(PYTHON) -m implicated.web --reload
seed: ## Fill the local library with demo data
	$(PYTHON) -m implicated.seed
import: ## Preview the info/ inbox, then import and archive it if you say yes
	$(PYTHON) -m implicated.importer info
import-preview: ## Only show what importing the info/ inbox would change
	$(PYTHON) -m implicated.importer info --preview
migrate-info: ## Once: move an older info/'s lyrics, tabs and concerts into local/
	$(PYTHON) -m implicated.migrate info
backup: ## Zip the whole library folder: make backup [FILE=...]
	$(PYTHON) -m implicated.backup backup $(FILE)
restore: ## Put a backup back, with the app stopped: make restore FILE=...
	$(PYTHON) -m implicated.backup restore "$(FILE)"
export: ## Zip the library as an inbox to import elsewhere: make export [FILE=...] [OWNER=...]
	$(PYTHON) -m implicated.backup export $(FILE) $(if $(OWNER),--owner "$(OWNER)")
export-data: ## Download your own data to a JSON file (OWNER=name to add yours)
	$(PYTHON) -m implicated.userdata export $(if $(OWNER),--owner "$(OWNER)")
import-data: ## Import your own data from a JSON file: make import-data FILE=...
	$(PYTHON) -m implicated.userdata import "$(FILE)"
test: ## Run the test suite
	$(PYTHON) -m pytest --cov
lint: ## Apply the linter
	ruff check --fix
