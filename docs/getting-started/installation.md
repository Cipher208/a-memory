# Installation

## npm (recommended)

```bash
npx mcp-ariel-memory --transport stdio
```

Requires Python 3.10+ on the system. The npm wrapper automatically installs the Python package.

## pip

```bash
pip install git+https://github.com/Cipher208/a-memory.git
python -m mcp_server --transport stdio
```

## Docker

```bash
docker build -t ariel-memory .
docker run -p 8000:8000 ariel-memory
```

## From source

```bash
git clone https://github.com/Cipher208/a-memory.git
cd mcp-ariel-memory
pip install -e ".[all]"
python -m mcp_server --transport stdio
```

## Dependencies

### Core

| Package | Version | Purpose |
|---------|---------|---------|
| mcp[cli] | >=1.27,<2 | MCP Python SDK |
| pydantic | >=2.0 | Data validation |
| pynacl | >=1.5.0 | Envelope encryption |
| pyyaml | >=6.0 | Config parsing |

### Optional

| Package | Extra | Purpose |
|---------|-------|---------|
| aiosqlite | win | Async SQLite (auto-installed on Linux/macOS) |
| numpy | binary | Binary embeddings (MIB search) |
| sqlite-vec | vec | Vector search |
| hnswlib | ann | Approximate nearest neighbors |
