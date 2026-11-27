#!/usr/bin/env python3
"""
A tiny, dependency-free MCP server used by mcp-doctor's demo and tests.

It implements just enough of the protocol to be useful as a fixture:

    initialize                 -> protocolVersion, serverInfo, capabilities
    notifications/initialized  -> no response
    tools/list                 -> echo + add
    tools/call                 -> executes either tool
    ping                       -> empty result

Run it by hand to see the wire format:

    python -u demo_server.py

Point a client at it with:

    {"mcpServers": {"demo": {"command": "python", "args": ["-u", "demo_server.py"]}}}
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "mcp-doctor-demo", "version": "1.0.0"}

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text you pass in unchanged.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "text to echo back"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "first addend"},
                "b": {"type": "number", "description": "second addend"},
            },
            "required": ["a", "b"],
        },
    },
]


def reply(message_id: Any, result: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def reply_error(message_id: Any, code: int, text: str) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    message_id = message.get("id")

    if method == "initialize":
        reply(
            message_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        reply(message_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            reply(message_id, {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]})
        elif name == "add":
            try:
                total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
            except (TypeError, ValueError):
                reply_error(message_id, -32602, "a and b must be numbers")
                return
            reply(message_id, {"content": [{"type": "text", "text": str(total)}]})
        else:
            reply_error(message_id, -32601, f"unknown tool: {name}")
    elif method == "ping":
        reply(message_id, {})
    elif message_id is not None:
        reply_error(message_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            handle(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
