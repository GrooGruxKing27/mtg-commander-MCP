#!/bin/bash
# Wrapper script for Claude Desktop MCP connection
# Uses --no-editable to avoid Python 3.14 .pth file issues
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
uv sync --no-editable --quiet 2>/dev/null
exec .venv/bin/mtg-commander-mcp "$@"
