# SHA-256 Deduplication

## Overview

Prevents duplicate observations from being stored when the same tool call is made multiple times within a 5-minute window.

## How It Works

1. Hash generated from session_id + tool_name + input[:500]
2. Hash checked against cache with 5-minute TTL
3. If duplicate found, returns status=skipped

## Usage

Dedup is automatic in `think` / `memory_remember` when a session_id is provided (sha256 of `session_id:tool_name:input[:500]`, 5-minute TTL, 10k cache, 60s cleanup sweep).

## Configuration

- TTL: 300 seconds (5 minutes)
- Max cache size: 10,000 entries
- Cleanup interval: 60 seconds

## Testing

pytest tests/test_dedup.py -v
