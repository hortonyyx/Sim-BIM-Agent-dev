"""Meaningful boundaries for the small offline check selector."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.dev import check
from tests import check_selection


def test_default_selection_excludes_archive_and_node_imports():
    selected = check.select("fast", [], None)
    assert selected
    assert all(check_selection.inventory()[path]["group"] == "fast" for path in selected)
    assert "tests/test_runtime_responses_integration.py" in selected
    assert "tests/test_harness_stage0_history.py" not in selected
    assert "tests/test_role_end_to_end.py" not in selected
    assert check_selection.unclassified() == []


def test_explicit_current_node_can_run_but_unknown_test_is_rejected():
    assert check.select("targeted", ["tests/test_role_end_to_end.py"], None) == ["tests/test_role_end_to_end.py"]
    with pytest.raises(ValueError, match="select a test module explicitly"):
        check.select("targeted", ["tests/test_harness_stage0_history.py"], None)




def test_default_new_test_is_reported_and_explicit_selection_is_allowed(monkeypatch, tmp_path):
    import tests.conftest as hooks
    root = tmp_path / "repository"
    new = root / "tests/test_new_contract.py"
    new.parent.mkdir(parents=True)
    new.write_text("def test_new(): pass\n", encoding="utf-8")
    monkeypatch.setattr(hooks, "ROOT", root)
    monkeypatch.setattr(hooks, "inventory", lambda: {})
    monkeypatch.delenv("SIM_BIM_TEST_SCOPE", raising=False)
    config = SimpleNamespace(args=["tests"], invocation_params=SimpleNamespace(dir=root))
    with pytest.raises(pytest.UsageError, match="Unclassified test"):
        hooks.pytest_ignore_collect(new, config)
    config.args = ["tests/test_new_contract.py"]
    with pytest.warns(pytest.PytestWarning, match="selected explicitly"):
        assert hooks.pytest_ignore_collect(new, config) is False


def test_check_command_retains_gate_and_places_all_temporary_output_in_cache(monkeypatch, tmp_path):
    seen = {}
    def run(command, *, cwd, env):
        seen.update(command=command, cwd=cwd, env=env)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main(["fast", "tests/test_check_workflow.py", "--cache-root", str(tmp_path / "cache")]) == 0
    command, env = seen["command"], seen["env"]
    assert command[command.index("-n") + 1] == "0"
    assert "no_billed_gate" in command and "-o" not in command
    assert env["PYTHONPATH"] == str(check.ROOT)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert Path(env["TEMP"]).is_relative_to(tmp_path / "cache")
    assert Path(command[command.index("--basetemp") + 1]).is_relative_to(tmp_path / "cache")
