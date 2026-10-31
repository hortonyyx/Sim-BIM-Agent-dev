"""Desktop evidence and recoverable publication, using only local Git remotes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from scripts.release import desktop_release as flow
from tests.test_product_repository import command, commit, initialize, release


class OfflineProcesses:
    """Git is real; GitHub, the updater and the verifier are controlled boundaries."""

    def __init__(self, desktop):
        self.desktop = desktop
        self.metadata = None
        self.fail_action = None
        self.fail_update = False
        self.calls = []

    def run(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "git":
            assert not any("https://" in a or "git@" in a for a in args)
            return subprocess.run(args, **kwargs)
        if args[:2] == ["gh", "api"]:
            assert "/releases/tags/" in args[2]
            if self.metadata is None:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.metadata), "")
        if args[:2] == ["gh", "release"]:
            action, tag, archive = args[2:5]
            assert action in {"create", "upload"}
            assert "--clobber" not in args
            if self.fail_action == action:
                self.fail_action = None
                raise subprocess.CalledProcessError(1, args, stderr="injected GitHub failure")
            data = Path(archive).read_bytes()
            self.metadata = {"tag_name": tag, "draft": False, "assets": [{
                "name": Path(archive).name, "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }]}
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "powershell.exe":
            assert args[args.index("-InstallRoot") + 1] == str(self.desktop)
            if self.fail_update:
                raise subprocess.CalledProcessError(1, args, stderr="injected updater failure")
            return subprocess.CompletedProcess(args, 0, "", "")
        if Path(args[0]) == self.desktop / ".venv/Scripts/python.exe":
            assert args[1:3] == ["-I", "-B"]
            assert args[args.index("--product") + 1] == str(self.desktop)
            output = Path(args[args.index("--output") + 1])
            output.mkdir(parents=True)
            write_report(output / "report.json", self.desktop)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"Unexpected external process in offline test: {args[0]}")

    def check_output(self, args, **kwargs):
        args = [str(a) for a in args]
        assert args[0] == "git"
        assert not any("https://" in a or "git@" in a for a in args)
        return subprocess.check_output(args, **kwargs)

    def mutations(self):
        return [a for a in self.calls if a[:2] == ["gh", "release"]]


def write_report(path, product):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "passed": True, "live_model_requests": 0, "product": str(product),
        "release_manifest_sha256": flow.sync.digest(product / flow.sync.MANIFEST),
    }), encoding="utf-8")
    return path


@pytest.fixture
def publication(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    initialize(seed)
    release(seed, "a")
    commit(seed)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(remote)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    cache, desktop, staging = tmp_path / "cache", tmp_path / "desktop", tmp_path / "staging"
    cache.mkdir()
    repo = cache / "repository"
    subprocess.run(["git", "clone", str(remote), str(repo)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    command(repo, "config", "user.name", "Offline Desktop Release Test")
    command(repo, "config", "user.email", "desktop-test@example.invalid")
    report = release(desktop, "b")
    notes = tmp_path / "notes.md"
    notes.write_text("Offline fixture release", encoding="utf-8")
    processes = OfflineProcesses(desktop)
    monkeypatch.setattr(flow, "REMOTE", str(remote))
    monkeypatch.setattr(flow, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(flow, "subprocess", SimpleNamespace(
        run=processes.run, check_output=processes.check_output))

    def identity(name, expected):
        assert (name, expected) == (flow.PRODUCT, flow.PRODUCT_ID)

    def source_identity(sha, *, published=False):
        # Source-repository identity is outside these desktop/publication cases.
        # This boundary prevents ever contacting the developer's actual origin.
        assert sha == "b" * 40

    monkeypatch.setattr(flow, "github_identity", identity)
    monkeypatch.setattr(flow, "source_identity", source_identity)
    flow.current_state(desktop, cache, repo, report=report)
    return SimpleNamespace(root=tmp_path, seed=seed, remote=remote, repo=repo, desktop=desktop,
                           cache=cache, staging=staging, report=report, notes=notes,
                           tag="release-offline-1", processes=processes)


def publish(p):
    return flow.publish(p.desktop, p.cache, p.staging, tag=p.tag, notes_file=p.notes)


def remote_refs(p):
    return command(p.repo, "ls-remote", "origin")


def record_git_publication(p):
    flow.sync.synchronize(p.desktop, p.repo, report=p.report, apply=True)
    commit(p.repo)
    revision = command(p.repo, "rev-parse", "HEAD").decode().strip()
    command(p.repo, "tag", "-a", p.tag, "-m", "Offline desktop candidate")
    command(p.repo, "push", "origin", "main", "--follow-tags")
    flow.save(p.cache / "publication.json", {
        "manifest_sha256": flow.sync.digest(p.desktop / flow.sync.MANIFEST),
        "tag": p.tag, "commit": revision,
    })
    return revision


def test_report_for_identical_bytes_at_another_directory_cannot_authorize_publication(publication):
    p = publication
    elsewhere = p.root / "other-release"
    shutil.copytree(p.desktop, elsewhere)
    report = write_report(p.root / "other-report.json", elsewhere)
    assert flow.sync.digest(elsewhere / flow.sync.MANIFEST) == flow.sync.digest(p.desktop / flow.sync.MANIFEST)
    state = flow.current_state(p.desktop, p.cache, p.repo, report=report)
    assert state["desktop_verified"] is False
    before = remote_refs(p), command(p.repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="actual desktop"):
        publish(p)
    assert before == (remote_refs(p), command(p.repo, "rev-parse", "HEAD"))
    assert not p.processes.mutations()


def test_desktop_managed_byte_change_after_verification_blocks_publication(publication):
    p = publication
    before = remote_refs(p), command(p.repo, "rev-parse", "HEAD")
    (p.desktop / "sim_bim.py").write_bytes(b"unverified edit\n")
    with pytest.raises(ValueError, match="missing or modified"):
        publish(p)
    assert before == (remote_refs(p), command(p.repo, "rev-parse", "HEAD"))
    assert not p.processes.mutations()


def test_equal_cloud_manifest_does_not_hide_a_missing_release(publication):
    p = publication
    record_git_publication(p)
    state = flow.current_state(p.desktop, p.cache, p.repo, report=p.report)
    assert state["desktop_verified"] is True
    assert state["git_synced"] is True
    assert state["release_ready"] is False
    assert state["status"] == "pending_publication"
    assert state["user_manual_testing"] == "not_recorded"
    assert not p.processes.mutations()


@pytest.mark.parametrize("failure", ["create", "upload"])
def test_retry_after_push_completes_same_release_without_recommit_or_retag(publication, failure):
    p = publication
    if failure == "upload":
        p.processes.metadata = {"tag_name": p.tag, "draft": False, "assets": []}
    p.processes.fail_action = failure
    with pytest.raises(subprocess.CalledProcessError):
        publish(p)
    first_commit = command(p.repo, "rev-parse", "HEAD")
    first_tag = command(p.repo, "rev-parse", p.tag)
    first_count = command(p.repo, "rev-list", "--count", "HEAD")
    first_refs = remote_refs(p)
    failed_staging = list(p.staging.iterdir())
    assert len(failed_staging) == 1 and (failed_staging[0] / "owner.json").is_file()
    pending = flow.current_state(p.desktop, p.cache, p.repo)
    assert pending["git_synced"] is True and pending["release_ready"] is False

    result = publish(p)

    assert result["status"] == "published" and result["release_ready"] is True
    assert result["desktop_verified"] is True
    assert command(p.repo, "rev-parse", "HEAD") == first_commit
    assert command(p.repo, "rev-parse", p.tag) == first_tag
    assert command(p.repo, "rev-list", "--count", "HEAD") == first_count
    assert remote_refs(p) == first_refs
    assert list(p.staging.iterdir()) == failed_staging  # Failed evidence stays; new successful work is removed.
    assert len(p.processes.mutations()) == 2
    assert flow.sync.read_release(p.repo) == flow.sync.read_release(p.desktop)
    assert publish(p)["release_ready"] is True
    assert len(p.processes.mutations()) == 2


def test_conflicting_existing_asset_is_not_replaced(publication):
    p = publication
    record_git_publication(p)
    p.processes.metadata = {"tag_name": p.tag, "draft": False, "assets": [{
        "name": f"Sim-BIM-Agent-{p.tag}.zip", "digest": "sha256:" + "0" * 64, "size": 1,
    }]}
    before = remote_refs(p), command(p.repo, "rev-parse", "HEAD"), json.dumps(p.processes.metadata)
    with pytest.raises(ValueError, match="asset differs"):
        publish(p)
    assert before == (remote_refs(p), command(p.repo, "rev-parse", "HEAD"), json.dumps(p.processes.metadata))
    assert not p.processes.mutations()


def test_existing_tag_for_another_commit_is_not_replaced(publication):
    p = publication
    command(p.repo, "tag", "-a", p.tag, "-m", "Older different product")
    command(p.repo, "push", "origin", "--tags")
    before = remote_refs(p), command(p.repo, "rev-parse", p.tag)
    with pytest.raises(ValueError, match="tag already exists"):
        publish(p)
    assert before == (remote_refs(p), command(p.repo, "rev-parse", p.tag))
    assert not p.processes.mutations()


@pytest.mark.parametrize("other_report", [False, True])
def test_same_version_stage_uses_desktop_verifier_when_backup_has_no_valid_report(publication, other_report):
    p = publication
    candidate = p.root / "same-version-release"
    shutil.copytree(p.desktop, candidate)
    if other_report:
        report = write_report(p.root / "candidate-report.json", candidate)
        flow.save(flow.BACKUP / "state.json", {"validation": {"report": str(report)}})
    else:
        assert not (flow.BACKUP / "state.json").exists()

    result = flow.stage(p.desktop, p.cache, p.staging, release=candidate)

    assert result["desktop_verified"] is True
    proof = Path(result["verification_report"])
    assert proof.is_relative_to(p.cache / "verification") and proof.is_file()
    assert json.loads(proof.read_bytes())["product"] == str(p.desktop)
    verifier_calls = [a for a in p.processes.calls if Path(a[0]) == p.desktop / ".venv/Scripts/python.exe"]
    assert len(verifier_calls) == 1
    assert not list(p.staging.iterdir())
    assert flow.sync.read_release(candidate) == flow.sync.read_release(p.desktop)
    assert not p.processes.mutations()


def test_failed_stage_preserves_its_staging_and_unrelated_files(publication):
    p = publication
    candidate = p.root / "candidate"
    shutil.copytree(p.desktop, candidate)
    unrelated = p.root / "old-blocked-cache"
    unrelated.mkdir()
    (unrelated / "keep").write_bytes(b"previous evidence")
    p.processes.fail_update = True
    with pytest.raises(subprocess.CalledProcessError):
        flow.stage(p.desktop, p.cache, p.staging, release=candidate)
    children = list(p.staging.iterdir())
    assert len(children) == 1 and (children[0] / "owner.json").is_file()
    assert (unrelated / "keep").read_bytes() == b"previous evidence"
    assert flow.sync.read_release(candidate) == flow.sync.read_release(p.desktop)
    assert not list((p.cache / "verification").glob("*/report.json"))


@pytest.mark.parametrize("damage", ["different_parent", "changed_marker"])
def test_staging_cleanup_rejects_unowned_paths_and_removes_only_its_own_work(tmp_path, damage):
    staging = tmp_path / "staging"
    owned = flow.owned_staging(staging)
    (owned / "temporary").write_bytes(b"generated")
    if damage == "different_parent":
        rejected = flow.owned_staging(tmp_path / "unrelated")
    else:
        rejected = flow.owned_staging(staging)
        marker = json.loads((rejected / "owner.json").read_bytes())
        marker["path"] = str(tmp_path / "not-this-directory")
        (rejected / "owner.json").write_text(json.dumps(marker))
    (rejected / "keep").write_bytes(b"do not delete")
    with pytest.raises(ValueError, match="Refusing cleanup"):
        flow.remove_staging(rejected, staging)
    assert (rejected / "keep").read_bytes() == b"do not delete"
    flow.remove_staging(owned, staging)
    assert not owned.exists() and rejected.is_dir()
