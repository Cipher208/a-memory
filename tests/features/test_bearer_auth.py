import pytest
from features.auth.bearer import BearerAuth

@pytest.fixture
def token_file(tmp_path):
    return tmp_path / "tokens.enc"

def test_bearer_auth_init_and_generate(token_file):
    auth = BearerAuth(token_file)
    token = auth.get_token()
    assert token.startswith("mt_")
    assert len(token) > 10
    assert auth.verify(f"Bearer {token}") is True

def test_bearer_auth_persistence(token_file):
    auth1 = BearerAuth(token_file)
    token1 = auth1.get_token()

    auth2 = BearerAuth(token_file)
    token2 = auth2.get_token()

    assert token1 == token2
    assert auth2.verify(f"Bearer {token1}") is True

def test_bearer_auth_env_priority(token_file, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "mt_env_token")
    auth = BearerAuth(token_file)
    assert auth.get_token() == "mt_env_token"
    assert auth.verify("Bearer mt_env_token") is True

def test_bearer_auth_rotation(token_file):
    auth = BearerAuth(token_file)
    token1 = auth.get_token()

    token2 = auth.rotate()
    assert token1 != token2
    assert token2.startswith("mt_")
    assert auth.get_token() == token2

    assert auth.verify(f"Bearer {token1}") is False
    assert auth.verify(f"Bearer {token2}") is True

def test_bearer_auth_verify_invalid(token_file):
    auth = BearerAuth(token_file)
    assert auth.verify("Bearer invalid") is False
    assert auth.verify("Invalid format") is False
    assert auth.verify("") is False
