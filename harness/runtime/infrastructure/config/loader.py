import tomllib
from pathlib import Path
from .schema import RuntimeConfig, validate_config

def load_config(path: str | Path) -> RuntimeConfig:
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Configuration file not found: {path_obj}")
    with path_obj.open("rb") as f:
        raw_config = tomllib.load(f)

    return validate_config(raw_config)
