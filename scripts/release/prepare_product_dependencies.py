"""Regenerate the product lock from the reviewed development-locked versions.

Run explicitly during release engineering, never during product execution or a
normal build. The resolver works offline and any dependency version not already
in the development lock is rejected.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.product_projection import (
    LOCK, LOCK_METADATA, PROFILE, dependency_project, json_bytes, project_sources, sha,
)


def prepare(staging):
    staging = staging.expanduser().resolve()
    if staging.exists() or staging.is_relative_to(ROOT):
        raise ValueError("Use a new dependency staging directory outside the checkout")
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                    cwd=ROOT).decode().split("\0")
    sources = {name: (ROOT / name).read_bytes() for name in names if name and (ROOT / name).is_file()}
    _, _, profile, imports = project_sources(sources)
    pyproject, requirements = dependency_project(sources, profile, imports)
    staging.mkdir(parents=True)
    (staging / "pyproject.toml").write_bytes(pyproject)
    (staging / "README.md").write_text("Dependency resolution only.\n", encoding="utf-8")
    (staging / "uv.lock").write_bytes(sources["uv.lock"])
    subprocess.run(["uv", "lock", "--offline", "--project", str(staging)], check=True)
    data = (staging / "uv.lock").read_bytes()
    old = tomllib.loads(sources["uv.lock"].decode("utf-8"))
    new = tomllib.loads(data.decode("utf-8"))
    versions = {(p["name"], p.get("version"), str(p.get("source"))) for p in old["package"]}
    unexpected = [p["name"] for p in new["package"] if p["name"] != "sim-bim-agent"
                  and (p["name"], p.get("version"), str(p.get("source"))) not in versions]
    if unexpected:
        raise ValueError(f"Product resolution changed development-locked dependencies: {unexpected}")
    (ROOT / LOCK).write_bytes(data)
    metadata = {"schema_version": "sim-bim-product-dependencies.v1",
                "profile_sha256": sha(sources[PROFILE]), "development_lock_sha256": sha(sources["uv.lock"]),
                "product_pyproject_sha256": sha(pyproject), "product_lock_sha256": sha(data),
                "direct_dependencies": requirements, "locked_package_count": len(new["package"])}
    (ROOT / LOCK_METADATA).write_bytes(json_bytes(metadata))
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staging", type=Path)
    print(json_bytes(prepare(parser.parse_args().staging)).decode("utf-8"))
