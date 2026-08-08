from pathlib import Path
from features.auth.api_key import APIKeyAuth
from features.auth.bearer import BearerAuth

# Default paths for singletons as per spec [S5]
DEFAULT_KEYS_FILE = Path.home() / ".mcp-ariel-memory" / "api_keys.json"
DEFAULT_TOKEN_FILE = Path.home() / ".mcp-ariel-memory" / "bearer_token.json"

# Initialize and export singletons
api_key_auth = APIKeyAuth(keys_file=DEFAULT_KEYS_FILE)
bearer_auth = BearerAuth(token_file=DEFAULT_TOKEN_FILE)

__all__ = ["APIKeyAuth", "BearerAuth", "api_key_auth", "bearer_auth"]
