# ──────────────────────────────────────────────────────────────
# CS-Agent Makefile
# ──────────────────────────────────────────────────────────────
# Usage: make [command]

.PHONY: help install dev test lint format check docker clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies
	pip install -r requirements-dev.txt
	cd dashboard && npm install

dev: ## Start both API and dashboard
	@echo "Starting API on http://localhost:8001..."
	@start cmd /c "cd /d $(CURDIR) && .\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload"
	@echo "Starting Dashboard on http://localhost:5173..."
	@start cmd /c "cd /d $(CURDIR)\dashboard && npx vite"
	@echo "Both servers starting..."

test: ## Run all tests
	pytest tests/ -v --tb=short

test-cov: ## Run tests with coverage
	pytest tests/ -v --tb=short --cov=agent --cov=api --cov=integrations --cov-report=term-missing

lint: ## Run ruff linter
	ruff check .

format: ## Run ruff formatter
	ruff format .
	ruff check --fix .

check: lint test ## Run linter and tests

typecheck: ## Run mypy type checker
	mypy agent/ api/ integrations/ --ignore-missing-imports

docker-build: ## Build Docker image
	docker build -t cs-agent:latest .

docker-run: ## Run Docker container
	docker run -p 8001:8001 --env-file .env cs-agent:latest

docker-up: ## Start with docker-compose
	docker-compose up -d

docker-down: ## Stop docker-compose
	docker-compose down

clean: ## Clean build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info
	rm -rf dashboard/dist dashboard/node_modules/.cache
