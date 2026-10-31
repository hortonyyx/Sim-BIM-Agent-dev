"""Publish a verified source release to a separate generated Git repository.

No commit, push, model request or dependency installation is performed here.
The desktop installer continues to consume an exported directory without .git.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

MANIFEST = "release-manifest.json"
REQUIRED = {"sim_bim.py", "Start.cmd", "setup.ps1", "README.md", "uv.lock",
            "pyproject.toml", ".python-version", ".gitignore", ".gitattributes",
            "src/agent_runtime/agent_versions.json"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), *args],
                                   encoding="utf-8", stderr=subprocess.PIPE).strip()


def safe_root(value):
    path = Path(value).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"Linked publication path: {part}")
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError("A filesystem root is not a product directory")
    return path


def member(root, relative):
    name = PurePosixPath(relative)
    if (not relative or name.is_absolute() or name.as_posix() != relative
            or any(c in relative for c in '\\:*?"<>|')
            or any(ord(c) < 32 for c in relative)
            or any(p.casefold() in {".", "..", ".git", ".venv"} or p.endswith((".", " "))
                   or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", p)
                   for p in name.parts)):
        raise ValueError(f"Unsafe payload path: {relative}")
    path = root.joinpath(*name.parts)
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"Linked payload path: {relative}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Payload escapes product: {relative}")
    return path


def read_release(root):
    root = safe_root(root)
    path = member(root, MANIFEST)
    value = json.loads(path.read_bytes())
    if (value.get("schema_version") != "sim-bim-release.v1"
            or not re.fullmatch(r"[a-f0-9]{40}", value.get("source_commit", ""))
            or not all(value.get(f"{line}_version") for line in ("runtime", "domain"))):
        raise ValueError("Invalid release identity")
    files = value.get("files")
    if not isinstance(files, dict) or not REQUIRED.issubset(files) or MANIFEST in files:
        raise ValueError("Incomplete product manifest")
    if len({n.casefold() for n in files}) != len(files):
        raise ValueError("Case-colliding payload paths")
    for name, expected in files.items():
        item = member(root, name)
        if (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)
                or not item.is_file() or digest(item) != expected):
            raise ValueError(f"Payload missing or modified: {name}")
    return value


def repository_state(root):
    root = safe_root(root)
    if not (root / ".git").is_dir() or Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Target must be the root of a separate Git repository")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Product repository has local changes; commit or inspect them first")
    tracked = set(filter(None, git(root, "ls-files", "-z").split("\0")))
    before = read_release(root) if (root / MANIFEST).exists() else None
    expected = set(before["files"]) | {MANIFEST} if before else set()
    if tracked != expected:
        raise ValueError("Tracked product files must exactly equal the current release manifest")
    return before


def verification_for(release, report):
    evidence = json.loads(Path(report).read_bytes())
    if (evidence.get("passed") is not True or evidence.get("live_model_requests") != 0
            or Path(evidence.get("product", "")).resolve() != release
            or evidence.get("release_manifest_sha256") != digest(release / MANIFEST)):
        raise ValueError("Verification must pass for this exact release directory and manifest")
    return evidence["release_manifest_sha256"]


def describe(before, after):
    old = before["files"] if before else {}
    new = after["files"]
    return {"source_commit": after["source_commit"], "runtime_version": after["runtime_version"],
            "domain_version": after["domain_version"], "files": len(new),
            "added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(n for n in old.keys() & new.keys() if old[n] != new[n])}


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sim-bim-publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_empty_parents(path, root):
    parent = path.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


@contextmanager
def publish_lock(root):
    path = root / ".git/sim-bim-publish.lock"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def synchronize(release, repository, *, report=None, apply=False):
    release, repository = safe_root(release), safe_root(repository)
    if release.is_relative_to(repository) or repository.is_relative_to(release):
        raise ValueError("Release and product repository must be separate")
    if (release / ".git").exists():
        raise ValueError("Publish a verified export, not a Git checkout")
    after = read_release(release)
    before = repository_state(repository)
    plan = describe(before, after)
    if not apply:
        return plan
    if report is None:
        raise ValueError("sync requires --verification")
    verified_manifest_hash = verification_for(release, report)
    with publish_lock(repository):
        # Recheck after acquiring our maintenance lock. Do not overwrite local edits.
        before = repository_state(repository)
        old = before["files"] if before else {}
        names = set(old) | set(after["files"]) | {MANIFEST}
        for name in names:
            path = member(repository, name)
            if path.exists() and not path.is_file():
                raise ValueError(f"Directory conflicts with payload: {name}")
            if name not in old and name != MANIFEST and path.exists():
                raise ValueError(f"Unmanaged file would be overwritten: {name}")
            for parent in path.parents:
                if parent == repository:
                    break
                if parent.is_file():
                    raise ValueError(f"File conflicts with payload directory: {name}")
        saved = {name: member(repository, name).read_bytes() if member(repository, name).is_file() else None
                 for name in names}
        incoming = {name: member(release, name).read_bytes() for name in set(after["files"]) | {MANIFEST}}
        # Capture verified bytes before mutation; source changes cannot slip through a later copy.
        if any(hashlib.sha256(incoming[n]).hexdigest() != h for n, h in after["files"].items()):
            raise ValueError("Release changed while preparing publication")
        if hashlib.sha256(incoming[MANIFEST]).hexdigest() != verified_manifest_hash:
            raise ValueError("Release manifest changed while preparing publication")
        try:
            for name in sorted(incoming):
                if saved[name] != incoming[name]:
                    atomic_write(member(repository, name), incoming[name])
            for name in sorted(old.keys() - after["files"].keys()):
                path = member(repository, name)
                path.unlink()
                remove_empty_parents(path, repository)
            read_release(repository)
        except BaseException:
            # Restore only files this invocation owns; preserve unrelated data and .git.
            for name in sorted(names):
                path = member(repository, name)
                if saved[name] is None:
                    if path.is_file():
                        path.unlink()
                        remove_empty_parents(path, repository)
                else:
                    atomic_write(path, saved[name])
            raise
    return {**plan, "synchronized": True, "commit_required": bool(git(repository, "status", "--porcelain=v1"))}


def export(repository, destination):
    repository, destination = safe_root(repository), safe_root(destination)
    if destination.exists() or destination.is_relative_to(repository) or repository.is_relative_to(destination):
        raise ValueError("Export needs a new directory outside the product repository")
    value = repository_state(repository)
    if value is None:
        raise ValueError("Product repository has no release")
    destination.mkdir(parents=True)
    for name in sorted(set(value["files"]) | {MANIFEST}):
        target = member(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(member(repository, name), target)
    read_release(destination)
    return {"directory": str(destination), "source_commit": value["source_commit"], "files": len(value["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "sync", "check", "export"))
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.action in {"plan", "sync"}:
        if args.release is None:
            parser.error("--release is required")
        result = synchronize(args.release, args.repository, report=args.verification, apply=args.action == "sync")
        # Keep CLI summaries compact; exact filenames remain available via Git diff.
        result = {k: len(v) if k in {"added", "removed", "changed"} else v for k, v in result.items()}
    elif args.action == "export":
        if args.destination is None:
            parser.error("--destination is required")
        result = export(args.repository, args.destination)
    else:
        value = repository_state(args.repository)
        if value is None:
            raise ValueError("Product repository has no release")
        result = {"verified": True, "source_commit": value["source_commit"], "files": len(value["files"])}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
