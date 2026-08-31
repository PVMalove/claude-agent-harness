import tomllib
from dataclasses import dataclass
from typing import Any
from pathlib import Path

def load_config(path: str | Path) -> dict[str, Any]:
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Configuration file not found: {path_obj}")
    with path_obj.open("rb") as f:
        return tomllib.load(f)
