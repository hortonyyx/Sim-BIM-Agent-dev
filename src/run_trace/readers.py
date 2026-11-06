"""Read persisted runtime evidence without opening the run for execution."""
from pathlib import Path


def read_events(directory: Path, runtime: str) -> list[dict]:
    directory = Path(directory).resolve()
    if runtime == "native":
        from .native import read_native
        return read_native(directory)
    if runtime == "deepseek":
        from .deepseek import read_deepseek
        return read_deepseek(directory)
    raise ValueError(f"Unsupported trace runtime: {runtime!r}")
