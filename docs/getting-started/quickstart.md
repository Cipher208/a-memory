# Quick Start

## Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "a-memory": {
      "command": "a-memory"
    }
  }
}
```

## Hermes Agent

```yaml
# ~/.hermes/config.yaml
memory:
  provider: ariel-memory
  transport: stdio
```

## HTTP Server

```bash
python -m mcp_server --transport http --port 8000
```

Then configure your MCP client to connect to `http://localhost:8000/mcp`.

## First Memory

Once connected, try:

```
think: {"text": "User prefers dark mode and concise answers"}
dream: {"query": "display preferences"}
```
