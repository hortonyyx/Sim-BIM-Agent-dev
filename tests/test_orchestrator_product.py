import json
from pathlib import Path

from PIL import Image
import pytest

from src.agent.runtime_configuration import argv_for, load_configuration
from src.dsh_runtime.product import host_config, installed_executable, matches_frozen_scope, verify_resume_inputs
from src.dsh_runtime.relay import native_name


def test_workbench_revision_carries_exact_generic_parent_and_new_user_evidence(tmp_path):
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from src.agent.model_revision import admit_revision, child_north_axis, digest, freeze_revision_parent, require_revision_descendant
    from tests.test_bim_agent_tools import _run_with_one_image, _two_room_proposal
    parent = _run_with_one_image(tmp_path)
    toolkit = Toolkit(parent)
    proposal = json.loads(_two_room_proposal())
    first = toolkit.build(proposal)["candidate"]
    toolkit.build(proposal)  # A saved alternative outside the delivered ancestry.
    edit = {"op": "update_opening", "id": "door", "changes": {"z": [0, 2.2]},
            "reason": "Fixture clear height", "source_refs": ["fixture"]}
    final = toolkit.revise(first, json.dumps([edit]))["candidate"]
    assert final == "candidate_03"
    source_path = parent / final / "source_model.json"
    parent_digest = digest(source_path)
    snapshot = tmp_path / "snapshot"
    frozen = freeze_revision_parent(parent, snapshot, candidate=final, source_sha256=parent_digest)
    assert not (snapshot / "candidate_02").exists() and not (snapshot / final / "viewer.html").exists()
    draft = {"instructions": "", "northAngle": 45, "notes": [{"id": "note", "text": "Widen this opening",
             "targets": [{"kind": "surface", "id": "door", "label": "Door", "zone": "left",
                          "surface": "door", "point": [3, 1, 2.2]}]}]}
    revision = {"parentRun": "parent", "parentCandidate": final, "parentSourceSha256": parent_digest,
                "snapshotSha256": frozen, "parentViewerSha256": "f" * 64, "draft": draft}
    run = _run_with_one_image(tmp_path / "child")
    task = admit_revision(run, revision, snapshot)
    assert 'Widen this opening' in task and '"editable_collection": "openings"' in task
    child = Toolkit(run)
    assert list(child.manifest["revisionEvidence"].values()) == ["Widen this opening"]
    assert child_north_axis(child)["value_deg"] == 315
    with pytest.raises(ValueError, match="new child"):
        require_revision_descendant(child, final)
    saved = child.revise(final, "[]")
    assert saved.get("source_geometry_ready"), saved
    name = saved["candidate"]
    assert name == "candidate_04"
    require_revision_descendant(child, name)
    before = json.loads(source_path.read_bytes())
    after = json.loads((run / name / "source_model.json").read_bytes())
    assert after["coordinate_system"]["north_axis"]["value_deg"] == 315
    assert before["source_geometry_sha256"] == after["source_geometry_sha256"]
    assert before["openings"] == after["openings"]
    assert digest(source_path) == parent_digest

    next_snapshot = tmp_path / "next-snapshot"
    next_digest = digest(run / name / "source_model.json")
    next_revision = dict(revision, parentRun="child", parentCandidate=name, parentSourceSha256=next_digest,
                         snapshotSha256=freeze_revision_parent(run, next_snapshot, candidate=name, source_sha256=next_digest),
                         draft={"notes": [], "instructions": "Review the room uses", "northAngle": None})
    grandchild = _run_with_one_image(tmp_path / "grandchild")
    admit_revision(grandchild, next_revision, next_snapshot)
    reopened = Toolkit(grandchild)
    assert child_north_axis(reopened)["value_deg"] == 315
    assert set(reopened.manifest["revisionEvidence"].values()) == {"Widen this opening", "Review the room uses"}
    (next_snapshot / name / "proposal.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        admit_revision(_run_with_one_image(tmp_path / "changed"), next_revision, next_snapshot)


def test_harness_cli_can_be_at_a_different_depth(tmp_path, monkeypatch):
    installation = tmp_path / "DeepSeek Harness"
    cli = installation / "bin/dsh.cmd"
    cli.parent.mkdir(parents=True)
    cli.touch()
    executable = installation / "DeepSeek Harness.exe"
    executable.touch()
    (installation / "resources").mkdir()
    (installation / "resources/app.asar").touch()
    monkeypatch.delenv("SIM_BIM_DSH_EXECUTABLE", raising=False)
    monkeypatch.setattr("src.dsh_runtime.product.shutil.which", lambda name: str(cli))
    monkeypatch.setattr("src.dsh_runtime.product._windows_installation", lambda: None)
    assert installed_executable(None) == executable


def test_edit_finish_requires_visible_change_and_accepts_property_only_retry(tmp_path):
    from copy import deepcopy
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from src.agent.model_revision import UNCHANGED_REVISION, admit_revision, digest, freeze_revision_parent, require_revision_change
    from src.agent.sdk import BimSession
    from tests.test_drawing_source_precision import rotated_plan
    from tests.test_sim_inspection_pipeline import _grade, _run

    parent, _, _ = _run(tmp_path)
    reviews = {"drawing_review": {"coverage": "whole_building", "summary": "Reviewed.", "unresolved": []}}
    rules = [{"kind": kind, "all": True, "spatial": _grade(1, "Unconfirmed.", "待确认。")}
             for kind in ("space", "boundary", "opening")]
    with BimSession(parent) as bim:
        original = bim.call("build_parametric_bim", plan_json=rotated_plan()).require_ok()["candidate"]
        bim.call("finish_bim", candidate=original, reviews_json=reviews, inspection_json={"rules": rules}).require_ok()
    folder = parent / original
    base_hash = digest(folder / "source_model.json")
    snapshot = tmp_path / "snapshot"
    snapshot_hash = freeze_revision_parent(parent, snapshot, candidate=original, source_sha256=base_hash)
    child = tmp_path / "edit"
    child.mkdir()
    run, _, _ = _run(child)
    admit_revision(run, {"parentRun": "parent", "parentCandidate": original, "parentSourceSha256": base_hash,
                         "snapshotSha256": snapshot_hash, "draft": {"northAngle": None, "notes": [],
                         "instructions": "Record the door core material."}}, snapshot)
    with BimSession(run) as bim:
        created = bim.call("revise_bim", candidate=original, operations_json="[]").require_ok()["candidate"]
        source = json.loads((run / created / "source_model.json").read_bytes())
        inspection = json.loads((run / created / "sim_inspection.json").read_bytes())
        changed = deepcopy(source)
        changed["generation"]["unresolved"] = ["Different internal review"]
        changed["openings"][0]["source_refs"] = ["Different locator"]
        points = changed["openings"][0]["vertices"]
        changed["openings"][0]["vertices"] = points[1:] + points[:1]
        relinked = deepcopy(inspection)
        next(iter(relinked["spatial"]["spaces"].values()))["source_refs"] = ["different internal locator"]
        toolkit = Toolkit(run)
        with pytest.raises(ValueError, match="No visible change"):
            require_revision_change(toolkit, changed, relinked)
        for key, value in (("role", "unknown"), ("height", source["spaces"][0]["height"] + .2)):
            changed = deepcopy(source)
            changed["spaces"][0][key] = value
            require_revision_change(toolkit, changed, inspection)
        changed = deepcopy(source)
        changed["coordinate_system"]["north_axis"] = {"value_deg": 42}
        require_revision_change(toolkit, changed, inspection)
        explanation = deepcopy(inspection)
        next(iter(explanation["spatial"]["spaces"].values()))["reason"]["en"] = "Boundary traced on plan."
        require_revision_change(toolkit, source, explanation)
        reviews["drawing_review"]["summary"] = "Rechecked the delivered extent."
        rejected = bim.call("finish_bim", candidate=created, reviews_json=reviews)
        assert rejected.is_error and "No visible change" in str(rejected)
        assert (run / UNCHANGED_REVISION).is_file() and not (run / "delivery_selection.json").exists()
        material = {"en": "Solid oak", "zh": "实心橡木", "source": "from user", "source_refs": ["brief"]}
        bim.call("finish_bim", candidate=created, reviews_json=reviews, inspection_json={
            "rules": [{"kind": "opening", "type": "door", "material": material}]}).require_ok()
        assert "Solid oak" in (run / created / "viewer.html").read_text(encoding="utf-8")
        assert not (run / UNCHANGED_REVISION).exists()
    assert digest(folder / "source_model.json") == base_hash


def project(tmp_path, **extra):
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (8, 8)).save(images / "Ground.png")
    case = dict(case_id="sample", mode="orchestrator", provider="chatgpt-subscription",
                model="gpt-6.1-sol", output_tokens=32000, max_candidates=12, output="sample",
                materials={"images": "images", "image_notes": {"Ground.png": "Ground floor"}},
                worker={"model": "gpt-6.1-terra", "max_concurrent": 2},
                limits={"seconds": 120, "tokens": None, "model_calls": 20, "tool_calls": 100})
    case.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict(schema_version=1, batch_id="sample", maximum_seconds_per_case=120,
                              approval_required_before_launch=True, input_root=str(tmp_path),
                              run_root=str(tmp_path / "runs"), cases=[case])))
    return path


@pytest.mark.parametrize("notes", [{}, {"Ground.png": "Ground floor"}])
def test_dsh_only_admits_exact_legacy_auto_scope_for_omitted_kickoff(tmp_path, notes):
    config = project(tmp_path, materials={"images": "images", "image_notes": notes})
    case = load_configuration(config)["cases"][0]
    legacy = "Deliver a simulation-ready Sim-BIM for the supplied building. Use the detail supported by the evidence."
    suffix = "\nUser-supplied image descriptions:\n" + json.dumps(notes, sort_keys=True) if notes else ""
    assert case["scope"] == suffix and matches_frozen_scope(case, legacy + suffix)
    assert not matches_frozen_scope(case, legacy + suffix + " extra")
    value = json.loads(config.read_bytes())
    value["cases"][0].update(scope="", _scope_was_omitted=True)
    config.write_text(json.dumps(value), encoding="utf-8")
    explicit = load_configuration(config)["cases"][0]
    assert matches_frozen_scope(explicit, suffix) and not matches_frozen_scope(explicit, legacy + suffix)


def test_dsh_is_default_and_worker_allowance_is_inside_whole_case(tmp_path, monkeypatch):
    path = project(tmp_path)
    case = load_configuration(path)["cases"][0]
    assert case["runtime"] == "dsh" and "Ground floor" in case["scope"]
    assert "src.dsh_runtime.product" in argv_for(case, configuration_path=path)
    monkeypatch.setattr("src.dsh_runtime.product.installed_executable", lambda _: Path("dsh"))
    config = host_config(case, tmp_path)
    assert config["worker"]["model"] == "gpt-6.1-terra"
    for key in ("model_calls", "tool_calls"):
        assert config["worker"][key] + config["limits"][key] == case["limits"][key]
    assert native_name("delegate_workers") == "mcp__sim_bim_workers__delegate_workers"


def test_same_materials_and_worker_in_native_comparison(tmp_path):
    path = project(tmp_path, runtime="native")
    case = load_configuration(path)["cases"][0]
    argv = argv_for(case)
    assert "--orchestrator" in argv and "--worker-concurrency" in argv
    assert argv[argv.index("--worker-model") + 1] == "gpt-6.1-terra"
    assert argv[argv.index("--images") + 1] == str(tmp_path / "images")


def test_unbound_material_description_is_rejected(tmp_path):
    path = project(tmp_path, materials={"images": "images", "image_notes": {"missing.png": "standard floor"}})
    with pytest.raises(ValueError, match="outside the admitted"):
        load_configuration(path)


def test_dsh_resume_checks_source_bytes_even_when_original_names_match(tmp_path):
    from src.agent.bim_inputs import digest
    image = tmp_path / "Ground.png"
    Image.new("RGB", (8, 8)).save(image)
    materials = {"images": tmp_path, "mesh": None, "building_input": None}
    manifest = {"images": {image.name: {"sha256": digest(image)}}}
    verify_resume_inputs(manifest, materials)
    Image.new("RGB", (8, 8), color="red").save(image)
    with pytest.raises(ValueError, match="configured images differ"):
        verify_resume_inputs(manifest, materials)


def test_dsh_worker_call_can_use_assigned_case_wall_time():
    from src.dsh_runtime.profile import make_patch
    patch = make_patch("http://127.0.0.1", "gpt-6.1-sol", worker_args=["--model", "gpt-6.1-terra"],
                       worker_wall_seconds=300)
    assert "toolCallTimeoutMs: 300000" in patch


def test_background_worker_configuration_is_dsh_only(tmp_path, monkeypatch):
    worker = {"model": "gpt-6.1-terra", "background": True}
    path = project(tmp_path, worker=worker)
    case = load_configuration(path)["cases"][0]
    monkeypatch.setattr("src.dsh_runtime.product.installed_executable", lambda _: Path("dsh"))
    assert host_config(case, tmp_path)["worker"]["background"]
    raw = json.loads(path.read_bytes())
    raw["cases"][0]["runtime"] = "native"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="worker.background"):
        load_configuration(path)


def test_dsh_mixed_routes_freeze_in_order_and_use_shared_limits(tmp_path, monkeypatch):
    choices = [{"provider": "chatgpt-subscription", "model": "gpt-6.1-terra"},
               {"provider": "deepseek", "model": "deepseek-flash"}]
    path = project(tmp_path, provider="deepseek", model="deepseek-flash", worker=None, workers=choices)
    case = load_configuration(path)["cases"][0]
    monkeypatch.setattr("src.dsh_runtime.product.installed_executable", lambda _: Path("dsh"))
    config = host_config(case, tmp_path)
    assert config["provider"] == "deepseek" and config["output_tokens"] == 32000
    assert config["worker"]["routes"] == choices and config["worker"]["max_concurrent"] == 2
    for key in ("tool_calls", "model_calls"):
        assert config["worker"][key] + config["limits"][key] == case["limits"][key]
    raw = json.loads(path.read_bytes())
    raw["cases"][0]["workers"].append(choices[1])
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="distinct"):
        load_configuration(path)


def test_resume_rejects_changed_input_groups_or_language():
    materials = dict(images=None, mesh=None, building_input=None, blocks=[{"id": "A", "caption": ""}], locale="en")
    manifest = {"images": {}, "blocks": materials["blocks"], "locale": "en"}
    verify_resume_inputs(manifest, materials)
    with pytest.raises(ValueError, match="configured blocks"):
        verify_resume_inputs(manifest, {**materials, "blocks": []})
    with pytest.raises(ValueError, match="configured locale"):
        verify_resume_inputs(manifest, {**materials, "locale": "zh-CN"})


def test_real_mixed_worker_profiles_keep_separate_transports(tmp_path, monkeypatch):
    import asyncio
    from src.agent import observer_mcp
    from src.agent_runtime.adapter import HttpChatAdapter
    from src.agent_runtime.responses import HttpResponsesAdapter
    from src.workbench import connections

    monkeypatch.setattr(connections, "official_deepseek_key", lambda p: "offline-test-key")
    async def validate(connection, model):
        assert (connection.descriptor.route_id, model) in {
            ("chatgpt-subscription", "gpt-6.1-terra"), ("deepseek", "deepseek-flash")}
    monkeypatch.setattr(observer_mcp, "validate_connection_model", validate)
    args = observer_mcp.parser().parse_args(["--run", str(tmp_path), "--store", str(tmp_path / "pool"),
        "--model", "gpt-6.1-terra", "--max-concurrent", "2", "--model-calls", "5", "--tool-calls", "3"])
    async def inspect_transport(provider, model, adapter_type, billing_mode):
        profile = observer_mcp.worker_profile(args, provider, model)
        assert profile["route"]["billing_mode"] == billing_mode
        adapter = await profile["adapter_factory"]("test", None)
        try:
            assert isinstance(adapter, adapter_type)
            if provider == "deepseek":
                assert adapter.endpoint == "https://api.deepseek.com/chat/completions"
            assert "offline-test-key" not in json.dumps(profile["route"])
        finally:
            await adapter.close()
    asyncio.run(inspect_transport("chatgpt-subscription", "gpt-6.1-terra", HttpResponsesAdapter, "subscription"))
    asyncio.run(inspect_transport("deepseek", "deepseek-flash", HttpChatAdapter, "metered"))
