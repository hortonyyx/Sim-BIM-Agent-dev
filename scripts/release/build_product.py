"""Build the current BIM product from one committed development tree."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.product_projection import (
    PROFILE, REGISTRY, json_bytes, project_sources, register_projection, sha, with_dependencies,
)


def committed_sources(root, commit):
    """Read archive members as bytes; never extract archive paths to disk."""
    archived = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as tree:
        return {item.name: tree.extractfile(item).read() for item in tree if item.isfile()}


def build(destination: Path):
    from src.agent_runtime.agent_registry import release_records, source_commit
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError("Use a new destination; existing releases are never overwritten")
    if destination.is_relative_to(ROOT):
        raise ValueError("Build releases outside the development checkout")
    # Reports may remain uncommitted, but all source, dependency and projection
    # implementation inputs must belong to the one sealed source commit.
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "src", "scripts", "product",
                    "pyproject.toml", "uv.lock", ".python-version", "sim_bim.py"], cwd=ROOT,
                   check=True, stdout=subprocess.DEVNULL)
    release_records(ROOT)
    commit = source_commit(ROOT)
    sources = committed_sources(ROOT, commit)
    payload, origins, profile, imports = project_sources(sources)
    requirements = with_dependencies(sources, payload, origins, profile, imports)
    destination.mkdir(parents=True)
    for relative, data in sorted(payload.items()):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    registry, projection = register_projection(destination, sources=sources, commit=commit, profile=profile)
    registry_bytes = json_bytes(registry)
    (destination / REGISTRY).write_bytes(registry_bytes)
    payload[REGISTRY] = registry_bytes
    origins[REGISTRY] = {"source": REGISTRY, "source_sha256": sha(sources[REGISTRY]),
                         "transform": "current_product_registration"}
    files = {name: sha(data) for name, data in sorted(payload.items())}
    manifest = {"schema_version": "sim-bim-release.v1", "source_commit": commit,
                "runtime_version": registry["current_runtime_version"],
                "domain_version": registry["current_domain_version"], "files": files,
                "projection": {**projection, "profile": profile, "file_origins": origins,
                               "direct_dependencies": requirements},
                "environment": "Rebuild with uv sync --frozen --no-dev --no-install-project; .venv is not payload"}
    (destination / "release-manifest.json").write_bytes(json_bytes(manifest))
    release_records(destination)
    return {"directory": str(destination), "files": len(files),
            "payload_bytes": sum(map(len, payload.values())), "source_commit": commit,
            "runtime_version": manifest["runtime_version"], "domain_version": manifest["domain_version"],
            "direct_dependencies": len(requirements)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(json.dumps(build(parser.parse_args().destination), indent=2))
