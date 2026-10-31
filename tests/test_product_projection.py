"""The slim distribution keeps current execution integrity without old systems."""
import json
from pathlib import Path
import subprocess
import tomllib

import numpy as np
import pytest

from scripts.release import product_projection as projection
from src.agent_runtime import agent_registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sources():
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                    cwd=ROOT).decode().split("\0")
    return {name: (ROOT / name).read_bytes() for name in names if name and (ROOT / name).is_file()}


def test_forbidden_import_and_unresolved_dynamic_import_fail_closed(sources):
    for extra, message in [(b"\nimport src.rag.rag\n", "excluded subsystem"),
                           (b"\nimport importlib\nimportlib.import_module(variable)\n", "Unresolved dynamic")]:
        changed = dict(sources)
        changed["src/agent/product_cli.py"] += extra
        with pytest.raises(ValueError, match=message):
            projection.project_sources(changed)


def test_runtime_dependency_lock_only_retains_development_versions(sources):
    payload, origins, profile, imports = projection.project_sources(sources)
    requirements = projection.with_dependencies(sources, payload, origins, profile, imports)
    product = tomllib.loads(payload["pyproject.toml"].decode())
    assert product["project"]["name"] == "sim-bim-agent"
    assert set(product["project"]["scripts"]) == {"sim-bim-agent"}
    assert "pyjwt[crypto]==2.12.1" in requirements
    old = tomllib.loads(sources["uv.lock"].decode())["package"]
    new = tomllib.loads(payload["uv.lock"].decode())["package"]
    versions = {(p["name"], p.get("version"), str(p.get("source"))) for p in old}
    assert all((p["name"], p.get("version"), str(p.get("source"))) in versions
               for p in new if p["name"] != "sim-bim-agent")
    assert not {"eppy", "idfpy", "langgraph", "langchain", "qdrant-client", "google-genai", "fastmcp"} & {p["name"] for p in new}


def test_projection_identity_membership_and_old_version_rejection(tmp_path, sources, monkeypatch):
    profile = json.loads(sources[projection.PROFILE])
    for name in ("src/__init__.py", "src/agent/__init__.py", "src/agent_runtime/a.py", "scripts/tool.py",
                 "pyproject.toml", "uv.lock"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(agent_registry, "_fingerprints", lambda *args: {
        "tool_catalog_sha256": {k: "a" * 64 for k in agent_registry._CATALOG_MODES},
        "mode_fingerprints": {"single_model/coordinator": {"tools": "b" * 64}}})
    first, _ = projection.register_projection(tmp_path, sources=sources, commit="a" * 40, profile=profile)
    second, _ = projection.register_projection(tmp_path, sources=sources, commit="a" * 40, profile=profile)
    assert first == second
    assert len(first["runtime_versions"]) == len(first["domain_versions"]) == 1
    assert first["aliases"] == {}
    assert first["current_domain_version"].startswith("domain-product-v1-")
    (tmp_path / projection.REGISTRY).write_bytes(projection.json_bytes(first))
    agent_registry.release_records(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        agent_registry.register_versions(tmp_path)
    with pytest.raises(agent_registry.AgentVersionMismatch, match="not registered"):
        agent_registry.agent_version_record(tmp_path, "domain-v153-20261026")
    for damage in ("alias", "extra_record"):
        edited = json.loads(projection.json_bytes(first))
        if damage == "alias":
            edited["aliases"]["domain-v153-20261026"] = edited["current_domain_version"]
        else:
            edited["domain_versions"]["domain-v153-20261026"] = dict(edited["domain_versions"][edited["current_domain_version"]])
        (tmp_path / projection.REGISTRY).write_bytes(projection.json_bytes(edited))
        with pytest.raises(agent_registry.AgentVersionMismatch, match="historical|only its current"):
            agent_registry.release_records(tmp_path)
    (tmp_path / projection.REGISTRY).write_bytes(projection.json_bytes(first))
    # Namespace parents can execute code on import; they must not bypass membership.
    (tmp_path / "scripts/__init__.py").write_text("# injected\n", encoding="utf-8")
    with pytest.raises(agent_registry.AgentVersionMismatch, match="file mismatch"):
        agent_registry.release_records(tmp_path)
    changed, _ = projection.register_projection(tmp_path, sources=sources, commit="a" * 40, profile=profile)
    assert changed["current_domain_version"] != first["current_domain_version"]
    (tmp_path / "scripts/__init__.py").unlink()
    edited = json.loads(projection.json_bytes(first))
    edited["domain_versions"][edited["current_domain_version"]]["source_commit"] = "b" * 40
    (tmp_path / projection.REGISTRY).write_bytes(projection.json_bytes(edited))
    with pytest.raises(agent_registry.AgentVersionMismatch, match="immutable projection"):
        agent_registry.release_records(tmp_path)
    (tmp_path / projection.REGISTRY).write_bytes(projection.json_bytes(first))
    manifest = {"schema_version": "sim-bim-release.v1", "source_commit": "b" * 40,
                "runtime_version": first["current_runtime_version"], "domain_version": first["current_domain_version"],
                "projection": first["product_projection"],
                "files": {p.relative_to(tmp_path).as_posix(): projection.sha(p.read_bytes())
                          for p in tmp_path.rglob("*") if p.is_file()}}
    (tmp_path / "release-manifest.json").write_bytes(projection.json_bytes(manifest))
    with pytest.raises(agent_registry.AgentVersionMismatch, match="source identity"):
        agent_registry.release_records(tmp_path)


def test_old_validator_and_current_geometry_share_the_same_ring_implementation():
    from src.agent.geometry import build
    from src.agent.geometry.ring_order import canonicalize_ring_vertices
    from src.validator.data_model import canonicalize_ring_vertices as legacy
    assert legacy is canonicalize_ring_vertices
    assert build.canonicalize_ring_vertices is canonicalize_ring_vertices
    ring = np.array([[0., 0., 0.], [2., 0., 0.], [2., 1., 0.], [1., 1., 0.], [1., 2., 0.], [0., 2., 0.]])
    normal = np.array([0., 0., 1.])
    assert np.array_equal(legacy(ring, normal), canonicalize_ring_vertices(ring[::-1], normal))
