"""Cross-directory launch contract for supported executable entry points.

Helpers need no textual sys.path idiom. Bare script launchers must outrank a
foreign editable checkout; the geometry viewer uses ``python -P -m`` with an
explicit code root (no cwd import precedence). Every child records loaded paths.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _launch(tmp_path, arguments, *, module=False):
    probe = tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    foreign = tmp_path / "foreign"
    (foreign / "src").mkdir(parents=True, exist_ok=True)
    (foreign / "src/__init__.py").write_text("raise RuntimeError('WRONG_CHECKOUT_IMPORTED')\n")
    record = tmp_path / "loaded.json"
    (probe / "sitecustomize.py").write_text(
        "import atexit,json,os,sys\n"
        "def record():\n"
        "    paths={n:m.__file__ for n,m in sys.modules.items() "
        "if n.startswith('src.') and getattr(m,'__file__',None)}\n"
        "    with open(os.environ['C3R_MODULE_RECORD'],'w') as out: json.dump(paths,out)\n"
        "atexit.register(record)\n")
    # A deliberate foreign PYTHONPATH checks actual precedence, not merely
    # whether a bootstrap-looking AST node happens to exist in every helper.
    paths = [probe, ROOT, foreign] if module else [probe, foreign]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, paths)),
                       C3R_MODULE_RECORD=str(record))
    result = subprocess.run([sys.executable, *arguments], cwd=foreign,
                            env=environment, capture_output=True, text=True, timeout=45)
    loaded = json.loads(record.read_text())
    return result, loaded


@pytest.mark.parametrize("entry", [
    "scripts/tool_scripts/run_bim_agent.py",
])
def test_real_bare_entry_loads_own_checkout_from_foreign_directory(tmp_path, entry):
    result, loaded = _launch(tmp_path, [str(ROOT / entry), "--help"])
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert loaded, "the entry must actually import src, not just parse a trivial help flag"
    assert all(Path(path).resolve().is_relative_to(ROOT) for path in loaded.values()), loaded


def test_real_viewer_module_launch_executes_deferred_src_imports(tmp_path):
    geometry = tmp_path / "geometry.json"
    from tests.test_geometry_viewer import _GEO
    geometry.write_text(json.dumps(_GEO), encoding="utf-8")
    output = tmp_path / "viewer.html"
    result, loaded = _launch(tmp_path, ["-P", "-m", "scripts.tool_scripts.render_geometry_viewer",
        str(geometry), "--out", str(output)], module=True)
    assert result.returncode == 0, result.stderr
    assert output.is_file() and output.stat().st_size > 1000
    assert "src.agent.roles" in loaded  # actual rendering, not --help alone
    assert all(Path(path).resolve().is_relative_to(ROOT) for path in loaded.values()), loaded


def test_launch_probe_detects_missing_bootstrap(tmp_path):
    script = tmp_path / "broken.py"
    script.write_text("from src.agent.roles import ROOM_TYPES\n")
    result, _loaded = _launch(tmp_path, [str(script)])
    assert result.returncode != 0
    assert "WRONG_CHECKOUT_IMPORTED" in result.stderr
