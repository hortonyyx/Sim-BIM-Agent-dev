"""A small checked-in test inventory, not a separate test framework."""

import json
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"fast", "targeted", "node"}


@lru_cache(maxsize=1)
def inventory():
    data = json.loads((ROOT / "tests/check_groups.json").read_text(encoding="utf-8"))
    entries = data["modules"]
    for path, entry in entries.items():
        if not path.startswith("tests/") or entry["group"] not in GROUPS or not entry["reason"]:
            raise ValueError(f"invalid check classification: {path}")
    return entries


def unclassified():
    actual = {p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("test_*.py")}
    return sorted(actual - inventory().keys())


def files_for(scope):
    if scope not in GROUPS:
        raise ValueError(f"unknown check scope: {scope}")
    included = {"fast"} if scope == "fast" else {"fast", "node"}
    return sorted(path for path, entry in inventory().items() if entry["group"] in included)
