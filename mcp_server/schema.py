"""Generate JSON schema for MCP tools."""

from typing import Any


def generate_tool_schemas() -> list[dict[str, Any]]:
    """Generate JSON schemas for all registered MCP tools."""
    from mcp_server import mcp

    schemas = []
    tools = mcp._tool_manager.list_tools()

    for tool in tools:
        schema = {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters or {},
        }
        schemas.append(schema)

    return schemas


def generate_openapi_spec() -> dict[str, Any]:
    """Generate OpenAPI 3.0 spec for MCP tools."""
    schemas = generate_tool_schemas()

    paths = {}
    for schema in schemas:
        name = schema["name"]
        paths[f"/tools/{name}"] = {
            "post": {
                "summary": name,
                "description": schema["description"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": schema["parameters"],
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Tool execution result",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object"},
                            }
                        },
                    }
                },
            }
        }

    return {
        "openapi": "3.0.0",
        "info": {
            "title": "mcp-ariel-memory",
            "description": "Universal Two-Layer Memory MCP Server",
            "version": "1.0.0",
        },
        "paths": paths,
    }


if __name__ == "__main__":
    spec = generate_openapi_spec()
