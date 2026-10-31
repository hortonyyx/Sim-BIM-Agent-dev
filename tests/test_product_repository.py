"""Generated repository publication preserves bytes, ownership and provenance."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.release import sync_product_repo as publisher


def command(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def initialize(root):
    root.mkdir()
    command(root, "init", "-b", "main")
    command(root, "config", "user.name", "Offline Release Test")
    command(root, "config", "user.email", "release-test@example.invalid")
    command(root, "config", "core.autocrlf", "true")


def commit(root):
    command(root, "add", "--all")
    command(root, "commit", "-m", "Generated test release")


def release(root, tag, extras=None):
    payload = {name: b"unchanged\n" for name in publisher.REQUIRED}
    payload.update({"sim_bim.py": tag.encode() + b"\n",
                    "Start.cmd": b"@echo off\r\n",
                    ".gitattributes": b"* -text\n",
                    ".gitignore": b".venv/\n*.local\n"})
    payload.update(extras or {})
    for name, data in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    value = {"schema_version": "sim-bim-release.v1", "source_commit": tag * 40,
             "runtime_version": "runtime-test", "domain_version": "domain-test",
             "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    (root / publisher.MANIFEST).write_text(json.dumps(value), encoding="utf-8")
    report = root.parent / f"{root.name}-verified.json"
    report.write_text(json.dumps({"passed": True, "live_model_requests": 0, "product": str(root),
                                 "release_manifest_sha256": publisher.digest(root / publisher.MANIFEST)}), encoding="utf-8")
    return report


def publish(root, repository, report):
    publisher.synchronize(root, repository, report=report, apply=True)
    commit(repository)


def test_roundtrip_removes_retired_files_preserves_local_environment_and_clone_bytes(tmp_path):
    repo = tmp_path / "product"
    initialize(repo)
    old, new = tmp_path / "release-a", tmp_path / "release-b"
    old_report = release(old, "a", {"src/old/tool.py": b"old\n"})
    new_report = release(new, "b", {"src/new/tool.py": b"new\n"})
    plan = publisher.synchronize(old, repo)
    assert plan["source_commit"] == "a" * 40 and not (repo / publisher.MANIFEST).exists()
    publish(old, repo, old_report)
    (repo / ".venv").mkdir()
    (repo / ".venv/keep").write_bytes(b"local environment")
    publish(new, repo, new_report)
    assert not (repo / "src/old").exists()
    assert (repo / ".venv/keep").read_bytes() == b"local environment"
    assert publisher.repository_state(repo)["source_commit"] == "b" * 40
    clone = tmp_path / "clone"
    subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "--local", str(repo), str(clone)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert publisher.repository_state(clone) == publisher.repository_state(repo)
    exported = tmp_path / "exported"
    publisher.export(clone, exported)
    assert not (exported / ".git").exists() and not (exported / ".venv").exists()
    assert publisher.read_release(exported) == publisher.read_release(new)
    assert (exported / "Start.cmd").read_bytes() == b"@echo off\r\n"
    assert (exported / "sim_bim.py").read_bytes() == b"b\n"


@pytest.mark.parametrize("damage", ["local_edit", "extra_tracked", "untracked", "release_edit", "wrong_report"])
def test_preflight_rejects_unreviewed_changes_without_mutating_product(tmp_path, damage):
    repo = tmp_path / "product"
    initialize(repo)
    old, new = tmp_path / "release-a", tmp_path / "release-b"
    publish(old, repo, release(old, "a"))
    report = release(new, "b")
    if damage == "local_edit":
        (repo / "sim_bim.py").write_bytes(b"my change")
    elif damage == "extra_tracked":
        (repo / "unmanaged.txt").write_bytes(b"personal file")
        commit(repo)
    elif damage == "untracked":
        (repo / "unmanaged.txt").write_bytes(b"personal file")
    elif damage == "release_edit":
        (new / "sim_bim.py").write_bytes(b"modified after validation")
    else:
        evidence = json.loads(report.read_bytes())
        evidence["release_manifest_sha256"] = "0" * 64
        report.write_text(json.dumps(evidence), encoding="utf-8")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    with pytest.raises(ValueError):
        publisher.synchronize(new, repo, report=report, apply=True)
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_ignored_local_file_is_not_overwritten_by_new_payload(tmp_path):
    repo = tmp_path / "product"
    initialize(repo)
    old, new = tmp_path / "release-a", tmp_path / "release-b"
    publish(old, repo, release(old, "a"))
    report = release(new, "b", {"private.local": b"incoming"})
    (repo / "private.local").write_bytes(b"user-owned")
    with pytest.raises(ValueError, match="Unmanaged file"):
        publisher.synchronize(new, repo, report=report, apply=True)
    assert (repo / "private.local").read_bytes() == b"user-owned"
    assert publisher.read_release(repo)["source_commit"] == "a" * 40


def test_failed_copy_restores_previous_owned_payload(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    initialize(repo)
    old, new = tmp_path / "release-a", tmp_path / "release-b"
    publish(old, repo, release(old, "a", {"old.py": b"old"}))
    report = release(new, "b", {"new.py": b"new"})
    original = publisher.atomic_write
    failed = False

    def fail_once(path, contents):
        nonlocal failed
        if path.name == "sim_bim.py" and not failed:
            failed = True
            raise OSError("injected copy failure")
        original(path, contents)

    monkeypatch.setattr(publisher, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected"):
        publisher.synchronize(new, repo, report=report, apply=True)
    assert publisher.repository_state(repo)["source_commit"] == "a" * 40
    assert not (repo / "new.py").exists()


@pytest.mark.parametrize("path", ["../outside", ".git/config", ".GiT/config", ".venv/config", ".VENV/config", "C:/outside", "a\\b", "CON", "a/../b"])
def test_manifest_cannot_claim_git_environment_or_escape(tmp_path, path):
    root = tmp_path / "release"
    release(root, "a")
    manifest = json.loads((root / publisher.MANIFEST).read_bytes())
    manifest["files"][path] = "0" * 64
    (root / publisher.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe"):
        publisher.read_release(root)
