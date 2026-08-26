# Installation

## pip (recommended)

```bash
pip install a-memory
a-memory          # MCP server on stdio — connect from any MCP client
```

## From source

```bash
git clone https://github.com/Cipher208/a-memory.git
cd a-memory
uv sync
uv run ariel-memory
```

## Docker

```bash
docker build -t a-memory .
docker run -p 8000:8000 a-memory
```

## Dependencies

Requires Python 3.10+. Core dependencies install automatically:
`mcp[cli]>=2,<3`, `pydantic>=2.0`, `pyyaml>=6.0`, `pynacl>=1.5.0`,
`aiosqlite>=0.22.1`, `numpy>=2.2.6`, `prometheus-client`, `alembic`,
`starlette`, `uvicorn`, `python-frontmatter`.

### Optional extras

| Extra | Installs | Purpose |
|-------|----------|---------|
| `binary` | numpy | Binary embeddings (MIB search) |
| `embeddings` | sentence-transformers | Real multilingual embeddings (e5-small); without it a deterministic hash fallback is used, and `ARIEL_HASH_EMBEDDINGS=1` forces that fallback explicitly |
| `vec` | sqlite-vec | Vector search backend |
| `ann` | hnswlib | Approximate nearest neighbors |
| `win` | pinned aiosqlite | Windows compatibility |
| `dev` | pytest, ruff, mypy, ... | Development |
| `docs` | mkdocs-material | Documentation build |
| `all` | everything above | Full setup |

```bash
pip install "a-memory[all]"
```
