"""Build a new immutable source distribution from a committed, verified checkout."""
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


def selected_payload(tracked):
    selected = {}
    for name in tracked:
        if name.startswith(("src/", "scripts/tool_scripts/", "data/")) or name in {
                "pyproject.toml", "uv.lock", ".python-version", "sim_bim.py",
                "scripts/glm_code.py", "scripts/glm_code.sh"}:
            if Path(name).name in {"affected_tests.py", "affected_tests_rules.yaml"}:
                continue
            selected[name] = name
        if name.startswith("product/"):
            selected[name.removeprefix("product/")] = name
    return selected


def build(destination: Path):
    from src.agent_runtime.agent_registry import release_records, source_commit
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError("Use a new destination; existing releases are never overwritten")
    if destination.is_relative_to(ROOT):
        raise ValueError("Build releases outside the development checkout")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    selected = selected_payload(filter(None, tracked))
    if "README.md" not in selected or "Start.cmd" not in selected:
        raise ValueError("Commit product entry files before building")
    # Only payload changes affect this build; in-progress reports can remain unstaged.
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", *selected.values()], cwd=ROOT,
                   check=True, stdout=subprocess.DEVNULL)
    records = release_records(ROOT)
    commit = source_commit(ROOT)
    for original in selected.values():
        path = ROOT / original
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Invalid payload path: {original}")
    # Read the immutable Git tree, not checkout bytes affected by core.autocrlf
    # or nested attributes. Windows launch scripts have an explicit output format.
    archived = subprocess.check_output(["git", "archive", "--format=tar", commit, "--", *selected.values()], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as tree:
        committed = {}
        for relative, original in selected.items():
            item = tree.getmember(original)
            if not item.isfile():
                raise ValueError(f"Invalid committed payload path: {original}")
            data = tree.extractfile(item).read()
            if Path(relative).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            committed[relative] = data
    destination.mkdir(parents=True)
    files = {}
    for relative, original in sorted(selected.items()):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(committed[relative])
        files[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"schema_version": "sim-bim-release.v1", "source_commit": commit,
                "runtime_version": records["runtime"]["version_id"],
                "domain_version": records["domain"]["version_id"], "files": files,
                "environment": "Rebuild with uv sync --frozen --no-dev --no-install-project; .venv is not payload"}
    (destination / "release-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    release_records(destination)
    return {"directory": str(destination), "files": len(files),
            "payload_bytes": sum((destination / name).stat().st_size for name in files),
            "source_commit": manifest["source_commit"], "runtime_version": manifest["runtime_version"],
            "domain_version": manifest["domain_version"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(json.dumps(build(parser.parse_args().destination), indent=2))
