import io
import json
from pathlib import Path

from PIL import Image
from starlette.testclient import TestClient
import pytest

from src.agent.runtime_configuration import load_configuration
from src.agent.runtime_entry import prepare_inputs
from src.workbench import runs
from src.workbench.server import Workbench, create_app
from src.workbench.storage import read_json


ORIGIN = "http://127.0.0.1"
COORD = {"provider": "chatgpt-subscription", "model": "gpt-6.2-sol"}
WORKER = {"provider": "deepseek", "model": "deepseek-flash"}


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(tmp_path / "settings"))
    backend = Workbench()
    monkeypatch.setattr(backend.connections, "view", lambda _: [
        {"provider": s["provider"], "connected": True, "models": [{"id": s["model"], "name": s["model"]}]}
        for s in [COORD, WORKER]])
    client = TestClient(create_app(backend, warm=False), base_url=ORIGIN, headers={"Origin": ORIGIN})
    root = tmp_path / "workspace"
    assert client.post("/api/workspace", json={"path": str(root)}).json()["workspace"] == str(root)
    return client, root


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (10, 12), color).save(out, format="PNG")
    return out.getvalue()


def test_order_caption_copy_and_production_config(workbench, monkeypatch, tmp_path):
    client, root = workbench
    images = [png(color) for color in ["red", "green", "blue"]]
    form = {"workspace": str(root), "revision": "0", "caption": "底层、标准层、顶层"}
    files = [("files", ("Plan.png", content, "image/png")) for content in images]
    block = client.post("/api/blocks", data=form, files=files).json()["project"]["blocks"][0]
    assert [client.get(a["url"]).content for a in block["assets"]] == images
    assert len(list((root / "materials").rglob("Plan.png"))) == 3
    payload = {"workspace": str(root), "revision": 1, "blocks": [
        {"id": block["id"], "caption": "first, typical, roof", "assetIds": [a["id"] for a in reversed(block["assets"])]}],
        "taskBrief": "Keep the entrance.", "coordinator": COORD, "workers": [WORKER],
        "thicknessCandidatesMm": {"glazing": [24, 16.8, 24], "horizontal": [160.5, 200]}}
    assert client.post("/api/project", json=payload).status_code == 200
    stale = client.post("/api/project", json=payload)
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    state = client.post("/api/preferences", json={"locale": "en", "workspace": str(root), "revision": 2}).json()
    assert state["project"]["taskBrief"] == payload["taskBrief"]
    revision = state["project"]["revision"]
    called = []
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *args, **kwargs: called.append(args))
    result = client.post("/api/runs", json={"workspace": str(root), "revision": revision}).json()["run"]
    assert result["status"] == "running" and called
    directory = root / "runs" / result["id"]
    assert runs.latest(root)["id"] == result["id"]
    assert client.post("/api/runs", json={"workspace": str(root), "revision": revision}).json()["run"] == result
    assert len(called) == 1
    monkeypatch.undo()  # restore subprocess before validating the actual CLI case
    case = load_configuration(directory / "case.json")["cases"][0]
    assert case["provider"] == COORD["provider"] and case["workers"] == [WORKER]
    frozen = read_json(directory / "input.json")
    expected = {"glazing": [16.8, 24], "horizontal": [160.5, 200]}
    assert state["project"]["thicknessCandidatesMm"] == expected
    assert frozen["thicknessCandidatesMm"] == case["thickness_candidates_mm"] == expected
    assets = frozen["blocks"][0]["assets"]
    assert [a["name"] for a in assets] == ["Plan.png"] * 3
    assert [(directory / a["path"]).read_bytes() for a in assets] == images[::-1]
    assert len({a["file"].casefold() for a in assets}) == 3
    run, guide, task = prepare_inputs(tmp_path / "production", images=directory / "inputs/images",
        mesh=None, building_input=None, scope=case["scope"], image_kind=case["image_kind"], max_candidates=4,
        orchestrator=True, blocks=case["blocks"], locale=case["locale"],
        thickness_candidates_mm=case["thickness_candidates_mm"])
    assert "first, typical, roof" in task and "Keep the entrance." in task and "English" in guide
    assert all(a["file"] in task for a in assets)
    assert read_json(run / "inputs.json")["blocks"] == frozen["blocks"]
    assert read_json(run / "inputs.json")["locale"] == "en"
    assert read_json(run / "inputs.json")["thickness_candidates_mm"] == expected
    assert read_json(run / "inputs.json")["spatial_assessment_policy"] == "object_evidence_v1"
    assert "160.5" in task


def test_thickness_candidates_legacy_draft_and_invalid_updates(workbench):
    client, root = workbench
    path = root / "project.json"
    old = read_json(path)
    del old["thicknessCandidatesMm"]
    path.write_text(json.dumps(old), encoding="utf-8")
    state = client.get("/api/state").json()
    assert set(state["thicknessPresetsMm"]) == {"wall", "horizontal", "roof", "glazing"}
    assert state["project"]["thicknessCandidatesMm"] == {}
    payload = dict(old, workspace=str(root), thicknessCandidatesMm={"wall": [201.5], "roof": []})
    saved = client.post("/api/project", json=payload).json()["project"]
    assert saved["thicknessCandidatesMm"] == {"wall": [201.5]}
    payload["revision"] = saved["revision"]
    for invalid in ({"wall": [True]}, {"wall": [-1]}, {"wall": "200"}, {"door": [55]}):
        assert client.post("/api/project", json=dict(payload, thicknessCandidatesMm=invalid)).status_code == 400
        assert read_json(path) == saved
    del payload["thicknessCandidatesMm"]
    assert client.post("/api/project", json=payload).json()["project"]["thicknessCandidatesMm"] == {"wall": [201.5]}


def test_atomic_import_block_edit_and_workspace_guards(workbench):
    client, root = workbench
    form = {"workspace": str(root), "revision": "0", "caption": ""}
    good = png("black")
    result = client.post("/api/blocks", data=form, files=[("files", ("A.png", good)), ("files", ("bad.png", b"bad"))])
    assert result.status_code == 400
    assert not read_json(root / "project.json")["blocks"]
    block = client.post("/api/blocks", data=form, files=[("files", ("A.png", good))]).json()["project"]["blocks"][0]
    form.update(revision="1", blockId=block["id"], assetOrder=json.dumps(["@0", block["assets"][0]["id"]]))
    edited = client.post("/api/blocks", data=form, files=[("files", ("A.png", png("red")))]).json()["project"]
    assert [a["name"] for a in edited["blocks"][0]["assets"]] == ["A.png", "A.png"]
    assert edited["blocks"][0]["assets"][1]["id"] == block["assets"][0]["id"]
    assert client.post("/api/preferences", json={"locale": "en"}, headers={"Origin": "https://example.org"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "example.org"}).status_code == 403
    other = root.parent / "other"
    other.mkdir()
    (other / "project.json").write_text('{"foreign":true}', encoding="utf-8")
    assert client.post("/api/workspace", json={"path": str(other)}).status_code == 400
    assert read_json(other / "project.json") == {"foreign": True}
    assert client.get("/api/state").json()["workspace"] == str(root)


def test_text_only_block_and_output_resolution(workbench):
    client, root = workbench
    result = client.post("/api/blocks", files={"caption": (None, "楼顶采用钢结构。"), "workspace": (None, str(root)),
                                               "revision": (None, "0")})
    assert result.json()["project"]["blocks"][0]["assets"] == []
    identity = "run-20261124-123456-abcdf012"
    directory = runs.run_dir(root, identity)
    bim = directory / "production/private/dsh/work/bim"
    (bim / "candidate_01").mkdir(parents=True)
    (bim / "delivery.html").write_text("report", encoding="utf-8")
    (bim / "candidate_01/viewer.html").write_text("viewer", encoding="utf-8")
    base = f"/results/{runs.workspace_tag(root)}/{identity}/"
    assert client.get(base + "delivery.html").status_code == 404
    (directory / "status.json").write_text(json.dumps({"status": "completed", "report": base + "delivery.html"}))
    assert client.get(base + "delivery.html").text == "report"
    assert client.get(base + "candidate_01/viewer.html").text == "viewer"
    (bim / "image_overlays").mkdir()
    overlay = "image_overlays/overlay_005.png"
    (bim / overlay).write_bytes(png("blue"))
    (bim / "delivery.json").write_text(json.dumps({"source_image_feedback": {
        "current_source_projections": [{"overlay_image": overlay}]}}), encoding="utf-8")
    assert client.get(base + overlay).content == png("blue")
    assert client.get(base + "image_overlays/unlisted.png").status_code == 404
    assert client.get(base + "inputs.json").status_code == 404
    model = b"<!doctype html><title>Sim-BIM</title>"
    (directory / "Sim-BIM.html").write_bytes(model)
    (directory / "status.json").write_text(json.dumps({"status": "completed", "viewer": base + "Sim-BIM.html"}))
    for private in ["delivery.html", "delivery.json", "candidate_01/viewer.html", overlay]:
        assert client.get(base + private).status_code == 404
    opened = client.get(base + "Sim-BIM.html")
    assert opened.content == model and "content-disposition" not in opened.headers
    download = client.get(base + "Sim-BIM.html?download=1")
    assert download.content == model
    assert download.headers["content-disposition"] == 'attachment; filename="Sim-BIM.html"'


def test_recover_unreadable_workspace_and_reject_stale_language(workbench):
    client, root = workbench
    (root / "project.json").write_text("{", encoding="utf-8")
    state = client.get("/api/state").json()
    assert state["workspace"] is None and state["workspaceError"]
    other = root.parent / "new-workspace"
    assert client.post("/api/workspace", json={"path": str(other)}).json()["workspace"] == str(other)
    stale = client.post("/api/preferences", json={"locale": "en", "workspace": str(root), "revision": 0})
    assert stale.status_code == 409
    assert read_json(other / "project.json")["locale"] == "zh-CN"
