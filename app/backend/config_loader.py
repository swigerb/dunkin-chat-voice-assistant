"""Loads the centralized config.yaml for the Dunkin backend."""

import os
from pathlib import Path
from typing import Any

import yaml

__all__ = ["get_config"]

_config: dict[str, Any] | None = None


def get_config() -> dict[str, Any]:
    """Return the parsed config.yaml as a dict. Cached after first load."""
    global _config
    if _config is not None:
        return _config

    config_path = os.environ.get("DUNKIN_CONFIG_PATH")
    if config_path:
        path = Path(config_path)
    else:
        path = Path(__file__).resolve().parent / "config.yaml"

    if not path.exists():
        _config = {}
        return _config

    with path.open("r", encoding="utf-8") as f:
        _config = yaml.safe_load(f) or {}
    return _config
