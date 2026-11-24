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
    config = runs.configuration(work, project, materials, blocks)
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
