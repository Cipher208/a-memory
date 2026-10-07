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

## Test Data and Fixtures

**Fixtures are shipped in the repository. Treat every fixture as public.**

This project pins real rows from its own history — broadcast reports, recall
episodes, session notes. That is deliberate and useful, but it means a fixture
can carry more than the behaviour it is meant to exercise. Most of this codebase's
tests are about memory, and memory is written by real operators about real machines.

Never commit these, in any form, including inside a fixture, a docstring, a test
constant or a comment:

- hostnames, node names, or machine identifiers — of this project's operators or anyone else's
- private-network addresses: RFC 1918, CGNAT (the `100.64/10` block, which includes
  every Tailscale address), link-local, or WireGuard/VPN subnets
- ssh users, key paths, or the *names* of secret-bearing environment variables
- VPN or tunnel parameters: interface names, listen ports, key material, obfuscation settings
- service inventories above the level of the generic ("a backup service listens on
  loopback"), and never with ports and paths attached
- personal names of operators and the people they live with

**Substitute, do not delete.** A fixture whose point is that a recon report is
classified as a broadcast must still look like a recon report. Keep the shape,
swap the identifiers — `node-b`, `<ssh-user>`, `<ssh-key-path>`,
`<secret-var-name>`, `10.9.1.0`. Anonymised rows catch regressions just as well.

**Note on secret scanning.** `detect-secrets` runs on every push and it will not
save you here. gitleaks matches credential *shapes* — provider prefixes, high-entropy
strings, private-key headers. Infrastructure disclosure has none of those: it is made
of names, paths and roles. A green scanner says "no credentials", not "nothing sensitive".

If you are unsure whether a fixture row is safe, anonymise it. There is no cost to
being generic and there is no way to un-publish a fork.

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
- **No operator-identifying data in fixtures** (see [Test Data and Fixtures](#test-data-and-fixtures))
- Documentation updates if behavior changes

## Reporting Issues

Use the issue templates for bug reports and feature requests. For security vulnerabilities, see [SECURITY.md](SECURITY.md).
