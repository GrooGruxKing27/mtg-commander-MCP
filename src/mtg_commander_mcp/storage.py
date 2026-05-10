"""On-disk persistence for the active collection.

JSON snapshot at ``$XDG_DATA_HOME/mtg-commander-mcp/collection.json``
(falls back to ``~/.local/share/...``). Single active collection per
user; no multi-collection or multi-user support in v0.3.0.

Kept deliberately thin: the rest of the codebase has no filesystem
state, and we'd rather rebuild this trivially than carry a SQLite
schema.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "mtg-commander-mcp"


def collection_path() -> Path:
    return _data_dir() / "collection.json"


def save_collection(payload: dict) -> Path:
    """Atomically write the active collection JSON.

    Atomicity matters because a partial write would leave a future
    `load_collection` call to crash on truncated JSON. tempfile +
    rename is the standard Linux/macOS approach.
    """
    path = collection_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix="collection.", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def load_collection() -> dict | None:
    """Return the persisted collection payload, or None if not set."""
    path = collection_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read persisted collection at %s: %s", path, e)
        return None
