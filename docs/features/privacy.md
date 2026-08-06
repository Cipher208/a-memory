# Privacy Filter

## Overview

The privacy filter automatically strips sensitive data (API keys, secrets, tokens, private tags) from memory entries before storage.

## Supported Patterns

- OpenAI API keys (sk-*)
- Anthropic API keys (sk-ant-*)
- GitHub tokens (ghp_, gho_, ghs_, ghr_)
- Slack tokens (xox*)
- AWS keys (AKIA*)
- Google API keys (AIza*)
- Stripe keys (sk_live_, pk_live_, sk_test_)
- Bearer tokens
- Private/secret tags

## Usage

The privacy filter is automatically applied in memory_remember.

from mcp_server.utils.privacy import strip_secrets, has_secrets

# Strip secrets
clean_text = strip_secrets(text_with_secrets)

# Check for secrets
if has_secrets(user_input):
    logger.warning('Input contains potential secrets')

## Testing

pytest tests/test_privacy.py -v
