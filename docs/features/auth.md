# Authentication

## API Key Auth

```python
from features.auth import APIKeyAuth

auth = APIKeyAuth()
key = auth.create_key("user1", "production")
verified = auth.verify(key)  # {"user_id": "user1", "label": "production"}
```

## Bearer Token Auth

```python
from features.auth import BearerAuth

auth = BearerAuth()
token = auth.get_token()  # "mt_..."
valid = auth.verify(f"Bearer {token}")  # True
```

## Memory Scopes (per-user isolation)

On HTTP transports, an API key bound to a user locks the `user_id` seen by
every memory tool:

```python
# tools receive ctx; _resolve_user_id reads Authorization from the request
from mcp_server.tools.base import _resolve_user_id

user_id = _resolve_user_id(ctx, requested_user_id)
# "Bearer ak_..." + valid key -> key-bound user_id (spoof-proof)
# no header / bearer token / invalid key -> requested_user_id (stdio/local unchanged)
```

- `AuthMiddleware` accepts either the global bearer token or a valid API key.
- Tools are registered through the `_scope_tool` wrapper — a client passing
  `user_id="bob"` with an `alice`-bound key silently operates on `alice`'s data.
- Without an API key (stdio, local, legacy clients) behaviour is unchanged.

## Key Features

- API keys and bearer tokens encrypted at rest (NaCl `SecretBox`, XSalsa20-Poly1305)
- Key rotation support
- Rate limiting per key
- Audit trail for all auth operations
- Per-user memory scoping on HTTP transports (API-key binding at the tool layer)
