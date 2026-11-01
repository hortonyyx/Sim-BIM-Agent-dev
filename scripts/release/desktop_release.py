"""Develop -> install and verify the desktop -> publish those exact desktop bytes.

The desktop is the only installed product. Git and evidence below cache are
maintenance data, never another environment or a separately editable product.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release import sync_product_repo as sync

PRODUCT = "CRIBE-Lab/Sim-BIM-Agent"
PRODUCT_ID = 1440941560
DEVELOPMENT = "hortonyyx/Sim-BIM-Agent-dev"
DEVELOPMENT_ID = 1216622598
REMOTE = f"https://github.com/{PRODUCT}.git"
BACKUP = Path("D:/Sim-BIM-Agent/archive/product-backup")


def run(*args, cwd=None):
    return subprocess.check_output([str(a) for a in args], cwd=cwd, encoding="utf-8").strip()


def save(path, value):
    sync.atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def github_identity(name, expected):
    value = json.loads(run("gh", "api", f"repos/{name}"))
    if value["id"] != expected or value["full_name"].casefold() != name.casefold():
        raise ValueError(f"Repository identity mismatch: {name}")


def source_identity(commit, *, published=False):
    sync.git(ROOT, "cat-file", "-e", f"{commit}^{{commit}}")
    if published:
        github_identity(DEVELOPMENT, DEVELOPMENT_ID)
        if sync.git(ROOT, "remote", "get-url", "origin") != f"https://github.com/{DEVELOPMENT}.git":
            raise ValueError("Development origin does not identify the development repository")
        sync.git(ROOT, "fetch", "--quiet", "origin", "main")
        sync.git(ROOT, "merge-base", "--is-ancestor", commit, "origin/main")


def repository(cache):
    target = cache / "repository"
    github_identity(PRODUCT, PRODUCT_ID)
    if not target.exists():
        subprocess.run(["git", "clone", "--quiet", REMOTE, str(target)], check=True)
    if sync.git(target, "remote", "get-url", "origin") != REMOTE:
        raise ValueError("Publication cache origin does not identify the product repository")
    if (target / ".venv").exists():
        raise ValueError("Publication cache must not contain a runtime environment")
    sync.repository_state(target)
    sync.git(target, "fetch", "--quiet", "origin", "main", "--tags")
    return target


def cloud_snapshot(repo):
    """Validate the remote Git tree in memory; no second runnable export."""
    commit = sync.git(repo, "rev-parse", "origin/main")
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", commit])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tree:
        contents = {item.name: tree.extractfile(item).read() for item in tree if item.isfile()}
    manifest_bytes = contents[sync.MANIFEST]
    manifest = json.loads(manifest_bytes)
    if set(contents) != set(manifest["files"]) | {sync.MANIFEST}:
        raise ValueError("Cloud tree contains files outside its manifest")
    if any(hashlib.sha256(contents[n]).hexdigest() != h for n, h in manifest["files"].items()):
        raise ValueError("Cloud tree differs from its manifest")
    return commit, hashlib.sha256(manifest_bytes).hexdigest()


def current_state(install, cache, repo, *, report=None):
    manifest = sync.read_release(install)
    manifest_hash = sync.digest(install / sync.MANIFEST)
    previous_path = cache / "state.json"
    previous = json.loads(previous_path.read_bytes()) if previous_path.exists() else {}
    if report is None and previous.get("desktop_manifest_sha256") == manifest_hash:
        report = previous.get("verification_report")
    verified = False
    if report:
        try:
            sync.verification_for(install, report)
            verified = True
        except (OSError, ValueError):
            report = None
    cloud_commit, cloud_hash = cloud_snapshot(repo)
    publication = publication_for(cache, manifest_hash)
    git_synced = cloud_hash == manifest_hash
    if not publication and git_synced:
        latest = release_metadata(None)
        tag = latest.get("tag_name", "") if latest else ""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag) and remote_tag(repo, tag) == cloud_commit:
            candidate = {"manifest_sha256": manifest_hash, "tag": tag, "commit": cloud_commit}
            if release_status(repo, candidate):
                publication = candidate
                save(cache / "publication.json", publication)
    release_ready = release_status(repo, publication) if publication else False
    result = {"schema_version": "sim-bim-desktop-publication.v1", "install_root": str(install),
              "status": "published" if git_synced and release_ready is True else "pending_publication",
              "git_synced": git_synced, "release_ready": release_ready,
              "release_tag": publication["tag"] if publication else None,
              "source_commit": manifest["source_commit"], "desktop_manifest_sha256": manifest_hash,
              "desktop_verified": verified, "verification_report": str(report) if report else None,
              "cloud_commit": cloud_commit, "cloud_manifest_sha256": cloud_hash,
              "user_manual_testing": "not_recorded", "checked_at": datetime.now(timezone.utc).isoformat()}
    save(previous_path, result)
    return result


@contextmanager
def maintenance(cache):
    path = cache / "maintenance.lock"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def owned_staging(staging_root):
    parent = sync.safe_root(staging_root)
    parent.mkdir(parents=True, exist_ok=True)
    work = parent / uuid.uuid4().hex
    work.mkdir()
    (work / "owner.json").write_text(json.dumps({"schema": "sim-bim-staging.v1", "path": str(work)}))
    return work


def remove_staging(work, staging_root):
    work, parent = sync.safe_root(work), sync.safe_root(staging_root)
    marker = json.loads((work / "owner.json").read_bytes())
    if (work.parent != parent or len(work.name) != 32 or any(c not in "0123456789abcdef" for c in work.name)
            or marker != {"schema": "sim-bim-staging.v1", "path": str(work)}):
        raise ValueError("Refusing cleanup outside this owned staging invocation")
    # This exact marked directory was created for this invocation, never an input,
    # installation, historical release, Git cache or arbitrary computed directory.
    shutil.rmtree(work)


def verify_desktop(install, evidence):
    output = evidence / uuid.uuid4().hex
    python = install / ".venv/Scripts/python.exe"
    subprocess.run([str(python), "-I", "-B", str(ROOT / "scripts/release/verify_product.py"),
                    "--product", str(install), "--output", str(output)], check=True)
    report = output / "report.json"
    sync.verification_for(install, report)
    return report


def stage(install, cache, staging_root, *, release=None, offline=False):
    work = owned_staging(staging_root)
    completed = False
    try:
        if release is None:
            release = work / "incoming"
            subprocess.run([sys.executable, "-B", str(ROOT / "scripts/release/build_product.py"), str(release)], check=True)
        release = sync.safe_root(release)
        source_identity(sync.read_release(release)["source_commit"])
        evidence = cache / "verification"
        command = ["powershell.exe", "-NoProfile", "-File", str(ROOT / "scripts/release/update_product.ps1"),
                   "-Action", "update", "-Release", str(release), "-InstallRoot", str(install),
                   "-BackupRoot", str(BACKUP), "-WorkRoot", str(staging_root / "update"),
                   "-VerificationRoot", str(evidence)]
        if offline:
            command.append("-Offline")
        subprocess.run(command, check=True)
        update_path = BACKUP / "state.json"
        update = json.loads(update_path.read_bytes()) if update_path.exists() else {}
        report = update.get("validation", {}).get("report")
        try:
            sync.verification_for(install, report)
        except (OSError, TypeError, ValueError):
            # An already-identical install is a no-op for the updater; still obtain
            # exact desktop evidence before it can be used as a publication source.
            report = verify_desktop(install, evidence)
        result = current_state(install, cache, repository(cache), report=report)
        completed = True
        return result
    finally:
        if completed:
            remove_staging(work, staging_root)


def publication_for(cache, manifest_hash):
    path = cache / "publication.json"
    value = json.loads(path.read_bytes()) if path.exists() else {}
    return value if value.get("manifest_sha256") == manifest_hash else None


def release_metadata(tag):
    endpoint = f"repos/{PRODUCT}/releases/tags/{tag}" if tag else f"repos/{PRODUCT}/releases/latest"
    result = subprocess.run(["gh", "api", endpoint],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "HTTP 404" in result.stderr:
        return None
    raise RuntimeError(f"Could not inspect Release {tag}: {result.stderr.strip()}")


def remote_tag(repo, tag):
    refs = sync.git(repo, "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    if not refs:
        return None
    return next((line.split()[0] for line in refs.splitlines() if line.endswith("^{}")), refs.split()[0])


def release_status(repo, publication):
    tag, commit = publication["tag"], publication["commit"]
    target = remote_tag(repo, tag)
    if target is None:
        return False
    if target != commit:
        raise ValueError("Published tag identifies a different commit; never overwrite it")
    metadata = release_metadata(tag)
    if metadata is None:
        return False
    if metadata.get("draft"):
        raise ValueError("Existing draft Release requires inspection")
    name = f"Sim-BIM-Agent-{tag}.zip"
    asset = next((a for a in metadata.get("assets", []) if a["name"] == name), None)
    if asset is None:
        return False
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=zip", commit])
    if asset.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest() or asset.get("size") != len(archive):
        raise ValueError("Existing Release asset differs from the verified desktop; never overwrite it")
    return True


def publish(install, cache, staging_root, *, tag, notes_file):
    repo = repository(cache)
    state = current_state(install, cache, repo)
    if not state["desktop_verified"]:
        raise ValueError("Verify the actual desktop with stage before publication")
    source_identity(state["source_commit"], published=True)
    if state["status"] == "published":
        return state  # No empty commit, unnecessary version, or duplicate Release.
    if not tag or not notes_file or not notes_file.is_file():
        raise ValueError("A new publication needs --tag and --notes-file")
    sync.git(repo, "check-ref-format", f"refs/tags/{tag}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Use a tag that is also a safe archive filename")
    publication = publication_for(cache, state["desktop_manifest_sha256"])
    if publication:
        if publication["tag"] != tag or sync.git(repo, "rev-parse", "HEAD") != publication["commit"]:
            raise ValueError("Resume the same recorded tag and commit; inspect conflicting cache changes")
        if sync.digest(repo / sync.MANIFEST) != state["desktop_manifest_sha256"]:
            raise ValueError("Recorded publication cache no longer matches the desktop")
    else:
        if sync.git(repo, "tag", "--list", tag):
            raise ValueError("Publication tag already exists")
        if sync.git(repo, "rev-parse", "HEAD") != state["cloud_commit"]:
            raise ValueError("Cache differs from origin/main; fast-forward or inspect unpublished commits first")
        sync.synchronize(install, repo, report=state["verification_report"], apply=True)
        if sync.git(repo, "status", "--porcelain=v1"):
            sync.git(repo, "add", "--all")
            sync.git(repo, "commit", "--quiet", "-m", f"Publish desktop {tag} from {state['source_commit']}")
        publication = {"manifest_sha256": state["desktop_manifest_sha256"], "tag": tag,
                       "commit": sync.git(repo, "rev-parse", "HEAD")}
        save(cache / "publication.json", publication)
    sync.repository_state(repo)
    # Re-read desktop after copying and immediately before any cloud mutation.
    sync.read_release(install)
    sync.verification_for(install, state["verification_report"])
    if not sync.git(repo, "tag", "--list", tag):
        sync.git(repo, "tag", "-a", tag, "-m", f"Verified desktop release {tag}")
    elif sync.git(repo, "rev-parse", f"{tag}^{{commit}}") != publication["commit"]:
        raise ValueError("Local tag identifies another commit")
    sync.git(repo, "push", "origin", "main", "--follow-tags")
    work = owned_staging(staging_root)
    completed = False
    try:
        archive = work / f"Sim-BIM-Agent-{tag}.zip"
        sync.git(repo, "archive", "--format=zip", "--output", str(archive), "HEAD")
        if not release_status(repo, publication):
            if release_metadata(tag) is None:
                subprocess.run(["gh", "release", "create", tag, str(archive), "--repo", PRODUCT,
                                "--verify-tag", "--title", f"Sim-BIM Agent {tag}",
                                "--notes-file", str(notes_file), "--latest"], check=True)
            else:
                subprocess.run(["gh", "release", "upload", tag, str(archive), "--repo", PRODUCT], check=True)
        if not release_status(repo, publication):
            raise ValueError("Release attachment is not yet confirmed; retry the same tag")
        completed = True
    finally:
        if completed:
            remove_staging(work, staging_root)
    sync.git(repo, "fetch", "--quiet", "origin", "main", "--tags")
    return current_state(install, cache, repo, report=state["verification_report"])


def default_install():
    desktop = run("powershell.exe", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')")
    return Path(desktop) / "Sim BIM Agent"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "publish", "status"))
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--cache-root", type=Path, default=Path("D:/Sim-BIM-Agent/cache/product-publish"))
    parser.add_argument("--staging-root", type=Path, default=Path("D:/Sim-BIM-Agent/cache/release-staging"))
    parser.add_argument("--release", type=Path, help="Install an existing frozen release instead of building HEAD")
    parser.add_argument("--tag")
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    install = sync.safe_root(args.install_root or default_install())
    cache, staging = sync.safe_root(args.cache_root), sync.safe_root(args.staging_root)
    for a in (install, cache, staging, ROOT):
        for b in (install, cache, staging, ROOT):
            if a != b and a.is_relative_to(b):
                raise ValueError("Desktop, source, publication cache and staging must be separate")
    if len({install, cache, staging, ROOT}) != 4:
        raise ValueError("Desktop, source, publication cache and staging must differ")
    cache.mkdir(parents=True, exist_ok=True)
    with maintenance(cache):
        if args.action == "stage":
            result = stage(install, cache, staging, release=args.release, offline=args.offline)
        elif args.action == "publish":
            result = publish(install, cache, staging, tag=args.tag, notes_file=args.notes_file)
        else:
            result = current_state(install, cache, repository(cache))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
