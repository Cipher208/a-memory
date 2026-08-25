# Contributing

## Development Setup

```bash
git clone https://github.com/Cipher208/a-memory.git
cd mcp-ariel-memory
pip install -e ".[dev,binary]"
```

## Running Tests

```bash
pytest tests/ -v --timeout=30
```

## Code Style

- Lint: `ruff check .`
- Format: `ruff format .`
- Type check: `mypy --config-file pyproject.toml features/ shared/ mcp_server/ rag/ hooks/ wiki/ lifecycle/ graph/ core/`

## Pull Requests

1. Fork and create branch
2. Make changes
3. Add tests
4. Ensure all checks pass
5. Submit PR
