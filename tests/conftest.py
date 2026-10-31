"""Classify offline checks and expose the early network guard.

The no_billed_gate plugin is loaded first through pyproject.toml so provider
connections are blocked before tests or other plugins can cache network APIs.
The hooks are defined only there; this module re-exports the shared exception
and local-address predicate for tests that need to inspect the guard.
"""

from no_billed_gate import (  # noqa: F401  (re-export for `from conftest import ...`)
    ProviderCallBlocked,
    _is_local,
)

# Classify checks before importing their modules.
import os
from pathlib import Path

import pytest

from tests.check_selection import GROUPS, ROOT, inventory, unclassified


def pytest_configure(config):
    for group in sorted(GROUPS):
        config.addinivalue_line("markers", f"{group}: classified offline check scope")


def _explicit_file(config, path):
    key = (str(config.invocation_params.dir), tuple(str(arg) for arg in config.args))
    cached = getattr(config, "_sim_bim_explicit_files", None)
    if cached is None or cached[0] != key:
        selected = set()
        for argument in config.args:
            candidate = Path(str(argument).split("::", 1)[0])
            if not candidate.is_absolute():
                candidate = Path(config.invocation_params.dir) / candidate
            if candidate.suffix == ".py":
                selected.add(candidate.resolve())
        cached = (key, selected)
        config._sim_bim_explicit_files = cached
    return path.resolve() in cached[1]


def pytest_sessionstart(session):
    scope = os.environ.get("SIM_BIM_TEST_SCOPE", "fast")
    if scope not in GROUPS:
        raise pytest.UsageError(f"unknown SIM_BIM_TEST_SCOPE: {scope}")


def pytest_ignore_collect(collection_path, config):
    path = Path(str(collection_path))
    if not path.name.startswith("test_") or path.suffix != ".py":
        return None
    try:
        relative = path.relative_to(ROOT).as_posix()
    except ValueError:
        return None
    scope = os.environ.get("SIM_BIM_TEST_SCOPE", "fast")
    if scope not in GROUPS:
        raise pytest.UsageError(f"unknown SIM_BIM_TEST_SCOPE: {scope}")
    explicit = _explicit_file(config, path)
    entry = inventory().get(relative)
    if entry is None:
        if explicit:
            import warnings
            warnings.warn(f"Unclassified new test selected explicitly: {relative}; add it to tests/check_groups.json", pytest.PytestWarning)
            return False
        # Pytest also walks siblings while resolving an explicit node id. An
        # unrelated new file must not block a valid targeted check.
        if all(Path(str(arg).split("::", 1)[0]).suffix == ".py" for arg in config.args):
            return True
        raise pytest.UsageError(f"Unclassified test: {relative}; add it to tests/check_groups.json or select its file explicitly")
    group = entry["group"]
    if explicit:
        return False
    if scope == "node":
        return group == "targeted"
    return scope == "fast" and group != "fast"


def pytest_collection_modifyitems(config, items):
    scope = os.environ.get("SIM_BIM_TEST_SCOPE", "fast")
    for item in items:
        path = Path(str(item.path))
        try:
            entry = inventory().get(path.relative_to(ROOT).as_posix())
        except ValueError:
            entry = None
        if entry:
            item.add_marker(getattr(pytest.mark, entry["group"]))
        if (scope != "node" and item.get_closest_marker("mutation")
                and not _explicit_file(config, path)):
            item.add_marker(pytest.mark.skip(reason="mutation matrix belongs to node checks"))


def pytest_report_header(config):
    scope = os.environ.get("SIM_BIM_TEST_SCOPE", "fast")
    return (f"Sim-BIM check scope: {scope}; classified selection, not all test modules. "
            "Fast excludes node/optional targeted checks. "
            "The no-billed-calls gate remains active.")


def pytest_terminal_summary(terminalreporter):
    scope = os.environ.get("SIM_BIM_TEST_SCOPE", "fast")
    terminalreporter.write_line(f"Coverage boundary: {scope} / explicitly selected modules only; "
                              "unselected node and optional targeted modules are not covered.")
