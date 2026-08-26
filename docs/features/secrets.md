# Encryption

## Envelope Encryption

All sensitive data (API keys, tokens, saga state) encrypted at rest using NaCl `SecretBox` (XSalsa20-Poly1305 via PyNaCl) — a 24-byte random nonce prepended to the ciphertext, 32-byte master key, 16-byte MAC.

```python
from features.secrets import encrypt_json, decrypt_json

# Encrypt
blob = encrypt_json({"token": "secret", "created_at": 1234567890})
# blob = nonce(24) + ciphertext (binary)

# Decrypt
data = decrypt_json(blob)
# data = {"token": "secret", "created_at": 1234567890}
```

## Key Resolution

1. OS keychain (keyring) — production
2. config.yaml (`crypto.master_key_hex`)
3. .env file (`MCP_MASTER_KEY=...`) — development
4. Environment variable
5. Auto-generate (saves to .env)

## Decrypt-First Pattern

Auth modules (`APIKeyAuth`, `BearerAuth`) use a decrypt-first pattern:

```python
# Try decrypt first (handles nonce collision where is_encrypted_blob returns False)
try:
    return decrypt_json(blob)
except Exception:
    pass

# Not encrypted — parse as legacy JSON and rotate
legacy = json.loads(blob.decode("utf-8"))
```

This eliminates flaky tests caused by the SecretBox nonce's first byte coincidentally being `{` or `[` (~0.78% probability).

## File Format

```
[nonce 24 bytes][ciphertext...]
```
