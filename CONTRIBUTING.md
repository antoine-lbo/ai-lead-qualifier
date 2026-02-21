# Contributing to AI Lead Qualifier

Thanks for your interest in contributing! This guide will help you get started.

## Development Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Redis 7+
- An OpenAI API key

### Getting Started

```bash
# Clone the repository
git clone https://github.com/antoine-lbo/ai-lead-qualifier.git
cd ai-lead-qualifier

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt  # Dev dependencies

# Set up environment variables
cp .env.example .env
# Edit .env with your API keys

# Initialize the database
psql -f scripts/init-db.sql

# Seed sample data (optional)
python scripts/seed_db.py --count 50

# Run the development server
uvicorn src.main:app --reload
```

### Running with Docker

```bash
docker-compose up -d
```

## Project Structure

```
src/
  main.py          # FastAPI application entry point
  qualifier.py     # Core GPT-4 qualification logic
  enrichment.py    # Lead enrichment (Clearbit, LinkedIn)
  router.py        # Lead routing engine
  models.py        # Pydantic models
  config.py        # Configuration loading
  env.py           # Environment validation (Pydantic Settings)
  analytics.py     # Analytics and metrics
  batch.py         # Batch CSV processing
  crm.py           # CRM integration (HubSpot/Salesforce)
  webhooks.py      # Webhook handlers
  slack_notifier.py # Slack notifications
  rate_limiter.py  # Rate limiting middleware
tests/
  conftest.py      # Shared fixtures
  test_qualifier.py
  test_batch.py
config/
  scoring.yaml     # ICP and scoring configuration
scripts/
  init-db.sql      # Database schema
  seed_db.py       # Sample data generator
```

## Code Standards

### Style

- Follow PEP 8 with a max line length of 100 characters
- Use type hints for all function signatures
- Write docstrings for public functions and classes (Google style)
- Use `async`/`await` for I/O-bound operations

### Naming Conventions

- `snake_case` for functions, variables, and modules
- `PascalCase` for classes
- `UPPER_SNAKE_CASE` for constants
- Prefix private methods with `_`

### Testing

We use `pytest` with async support. All tests should be in the `tests/` directory.

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_qualifier.py

# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration
```

**Test requirements:**
- All new features must include tests
- Maintain test coverage above 80%
- Mock external services (OpenAI, Clearbit, Slack, etc.)
- Use fixtures from `conftest.py` for common test data

## Making Changes

### Branch Naming

- `feature/description` — New features
- `fix/description` — Bug fixes
- `refactor/description` — Code refactoring
- `docs/description` — Documentation updates
- `test/description` — Test additions or fixes

### Commit Messages

Follow conventional commits:

```
feat: add batch CSV processing endpoint
fix: handle timeout in Clearbit enrichment
refactor: extract scoring logic into separate module
docs: update API documentation for /qualify endpoint
test: add integration tests for CRM sync
```

### Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with clear, atomic commits
3. Ensure all tests pass (`pytest`)
4. Update documentation if needed
5. Open a PR with a clear description of changes
6. Request review from a maintainer

### PR Checklist

- [ ] Tests added/updated
- [ ] Type hints included
- [ ] Docstrings added for public APIs
- [ ] No hardcoded secrets or API keys
- [ ] Configuration changes documented in `.env.example`
- [ ] CI pipeline passes

## Architecture Decisions

### Why GPT-4 for Qualification?

We use GPT-4 for its ability to understand nuanced intent signals, company fit analysis, and budget indicators from unstructured lead messages. The scoring weights in `config/scoring.yaml` can be tuned without code changes.

### Why FastAPI?

FastAPI provides async support out of the box, automatic OpenAPI documentation, and excellent performance for our concurrent enrichment and qualification pipeline.

### Rate Limiting Strategy

We use Redis-backed rate limiting to protect both our API and external service quotas. The rate limiter supports per-client and global limits.

## Reporting Issues

When filing an issue, please include:

- Steps to reproduce
- Expected vs actual behavior
- Environment details (Python version, OS)
- Relevant logs or error messages

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
