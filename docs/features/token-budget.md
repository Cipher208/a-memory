# Token Budget

## Overview

Limits context injection to 2000 tokens with CJK-aware estimation.

## Configuration

- DEFAULT_TOKEN_BUDGET: 2000 tokens
- CHARS_PER_TOKEN: 4 (English), 1 (CJK)

## How It Works

1. Token count estimated from text
2. If over budget, truncates at line boundaries
3. Returns truncation status and metrics

## Usage

Token budget is automatic in memory_context_inject.

result = await memory_context_inject(layer='user', user_id='default')
# Result includes: estimated_tokens, was_truncated, token_budget

## Testing

pytest tests/test_token_budget.py -v
