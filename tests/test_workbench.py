import io
import hashlib
import json
from pathlib import Path

from PIL import Image
from starlette.testclient import TestClient
import pytest
import uuid

from src.agent.runtime_configuration import load_configuration
from src.agent.runtime_entry import prepare_inputs
from src.workbench import runs
from src.workbench import custom_connections as custom
from src.workbench.server import Workbench, create_app
from src.workbench.storage import read_json, save_json


ORIGIN = "http://127.0.0.1"
COORD = {"provider": "chatgpt-subscription", "model": "gpt-6.2-sol"}
WORKER = {"provider": "deepseek", "model": "deepseek-flash"}


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(tmp_path / "settings"))
    executable = tmp_path / "DeepSeek Harness.exe"
    executable.touch()
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources/app.asar").touch()
    monkeypatch.setenv("SIM_BIM_DSH_EXECUTABLE", str(executable))
    backend = Workbench()
    monkeypatch.setattr(backend.connections, "view", lambda locale: [
        {"provider": s["provider"], "connected": True, "models": [{"id": s["model"], "name": s["model"]}]}
        for s in [COORD, WORKER]] + custom.custom_catalog(locale, ()))
    client = TestClient(create_app(backend, warm=False), base_url=ORIGIN, headers={"Origin": ORIGIN})
    root = tmp_path / "workspace"
    assert client.post("/api/workspace", json={"path": str(root)}).json()["workspace"] == str(root)
    return client, root


def test_installed_workbench_remembers_its_own_workspace(tmp_path, monkeypatch):
    from src.workbench import server
    program = tmp_path / "product"
    program.mkdir()
    settings = tmp_path / "settings"
    monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(settings))
    monkeypatch.setattr(server, "ROOT", program)
    monkeypatch.setattr(server.Connections, "view", lambda self, locale: [])
    developer = {"locale": "en", "workspace": str(tmp_path / "dev-workspace")}
    save_json(settings / "preferences.json", developer)
    (program / "release-manifest.json").touch()
    app = Workbench()
    assert app.prefs == {"locale": "zh-CN", "workspace": None}
    app.select({"path": str(tmp_path / "first-project")})
    assert Workbench().prefs == {"locale": "zh-CN", "workspace": str(tmp_path / "first-project")}
    assert read_json(settings / "preferences.json") == developer


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (10, 12), color).save(out, format="PNG")
    return out.getvalue()


def test_second_launch_reopens_only_the_same_installation(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from src.workbench import server

    monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(tmp_path))
    identity = server.installation_id()
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)

    class Instance(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/instance"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"product": "Sim-BIM Agent", "installation": identity}).encode())

    with HTTPServer(("127.0.0.1", 0), Instance) as peer:
        port = peer.server_port
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        try:
            assert server.serve(port) == 0
            assert opened == [f"http://127.0.0.1:{port}/"]
            monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(tmp_path / "other"))
            with pytest.raises(ValueError, match="Cannot open local port"):
                server.serve(port)
            assert len(opened) == 1
        finally:
            peer.shutdown()
            thread.join()


def test_chinese_run_keeps_english_object_identifiers_and_original_filenames(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "首层.png").write_bytes(png("white"))
    _, guide, task = prepare_inputs(tmp_path / "run", images=images, mesh=None, building_input=None,
        scope="首层带商业", image_kind="drawings", max_candidates=1, orchestrator=True, locale="zh-CN")
    assert "stable English ASCII identifiers" in guide
    assert "modelling notes in Simplified Chinese" in guide
    assert "首层带商业" in task and "首层.png" in task


def test_kickoff_migrates_only_unchanged_legacy_text_and_freezes_empty(workbench, tmp_path):
    from src.workbench.storage import LEGACY_DEFAULT_BRIEFS, read_project
    client, root = workbench
    draft = client.get("/api/state").json()["project"]
    assert draft["taskBrief"] == ""
    path = root / "project.json"
    for auto in LEGACY_DEFAULT_BRIEFS:
        for raw, expected in [(auto, ""), (auto + " Two podium floors.", auto + " Two podium floors."),
                              (auto + "\r\n\r\nTwo podium floors.\nA roof terrace.", "Two podium floors.\nA roof terrace.")]:
            path.write_text(json.dumps({**draft, "schema": "sim-bim-workbench.v1", "taskBrief": raw}), encoding="utf-8")
            assert client.get("/api/state").json()["project"]["taskBrief"] == expected
    auto = next(iter(sorted(LEGACY_DEFAULT_BRIEFS)))
    old_client = dict(workspace=str(root), revision=draft["revision"], blocks=[], taskBrief=auto,
                      coordinator=COORD, workers=[])
    assert client.post("/api/project", json=old_client).json()["project"]["taskBrief"] == ""
    # Once typed deliberately in the new form, even the old boilerplate is user input.
    saved = client.post("/api/project", json={**old_client, "revision": 1, "schema": draft["schema"]}).json()["project"]
    assert read_project(root)["taskBrief"] == saved["taskBrief"] == auto
    block = client.post("/api/blocks", data={"workspace": str(root), "revision": 2, "caption": ""},
                        files=[("files", ("plan.png", png("white"), "image/png"))]).json()["project"]["blocks"][0]
    client.post("/api/project", json={**old_client, "schema": draft["schema"], "revision": 3, "taskBrief": "",
        "blocks": [{"id": block["id"], "assetIds": [a["id"] for a in block["assets"]]}]}).raise_for_status()
    work = tmp_path / "run"
    project = read_project(root)
    materials, blocks = runs.freeze(root, work, project)
    config = runs.configuration(work, project, materials, blocks, executable=tmp_path / "DeepSeek Harness.exe")
    config_path = work / "case.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert load_configuration(config_path)["cases"][0]["scope"] == read_json(work / "input.json")["taskBrief"] == ""


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
    with monkeypatch.context() as missing:
        missing.setenv("SIM_BIM_DSH_EXECUTABLE", str(tmp_path / "missing.exe"))
        denied = client.post("/api/runs", json={"workspace": str(root), "revision": revision})
        assert denied.status_code == 503 and "DeepSeek Harness" in denied.json()["error"]
        assert not (root / "runs").exists()
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
    assert case["dsh_executable"] == str(tmp_path / "DeepSeek Harness.exe")
    assert case["provider"] == COORD["provider"] and case["workers"] == [WORKER]
    frozen = read_json(directory / "input.json")
    expected = {"glazing": [16.8, 24], "horizontal": [160.5, 200]}
    assert state["project"]["thicknessCandidatesMm"] == expected
    assert frozen["thicknessCandidatesMm"] == case["thickness_candidates_mm"] == expected
    assets = frozen["blocks"][0]["assets"]
    assert frozen["blocks"][0]["number"] == 1
    assert [a["number"] for a in assets] == [1, 2, 3]
    assert [a["name"] for a in assets] == ["Plan.png"] * 3
    assert [(directory / a["path"]).read_bytes() for a in assets] == images[::-1]
    assert len({a["file"].casefold() for a in assets}) == 3
    run, guide, task = prepare_inputs(tmp_path / "production", images=directory / "inputs/images",
        mesh=None, building_input=None, scope=case["scope"], image_kind=case["image_kind"], max_candidates=4,
        orchestrator=True, blocks=case["blocks"], locale=case["locale"],
        thickness_candidates_mm=case["thickness_candidates_mm"])
    assert "first, typical, roof" in task and "Keep the entrance." in task and "English" in guide
    numbered = json.loads(task.split("User material blocks in submitted order:\n", 1)[1].splitlines()[0])
    assert numbered[0]["number"] == 1
    assert [(a["number"], a["tool_filename"]) for a in numbered[0]["files"]] == [
        (i, a["file"]) for i, a in enumerate(assets, 1)]
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


@pytest.mark.parametrize("linked", [False, True], ids=["repository", "linked-worktree"])
def test_workspace_rejects_git_checkout_before_writing(workbench, tmp_path, linked):
    client, current = workbench
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = checkout / ".git"
    if linked:
        marker.write_text("gitdir: ../parent/.git/worktrees/checkout", encoding="utf-8")
    else:
        marker.mkdir()
    for target in (checkout, checkout / "new-project"):
        response = client.post("/api/workspace", json={"path": str(target)})
        assert response.status_code == 400 and "Git" in response.json()["error"]
        assert list(checkout.iterdir()) == [marker]
    assert client.get("/api/state").json()["workspace"] == str(current)


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
    assert opened.headers["cache-control"] == "no-store"
    download = client.get(base + "Sim-BIM.html?download=1")
    assert download.content == model
    assert download.headers["content-disposition"] == 'attachment; filename="Sim-BIM.html"'
    assert "allow-forms" in opened.headers["content-security-policy"]
    assert "form-action 'none'" in opened.headers["content-security-policy"]
    for path in ("/static/viewer.html", "/static/viewer-draft.js", "/static/viewer-host.js"):
        assert client.get(path).headers["cache-control"] == "no-store"

    head = client.head(base + "Sim-BIM.html")
    assert head.content == b"" and head.headers["etag"] == opened.headers["etag"]
    revised = model + b"<p>O14</p>"
    (directory / "Sim-BIM.html").write_bytes(revised)
    changed = client.head(base + "Sim-BIM.html")
    assert changed.headers["etag"] != head.headers["etag"]
    for query in ({"rev": head.headers["etag"]}, {"download": "1"}):
        assert client.get(base + "Sim-BIM.html", params=query).content == revised


def completed_parent(parent, first):
    candidate = parent / "production/private/dsh/work/bim/candidate_03"
    model_id = "a" * 64
    save_json(candidate / "source_model.json", {"source_model_sha256": model_id, "spaces": [], "boundaries": [],
        "floors": [{"id": "F1"}], "openings": [{"id": "W1"}], "coordinate_system": {"north_axis": {"value_deg": 315}}})
    save_json(candidate / "sim_inspection.json", {})
    viewer = parent / "Sim-BIM.html"
    viewer.write_bytes(b"<html>parent</html>")
    hashes = {"candidate_03/" + name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in
              (("viewer.html", viewer), ("source_model.json", candidate / "source_model.json"),
               ("sim_inspection.json", candidate / "sim_inspection.json"))}
    save_json(parent / "production/control/terminal.json", {"delivered": True, "proof": {"origin": "domain_tool",
              "tool_name": "finish_bim", "candidate": "candidate_03", "artifact_sha256": hashes}})
    save_json(parent / "status.json", {**first, "status": "completed"})
    return model_id, viewer


def test_revision_admission_reuses_frozen_parent_and_is_idempotent(workbench, monkeypatch):
    from src.agent import model_revision

    client, root = workbench
    launched = []
    monkeypatch.setattr(runs.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    uploaded = client.post("/api/blocks", data={"workspace": str(root), "revision": 0, "caption": "floor"},
                           files=[("files", ("plan.png", png("white"), "image/png"))]).json()["project"]
    block = uploaded["blocks"][0]
    client.post("/api/project", json={"workspace": str(root), "revision": 1, "taskBrief": "Keep the floor.",
        "coordinator": COORD, "workers": [], "blocks": [{"id": block["id"], "assetIds": [block["assets"][0]["id"]]}]}).raise_for_status()
    first = client.post("/api/runs", json={"workspace": str(root), "revision": 2}).json()["run"]
    parent = runs.run_dir(root, first["id"])
    model_id, viewer = completed_parent(parent, first)
    monkeypatch.setattr(model_revision, "freeze_revision_parent", lambda *args, **kwargs: "e" * 64)
    target = {"kind": "surface", "id": "W1", "label": "W1", "zone": "", "surface": "W1", "point": [1, 2, 3]}
    note = {"id": "one", "text": "Raise the sill of this window.", "targets": [target]}
    draft = {"notes": [note], "instructions": "", "northAngle": None}
    body = {"workspaceTag": runs.workspace_tag(root), "parentRunId": parent.name, "parentModelId": model_id,
            "draft": draft, "requestId": str(uuid.uuid4())}
    def submit(**changes):
        return client.post("/api/revisions", json={**body, **changes})
    assert submit(workspaceTag="other").status_code == 409
    assert submit(parentModelId="0" * 64).status_code == 409
    unchanged = submit(draft={"notes": [], "instructions": "", "northAngle": 45})
    assert unchanged.status_code == 400 and "这个北向" in unchanged.json()["error"]
    assert len(launched) == 1 and len(list((root / "runs").glob("run-*"))) == 1
    assert submit(draft={**draft, "notes": [{**note, "targets": [{**target, "id": "stale"}]}]}).status_code == 409
    # Edits use the original material even if it changed in the working project's material folder.
    next((root / "materials").rglob("plan.png")).write_bytes(png("blue"))
    child = submit().json()["run"]
    assert child["parentRunId"] == parent.name and len(launched) == 2
    assert submit().json()["run"] == child and len(launched) == 2
    assert submit(draft={**draft, "northAngle": 0}).status_code == 409
    directory = runs.run_dir(root, child["id"])
    case = read_json(directory / "case.json")["cases"][0]
    assert case["revision"]["draft"] == draft and case["revision"]["parentCandidate"] == "candidate_03"
    assert (directory / "inputs/images/plan.png").read_bytes() == png("white")
    assert viewer.read_bytes() == b"<html>parent</html>"
    assert runs.latest(root, builds_only=True)["id"] == parent.name
    assert client.get("/api/runs").json()["runs"][0]["parentRunId"] == parent.name
    viewer.write_bytes(b"changed")
    assert submit(requestId=str(uuid.uuid4())).status_code == 409
    assert len(launched) == 2


def test_revision_freezes_current_connections_for_original_models(workbench, monkeypatch):
    from src.agent import model_revision

    client, root = workbench
    monkeypatch.setattr(runs, "launch_prepared", lambda directory, locale, started: started)
    monkeypatch.setattr(model_revision, "freeze_revision_parent", lambda *args, **kwargs: "e" * 64)
    coordinator, first_coord = custom.save_custom({"label": "Coordinator", "baseUrl": "http://localhost:8000/v1",
                                                   "modelIds": ["vision"]})
    worker, first_worker = custom.save_custom({"label": "Worker", "baseUrl": "http://localhost:8001/v1",
                                              "modelIds": ["reader"]})
    routes = {"coordinator": {"provider": coordinator, "model": "vision"},
              "workers": [{"provider": worker, "model": "reader"}]}
    block = client.post("/api/blocks", data={"workspace": str(root), "revision": 0},
                        files={"files": ("plan.png", png("white"))}).json()["project"]["blocks"][0]
    client.post("/api/project", json={"workspace": str(root), "revision": 1, "taskBrief": "Build.", **routes,
        "blocks": [{"id": block["id"], "assetIds": [block["assets"][0]["id"]]}]}).raise_for_status()
    first = client.post("/api/runs", json={"workspace": str(root), "revision": 2,
        "connectionRevisions": {coordinator: first_coord, worker: first_worker}}).json()["run"]
    parent = runs.run_dir(root, first["id"])
    model_id, _ = completed_parent(parent, first)
    parent_config = (parent / "case.json").read_bytes()
    original_edit = {"workspaceTag": runs.workspace_tag(root), "parentRunId": parent.name, "parentModelId": model_id,
                     "draft": {"notes": [], "instructions": "Raise the window sill.", "northAngle": None},
                     "requestId": str(uuid.uuid4())}

    _, next_coord = custom.save_custom({"baseUrl": "http://localhost:8002/v1", "contextWindow": 32768,
        "outputTokens": 2048, "apiKey": "new-local-test-key"}, provider=coordinator)
    _, next_worker = custom.save_custom({"outputTokens": 512}, provider=worker)
    response = client.post("/api/revisions", json=original_edit)
    response.raise_for_status()
    child_id = response.json()["run"]["id"]
    child = runs.run_dir(root, child_id)
    frozen_child = (child / "case.json").read_bytes()
    case = json.loads(frozen_child)["cases"][0]
    assert {"provider": case["provider"], "model": case["model"]} == routes["coordinator"]
    assert case["workers"] == routes["workers"]
    assert case["connection_revisions"] == {coordinator: next_coord, worker: next_worker}
    assert (case["context_tokens"], case["output_tokens"], case["worker"]["output_tokens"]) == (32768, 2048, 512)

    _, latest_coord = custom.save_custom({"contextWindow": 65536, "outputTokens": 4096}, provider=coordinator)
    assert client.post("/api/revisions", json=original_edit).json()["run"]["id"] == child_id
    another = client.post("/api/revisions", json={**original_edit, "requestId": str(uuid.uuid4())}).json()["run"]
    another_case = read_json(runs.run_dir(root, another["id"]) / "case.json")["cases"][0]
    assert another_case["connection_revisions"] == {coordinator: latest_coord, worker: next_worker}
    assert another_case["context_tokens"] == 65536 and another_case["output_tokens"] == 4096
    custom.save_custom({"modelIds": ["other-reader"]}, provider=worker)
    unavailable = client.post("/api/revisions", json={**original_edit, "requestId": str(uuid.uuid4())})
    assert unavailable.status_code == 400
    assert len(list((root / "runs").glob("run-*"))) == 3
    assert (parent / "case.json").read_bytes() == parent_config and (child / "case.json").read_bytes() == frozen_child
    assert custom.custom_connection(coordinator, revision=first_coord)["base_url"] == "http://localhost:8000/v1"
    frozen = custom.custom_connection(coordinator, revision=next_coord)
    assert (frozen["base_url"], frozen["api_key"], frozen["context_window"]) == (
        "http://localhost:8002/v1", "new-local-test-key", 32768)
    assert custom.custom_connection(worker, revision=next_worker)["output_tokens"] == 512


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


def test_live_trace_projects_native_events_and_resumes_partial_rows(workbench):
    client, root = workbench
    identity = "run-20261125-130000-abcd0000"
    directory = root / "runs" / identity
    session = directory / "production/private/dsh/session"
    session.mkdir(parents=True)
    (directory / "input.json").write_text(json.dumps({"locale": "en", "coordinator": COORD}))
    (directory / "status.json").write_text(json.dumps({"status": "running", "id": identity}))
    path = "/api/run/trace"
    params = {"id": identity}
    line = lambda row: json.dumps(row).encode() + b"\n"
    log = session / "events.jsonl"
    call = {"type": "tool_call", "tool": "mcp__sim_bim_workers__delegate_workers", "callId": "call-1",
            "input": {"tasks": [{"task_id": "read-plan", "question": "PRIVATE prompt"}]}}
    result = line({"type": "tool_result", "callId": "call-1", "status": "completed",
                   "result": json.dumps({"status": "error", "error": "PRIVATE diagnostic"})})
    log.write_bytes(line({"type": "session", "cwd": "PRIVATE path"})
                    + line({"type": "status", "phase": "step_start", "turn": 1, "step": 2})
                    + line({"type": "thinking", "text": "PRIVATE reasoning"})
                    + line(call) + result[:-4])
    first = client.get(path, params=params).json()
    assert first["run"]["status"] == "running" and first["meta"]["model"] == COORD["model"]
    assert [e["kind"] for e in first["events"]] == ["session", "step", "tool"]
    called = first["events"][-1]
    assert called["workers"] == ["read-plan"] and called["step"] == 2
    params["cursor"] = first["cursor"]
    idle = client.get(path, params=params).json()
    assert idle["cursor"] == first["cursor"] and not idle["events"] and not idle["more"]
    with log.open("ab") as file:
        file.write(result[-4:] + line({"type": "text", "text": "Public. <think>PRIVATE</think> sk-abcdefghijklmnop"}))
    retry = session / "attempt-002"
    retry.mkdir()
    (retry / "events.jsonl").write_bytes(line({"type": "status", "phase": "step_start", "turn": 1, "step": 1}))
    worker = directory / "production/private/workers"
    worker.mkdir()
    (worker / "events.jsonl").write_bytes(line({"event_id": "w1", "task_id": "read-plan",
        "parent_task": {"kind": "known", "task_id": "observer_pool"},
        "occurred_at": {"kind": "known", "value": "2026-11-25T00:00:00Z"},
        "payload": {"event_type": "adapter_request", "final_request_body": "PRIVATE",
                    "versions": {"remote_model": {"remote_alias": "flash-real"}}}}))
    control = directory / "production/control"
    control.mkdir()
    (control / "relay.jsonl").write_bytes(line({"event": "automatic_request_recovery", "attempt": 1,
                                                "prior_turn": 3, "backoff_seconds": 5, "failure": "PRIVATE"}))
    second = client.get(path, params=params).json()
    assert second["run"]["status"] == "running"
    returned, message, resumed, child, recovery = second["events"]
    assert returned["id"] == called["id"] and returned["status"] == "failed"
    assert message["text"].startswith("Public.") and "sk-" not in message["text"]
    assert resumed["attempt"] == 2 and resumed["id"] != first["events"][1]["id"]
    assert child["model"] == "flash-real" and child["task"] == "read-plan" and child["time"] == 1795564800
    assert recovery["result"] == {"prior_turn": 3, "backoff_seconds": 5}
    assert "PRIVATE" not in json.dumps([first, second])
    assert not client.get(path, params={"id": identity, "cursor": second["cursor"]}).json()["events"]
    assert client.get(path, params={"id": identity, "cursor": "garbage"}).status_code == 400
    assert client.get(path, params={"id": "../runs"}).status_code == 404
    assert client.get("/api/runs").json()["runs"] == [
        {"id": identity, "status": "running", "started_at": first["meta"]["started_at"]}]


def test_live_trace_paginates_host_rows_without_duplicate_text(workbench, monkeypatch):
    from src.workbench import trace
    client, root = workbench
    identity = "run-20261125-130001-abcd0001"
    directory = root / "runs" / identity
    session = directory / "production/private/dsh/session"
    session.mkdir(parents=True)
    (directory / "input.json").write_text(json.dumps({"locale": "en", "coordinator": COORD}))
    (directory / "status.json").write_text(json.dumps({"status": "completed", "id": identity}))
    (session / "events.jsonl").write_text("".join(json.dumps({"type": "text", "text": str(i)}) + "\n" for i in range(5)))
    monkeypatch.setattr(trace, "_BATCH", 2)
    all_rows, cursor = [], None
    for more in (True, True, False):
        params = {"id": identity, **({"cursor": cursor} if cursor else {})}
        page = client.get("/api/run/trace", params=params).json()
        assert page["more"] is more
        all_rows.extend(page["events"])
        cursor = page["cursor"]
    assert [e["text"] for e in all_rows] == list("01234")
    assert len({e["id"] for e in all_rows}) == 5


def test_live_trace_settles_only_native_receipts_across_recovery(tmp_path, monkeypatch):
    from src.workbench import trace
    session = tmp_path / "production/private/dsh/session"
    session.mkdir(parents=True)
    rows = [
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {}},
        {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "aborted"}},
        {"type": "status", "phase": "turn_start", "turn": 2},
        {"type": "status", "phase": "turn_end", "turn": 2, "reason": {"error": {"code": "TIMEOUT"}}},
        {"type": "status", "phase": "turn_end", "turn": 3, "reason": {}},
    ]
    (session / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    worker = tmp_path / "production/private/workers"
    worker.mkdir()
    rows = [
        {"event_type": "adapter_request"},
        {"event_type": "run_lifecycle", "action": "failure", "reason": "timeout",
         "model_failure": {"request_event_id": "e0"}},
        {"event_type": "run_lifecycle", "action": "retry", "reason": "retrying"},
        {"event_type": "adapter_request"},
        {"event_type": "model_response", "request_event_id": "e3", "visible_text": ["Recovered."]},
        {"event_type": "tool_execution", "call_id": "t1", "outcome": "unknown"},
        {"event_type": "run_lifecycle", "action": "stop", "reason": "completed"},
    ]
    with (worker / "events.jsonl").open("w") as stream:
        for index, payload in enumerate(rows):
            stream.write(json.dumps({"event_id": f"e{index}", "task_id": "reader",
                                     "parent_task": {"task_id": "pool"}, "payload": payload}) + "\n")
    monkeypatch.setattr(trace, "_BATCH", 1)
    records, cursor, more = {}, None, True
    while more:
        events, cursor, more = trace._page(tmp_path, cursor)
        for event in events:
            records.setdefault(event["id"], {}).update(event)
    assert records["h1:1:1"]["status"] == "failed" and "h1:2:1" not in records
    assert [r["status"] for r in records.values() if r["kind"] == "session"] == ["failed", "failed", "recorded"]
    assert records["w:reader:step:e0"]["status"] == "failed"
    assert records["w:reader:step:e3"]["text"] == "Recovered."
    assert records["w:reader:tool:t1"]["status"] == "recorded"
    assert (records["w:reader"]["status"], records["w:reader"]["text"]) == ("completed", "")
