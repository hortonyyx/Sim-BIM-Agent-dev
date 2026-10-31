"""The dependency selector follows imports and refuses incomplete coverage."""
import subprocess
import sys
from pathlib import Path
import pytest
from scripts.dev import affected_tests as affected

@pytest.fixture
def graph(tmp_path, monkeypatch):
    files = {
        "src/middle.py": "import src.leaf\n",
        "src/leaf.py": "REFERENCE = 'tests/test_unrelated.py'\n",
        "src/uncovered.py": "VALUE = 1\n",
        "scripts/tool_scripts/probe.py": "VALUE = 2\n",
        "tests/helpers.py": "import src.middle\n",
        "tests/test_imports.py": "from tests import helpers\n",
        "tests/test_command.py": "COMMAND = 'scripts/tool_scripts/probe.py'\n",
        "tests/test_unrelated.py": "def test_ok(): pass\n",
        "README.md": "Instructions\n",
        "pyproject.toml": "[project]\n",
    }
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    monkeypatch.setattr(affected, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(affected, "first_class_files", lambda: tuple(n for n in files if n.endswith(".py")))
    return tuple(n for n in files if n.endswith(".py"))

def test_deterministic_output_and_cli_contract(monkeypatch):
    path = "src/agent/geometry/source_volume.py"
    first = affected.render(affected.affected_tests([path]), "test", explain=True)
    files = affected.first_class_files()
    monkeypatch.setattr(affected, "first_class_files", lambda: tuple(reversed(files)))
    assert affected.render(affected.affected_tests([path]), "test", explain=True) == first
    assert first.startswith("SCOPE: SUBSET\npython scripts/dev/check.py ")
    completed = subprocess.run([sys.executable, "scripts/dev/affected_tests.py", "--changed", path, "--explain"], text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("SCOPE: SUBSET\n")
    assert "EXPLAIN: " in completed.stdout

def test_transitive_import_keeps_helper_edges_but_selects_only_tests(graph):
    result = affected.affected_tests(["src/leaf.py"])
    assert result.scope == "SUBSET"
    assert result.tests == ("tests/test_imports.py",)
    chain = affected.find_path(affected.build_edges(graph), "tests/test_imports.py", "src/leaf.py")
    assert len(chain) == 3
    assert all(edge.kind == "import" for edge in chain)

def test_string_path_selects_command_without_production_to_test_bridge(graph):
    result = affected.affected_tests(["scripts/tool_scripts/probe.py"])
    assert result.tests == ("tests/test_command.py",)
    edges = affected.build_edges(graph)
    assert affected.Edge("tests/test_command.py", "scripts/tool_scripts/probe.py", "string-path") in edges
    assert not [e for e in edges if not e.source.startswith("tests/") and e.target.startswith("tests/")]

@pytest.mark.parametrize("path,message", [
    ("README.md", "not a first-class Python file"),
    ("pyproject.toml", "full-scope trigger matched"),
    ("src/absent.py", "deleted or absent"),
    ("src/uncovered.py", "has no covering test"),
])
def test_unknown_or_uncovered_changes_fail_closed(graph, path, message):
    result = affected.affected_tests([path])
    assert result.scope == "FULL"
    assert message in result.reasons[0]

def test_fail_closed_for_broken_rules_table(graph, tmp_path, monkeypatch):
    broken = tmp_path / "broken.yaml"
    broken.write_text("full_scope: [", encoding="utf-8")
    monkeypatch.setattr(affected, "RULES_PATH", broken)
    result = affected.affected_tests(["src/leaf.py"])
    assert result.scope == "FULL"
    assert "rules table cannot be parsed" in result.reasons[0]

def test_incomplete_mapping_does_not_claim_full_coverage():
    rendered = affected.render(affected.MappingResult("FULL", (), ("unknown interface",), {}), "test", False)
    assert rendered.startswith("SCOPE: NODE\npython scripts/dev/check.py node\nMAPPING: INCOMPLETE\n")
    assert "SCOPE: FULL" not in rendered

def test_since_uses_committed_range_only(tmp_path, monkeypatch):
    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init")
    git("config", "user.email", "affected-tests@example.invalid")
    git("config", "user.name", "Affected Tests")
    (tmp_path / "baseline.py").write_text("BASELINE = 1\n", encoding="utf-8")
    git("add", "baseline.py")
    git("commit", "-m", "baseline")
    base = git("rev-parse", "HEAD")
    (tmp_path / "committed.py").write_text("COMMITTED = 1\n", encoding="utf-8")
    git("add", "committed.py")
    git("commit", "-m", "committed change")
    (tmp_path / "baseline.py").write_text("BASELINE = 2\n", encoding="utf-8")

    monkeypatch.setattr(affected, "REPO_ROOT", tmp_path)
    assert affected.changed_since(base) == ["committed.py"]


def test_graph_snapshot_cache_invalidates_when_a_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(affected, "REPO_ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    consumer = tmp_path / "src/consumer.py"
    consumer.write_text("import src.first\n", encoding="utf-8")
    (tmp_path / "src/first.py").write_text("", encoding="utf-8")
    (tmp_path / "src/second.py").write_text("", encoding="utf-8")
    files = ("src/consumer.py", "src/first.py", "src/second.py")
    first = affected.build_edges(files)
    assert affected.build_edges(tuple(reversed(files))) is first
    assert affected.Edge("src/consumer.py", "src/first.py", "import") in first
    consumer.write_text("import src.second\n", encoding="utf-8")
    changed = affected.build_edges(files)
    assert affected.Edge("src/consumer.py", "src/second.py", "import") in changed
    assert affected.Edge("src/consumer.py", "src/first.py", "import") not in changed
