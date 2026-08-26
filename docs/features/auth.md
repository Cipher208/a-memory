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

## Key Features

- API keys and bearer tokens encrypted at rest (NaCl `SecretBox`, XSalsa20-Poly1305)
- Key rotation support
- Rate limiting per key
- Audit trail for all auth operations
