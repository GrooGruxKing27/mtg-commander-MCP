import logging as _logging
import sys as _sys

__version__ = "0.2.1"

# Configure a stderr handler at WARNING for the package on first import,
# unless the embedding application has already set up logging. This makes
# swallowed-exception sites debuggable in production (Claude Desktop captures
# stderr into its MCP log) without flooding well-configured callers.
_root = _logging.getLogger("mtg_commander_mcp")
if not _root.handlers and not _logging.getLogger().handlers:
    _handler = _logging.StreamHandler(_sys.stderr)
    _handler.setFormatter(
        _logging.Formatter("[mtg-commander-mcp] %(levelname)s %(name)s: %(message)s")
    )
    _root.addHandler(_handler)
    _root.setLevel(_logging.WARNING)
    _root.propagate = False
