# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, use **GitHub Private Vulnerability Reporting**:

1. Go to the [Security tab](https://github.com/Cipher208/a-memory/security) of the repository
2. Click "Report a vulnerability"
3. Fill in the form with details about the vulnerability

### What to include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response timeline

- **Acknowledgment**: within 48 hours
- **Assessment**: within 1 week
- **Fix**: critical vulnerabilities within 2 weeks, others within 30 days

### What happens after reporting

- We will confirm receipt of your report
- We will investigate and assess the severity
- We will develop a fix
- We will release a patch version
- We will credit you in the release notes (unless you prefer anonymity)

## Security Features

- **Envelope encryption** — all sensitive data encrypted at rest (libsodium secretbox)
- **Keychain-first key resolution** — master key from OS keychain, not .env
- **Secret scanning** — GitHub secret scanning + push protection enabled
- **Dependency auditing** — pip-audit scans for CVEs on every CI run
- **CodeQL analysis** — AST-based vulnerability detection
- **gitleaks** — secret scanning in CI pipeline
- **CORS restricted** — localhost only by default, configurable via config.yaml
- **Docker** — runs as non-root user (UID 1000)

## Scope

This security policy applies to:

- The MCP server code (Python)
- The Docker image
- The npm wrapper
- The documentation site

Out of scope:

- Third-party dependencies (report to their maintainers)
- The LLM agent using this server (report to the agent framework)
