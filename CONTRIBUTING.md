# Contributing to mcp-ariel-memory

Thanks for your interest in contributing!

## Before You Open a PR

**Your PR will be closed without explanation if CI is broken.** Run these locally first:

```bash
# 1. Lint + format
ruff check .
ruff format --check .

# 2. Type check
mypy --config-file pyproject.toml features/ shared/ mcp_server/ rag/ hooks/ wiki/ lifecycle/ graph/ core/

# 3. Tests
pytest tests/ -v --timeout=30
```

All three must pass. No exceptions.

## Development Setup

```bash
git clone https://github.com/Cipher208/a-memory.git
cd mcp-ariel-memory
pip install -e ".[dev,binary]"
```

## Commit Messages

We use **Conventional Commits**:

```
feat: add new memory compression algorithm
fix: resolve race condition in ReflexBuffer
docs: update API reference for memory_recall
chore: update CI dependencies
test: add Hypothesis tests for scoring
refactor: extract shared utilities from hooks
```

Format: `<type>(<scope>): <description>`

Types: `feat`, `fix`, `docs`, `chore`, `test`, `refactor`, `perf`, `ci`, `build`

## Pull Request Rules

1. Fork the repo and create a branch from `master`
2. Make your changes
3. **Run the full check suite** (ruff, mypy, pytest)
4. Add tests for new functionality
5. Use Conventional Commits in your commit messages
6. Submit a PR using the PR template
7. **If CI fails, your PR will be closed**

## What We Review

- Code correctness
- Test coverage for new features
- Type annotations (mypy passes)
- No regressions (all 338 tests pass)
- Documentation updates if behavior changes

## Reporting Issues

Use the issue templates for bug reports and feature requests. For security vulnerabilities, see [SECURITY.md](SECURITY.md).
