"""Publish a byte-bound native handoff, without another model or render pass."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from .viewer_publication import VIEWER_FILENAME, publish_attested_viewer


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def publication_target(run: Path, output_directory) -> Path:
    target = Path(output_directory).resolve()
    if target == run or target.is_relative_to(run):
        raise ValueError("public output must be a new directory outside the native run")
    if target.exists():
        raise FileExistsError(f"public output already exists; choose a new directory: {target}")
    return target


def publish_finished(run: Path, candidate: str, reply, output_directory) -> dict:
    from src.agent.runtime_delivery import attest_finished_handoff
    run = Path(run).resolve()
    target = publication_target(run, output_directory)
    reply.require_ok()
    attested = attest_finished_handoff(run, reply.data, candidate)
    if attested is None:
        raise ValueError("finish is incomplete, invalid or stale; public delivery was not created")
    selected = run / candidate
    source_path = selected / "source_model.json"
    source = json.loads(source_path.read_bytes())
    proposal_sha = _sha((selected / "proposal.json").read_bytes())
    report = json.loads((selected / "report.json").read_bytes())
    if (source.get("generation", {}).get("proposal_sha256") != proposal_sha
            or report.get("proposal_sha256") != proposal_sha
            or report.get("source_model_sha256") != attested["source_model_sha256"]):
        raise ValueError("candidate report/proposal no longer matches the finished source")
    root = Path(__file__).resolve().parents[2]
    renderer_names = ("src/agent/execution/source_proposal.py", "src/agent/geometry/source_bim.py",
                      "scripts/tool_scripts/render_geometry_viewer.py",
                      "scripts/tool_scripts/bim_agent_delivery_display.py",
                      "scripts/tool_scripts/bim_display_language.py")
    publication = {
        "schema": "bim_sdk_publication_v2", "candidate": candidate,
        "source_model_sha256": attested["source_model_sha256"],
        "native_run": str(run), "finish_receipt_file": reply.receipt_file,
        "native_artifact_sha256": attested["artifact_sha256"],
        "output_directory": str(target),
        "public_files": {VIEWER_FILENAME: attested["artifact_sha256"][f"{candidate}/viewer.html"]},
        "renderer_files_sha256": {name: _sha((root / name).read_bytes()) for name in renderer_names},
        "architectural_quality": "model_review_only; independent acceptance not implied",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}-{uuid.uuid4().hex}"
    record = run / "sdk" / "publications" / f"{uuid.uuid4().hex}.json"
    try:
        # Windows restricted tokens need the workspace's inherited ACL. Python's
        # mode-0700 tempfile directory excludes that token from its own child
        # writes. Keep ordinary inherited permissions on Windows; private mode
        # on POSIX. This changes no sandbox policy or existing directory ACL.
        staging.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        publish_attested_viewer(run, attested, staging)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            json.dumps(publication, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # One writer per run/output. No existing user output is replaced.
        if target.exists():
            raise FileExistsError(f"public output appeared during publication: {target}")
        staging.rename(target)
    except BaseException as error:
        record.unlink(missing_ok=True)
        # Do not mask a copy failure with cleanup failure, or rerun native finish.
        # A partial private staging folder is evidence, never a public delivery.
        error.add_note(f"Native finish remains at {run}; finish receipt: {reply.receipt_file}; "
                       f"publication target: {target}; staging (possibly partial): {staging}. "
                       "Publication outcome may be partial or unknown; inspect target and staging "
                       "before retrying. Do not rerun native finish merely to copy its artifacts.")
        raise
    return {"output_directory": str(target), "source_model": str(source_path),
            "viewer": str(target / VIEWER_FILENAME), "publication_file": str(record),
            "candidate": candidate, "source_model_sha256": attested["source_model_sha256"]}
