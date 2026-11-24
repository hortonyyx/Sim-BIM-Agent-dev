import concurrent.futures
import hashlib
import json
from pathlib import Path
import threading

import pytest

from src.agent.viewer_publication import VIEWER_FILENAME, publish_attested_viewer
from src.dsh_runtime.product import publish_result


def finished(root: Path, candidate: str, body: bytes) -> dict:
    selected = root / candidate
    selected.mkdir(parents=True)
    (selected / "viewer.html").write_bytes(body)
    return {"origin": "domain_tool", "tool_name": "finish_bim", "candidate": candidate,
            "artifact_sha256": {f"{candidate}/viewer.html": hashlib.sha256(body).hexdigest()}}


def test_product_returns_direct_bound_model_and_retries_without_clobber(tmp_path, capsys):
    native = tmp_path / "private/dsh/work/bim"
    proof = finished(native, "candidate_03", b"portable model")
    (native / "delivery.html").write_bytes(b"internal report")
    outcome = {"status": "delivered", "delivered": True, "proof": proof}
    dest = tmp_path / VIEWER_FILENAME
    for _ in range(2):
        publish_result(tmp_path, outcome, tmp_path)
        assert json.loads(capsys.readouterr().out) == {"status": "delivered", "viewer": str(dest)}
        assert dest.read_bytes() == b"portable model"
    dest.write_bytes(b"user edits")
    with pytest.raises(FileExistsError):
        publish_result(tmp_path, outcome, tmp_path)
    assert dest.read_bytes() == b"user edits"
    dest.unlink()
    (native / "candidate_03/viewer.html").write_bytes(b"changed since finish")
    with pytest.raises(ValueError, match="changed after finish"):
        publish_result(tmp_path, outcome, tmp_path)
    with pytest.raises(ValueError, match="without a delivered"):
        publish_result(tmp_path, {"delivered": False}, tmp_path)
    assert not dest.exists() and not list(tmp_path.glob(".sim-bim-*"))


def test_distinct_concurrent_viewers_cannot_replace_each_other(tmp_path):
    run = tmp_path / "bim"
    proofs = [finished(run, f"candidate_{n}", str(n).encode()) for n in range(6)]
    ready = threading.Barrier(len(proofs))

    def publish(index):
        ready.wait(timeout=5)
        try:
            publish_attested_viewer(run, proofs[index], tmp_path)
            return index
        except FileExistsError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(proofs)) as pool:
        winners = [i for i in pool.map(publish, range(len(proofs))) if i is not None]
    assert winners == [int((tmp_path / VIEWER_FILENAME).read_text())]
    assert not list(tmp_path.glob(".sim-bim-*"))
