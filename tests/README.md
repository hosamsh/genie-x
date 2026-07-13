# Tests

This directory contains all tests for the genie-x project.

## Running Tests

This project uses **uv** for dependency management and running commands.

### Prerequisites

```bash
# Sync dependencies (includes pytest from pyproject.toml)
uv sync
```

### Run All Unit Tests

```bash
# From project root
uv run pytest tests/unit/ -v
```

### Run All Integration Tests

```bash
uv run pytest tests/integration/ -v
```

### Run All Tests

```bash
uv run pytest tests/ -v
```


## Examples

### Run a specific test file

```bash
uv run pytest tests/unit/test_cli_extract.py -v
```

### Run a specific test class

```bash
uv run pytest tests/unit/test_contracts.py::TestCombinedTurnsView -v
```

### Run a specific test

```bash
uv run pytest tests/unit/test_cli_extract.py::test_extract_single_workspace_creates_database -v
```

### Run tests matching a pattern

```bash
# Run all tests with "search" in the name
uv run pytest tests/ -k "search" -v

# Run all tests with "extract" but not "refresh"
uv run pytest tests/ -k "extract and not refresh" -v
```

### Run tests with coverage

```bash
uv run pytest tests/unit/ --cov=src --cov-report=html
# Open htmlcov/index.html in browser
```

### Run tests in parallel (requires pytest-xdist)

```bash
uv add --dev pytest-xdist
uv run pytest tests/unit/ -n auto
```

## Test Categories

### Unit Tests (`tests/unit/`)

Fast tests that use **synthetic fixtures** (fake workspace data created in memory). These tests:
- Don't require real supported-agent workspace data
- Run in ~30-50 seconds total
- Are isolated and deterministic

### Integration Tests (`tests/integration/`)

Slower tests that use **real workspace data** from your machine. These tests:
- Require actual supported-agent workspaces to exist
- May be skipped if no workspaces are available
- Test end-to-end behavior

### Markers

Some tests are marked for filtering:

```bash
# Run only integration-marked tests
uv run pytest tests/ -m integration -v

# Skip integration tests
uv run pytest tests/ -m "not integration" -v
```


## Troubleshooting

### Can't find modules

Make sure you're running from the project root:

```bash
cd c:\code\projects\genie-x
uv run pytest tests/unit/ -v
```

### Integration tests are skipped

Integration tests require real workspace data. If skipped, it means no workspaces were found in your configured storage paths.

### Web API tests fail

Web API tests require FastAPI and its dependencies. They should already be installed via `uv sync`, but if needed:

```bash
uv add fastapi httpx
```

### Tests are slow

Unit tests should complete in under a minute. If slow:
- Use `-x` to stop on first failure
- Run specific test files instead of all tests

## Writing New Tests

1. **Unit tests** go in `tests/unit/`
2. Use existing fixtures from `conftest.py`
3. Follow the `test_*` naming convention
4. Add docstrings with test IDs (e.g., `T1-4: ...`)

Example:

```python
def test_my_new_feature(cli_runner, make_test_config, copilot_workspace, run_dir):
    """T1-XX: Description of what this test verifies."""
    config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
    
    result = cli_runner("--my-flag", "--run-dir", str(run_dir), config_path=config_path)
    
    assert result.returncode == 0
    # ... more assertions
```
