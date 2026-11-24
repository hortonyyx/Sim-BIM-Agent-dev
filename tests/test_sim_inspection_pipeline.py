"""Saved object assessments survive unchanged geometry and bind the final offline viewer."""
import json
from pathlib import Path

from PIL import Image
import pytest

from scripts.tool_scripts.bim_agent_guidance import REFERENCES, SPATIAL_GRADE_SCALE
from scripts.tool_scripts.bim_agent_inspection import apply_rules, inherited_records, _empty
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.runtime_delivery import attest_finished_handoff
from src.agent.runtime_entry import prepare_inputs
from src.agent.sdk import BimSession
from tests.test_drawing_source_precision import rotated_plan


def _run(tmp_path, *, orchestrator=True):
    images = tmp_path / "originals"
    images.mkdir()
    Image.new("RGB", (120, 120), "white").save(images / "plan.png")
    return prepare_inputs(tmp_path / "execution", images=images, mesh=None, building_input=None,
        scope="The door leaf has a solid oak core.", image_kind="drawings", floor_plan_images=None,
        max_candidates=5, orchestrator=orchestrator, thickness_candidates_mm={"wall": [180]})


def _grade(value, en, zh, refs=()):
    return {"grade": value, "reason": {"en": en, "zh": zh}, "source_refs": list(refs)}


@pytest.mark.parametrize("orchestrator", [False, True])
def test_system_and_reference_share_spatial_scale_separately_from_kickoff(tmp_path, orchestrator):
    from scripts.tool_scripts.run_bim_agent import run_guide
    from src.agent.runtime_roles.guidance import get_role_guide
    run, guide, task = _run(tmp_path, orchestrator=orchestrator)
    assert guide.count(SPATIAL_GRADE_SCALE) == run_guide(run).count(SPATIAL_GRADE_SCALE) == 1
    assert SPATIAL_GRADE_SCALE in get_role_guide("coordinator")
    assert SPATIAL_GRADE_SCALE not in get_role_guide("plan_reader")
    assert SPATIAL_GRADE_SCALE not in task and "solid oak core" in task
    assert "solid oak core" not in guide
    assert SPATIAL_GRADE_SCALE in REFERENCES["sim_inspection"]


def test_partial_rules_revision_and_attested_single_html(tmp_path, monkeypatch):
    run, guide, task = _run(tmp_path)
    assert "sim_inspection" in guide and "180" in task
    with BimSession(run) as bim:
        assert "inspection_json" in bim.describe("finish_bim")["inputSchema"]["properties"]
        candidate = bim.call("build_parametric_bim", plan_json=rotated_plan()).require_ok()["candidate"]
        directory = run / candidate
        source_bytes = (directory / "source_model.json").read_bytes()
        source = json.loads(source_bytes)
        sidecar = directory / "sim_inspection.json"
        assert all(not rows for rows in json.loads(sidecar.read_bytes())["spatial"].values())
        view_before = (directory / "viewer.html").read_bytes()
        reviews = {"drawing_review": {"coverage": "whole_building", "summary": "Synthetic fixture.", "unresolved": []}}
        assert bim.call("finish_bim", candidate=candidate, reviews_json=reviews).is_error
        assert not (run / "delivery_selection.json").exists()

        space = {"kind": "space", "all": True, "spatial": _grade(4, "Room perimeter traced on plan.",
            "平面图可见空间周界。", ["image:plan.png#fixture-locator"])}
        record = bim.call("record_sim_inspection", candidate=candidate, inspection_json={"rules": [space]}).require_ok()
        assert record["coverage"]["spaces"]["assessed"] == len(source["spaces"])
        boundary = {"kind": "boundary", "all": True, "spatial": _grade(1, "Partition height assumed.", "分隔高度为推断。")}
        opening = {"kind": "opening", "all": True, "spatial": _grade(2, "Door presence stated in brief.",
            "用户提及门，位置为估算。", ["brief"])}
        oak = {"kind": "opening", "type": "door", "material": {
            "en": "The door leaf has a solid oak core.", "zh": "门扇芯材为实心橡木。",
            "source": "from user", "source_refs": ["brief"]}}
        packet = {"rules": [boundary, opening, oak]}
        published = bim.finish_to(tmp_path / "delivery", candidate, reviews=reviews, inspection=packet)
        public = Path(published["viewer"])
        assert [p.name for p in public.parent.iterdir()] == ["Sim-BIM.html"]
        assert (directory / "source_model.json").read_bytes() == source_bytes
        assert public.read_bytes() == (directory / "viewer.html").read_bytes() != view_before
        assert b"Room perimeter traced on plan." in public.read_bytes()
        assert b"fixture-locator" not in public.read_bytes()
        assessment = json.loads(sidecar.read_bytes())
        assert assessment["source_model_sha256"] == source["source_model_sha256"]
        assert assessment["properties"]["material_scope"] == "principal_construction"
        assert json.loads((directory / "sim_inspection_draft.json").read_bytes())["material_scope"] == "principal_construction"
        assert oak["material"]["en"].encode() in public.read_bytes()
        for group in ("spaces", "boundaries", "openings"):
            assert set(assessment["spatial"][group]) == {r["id"] for r in source[group]}
        for door in source["openings"]:
            assert assessment["properties"]["openings"][door["id"]]["material"] == oak["material"]
        finish = bim.call("finish_bim", candidate=candidate, reviews_json=reviews).require_ok()
        assert attest_finished_handoff(run, finish, candidate)
        previous = {name: (run / name).read_bytes() for name in finish["delivery_artifact_sha256"]}
        draft = (directory / "sim_inspection_draft.json").read_bytes()
        changed = {"rules": [{**space, "spatial": _grade(1, "Unverified plan.", "平面未经核实。")}]}
        def assert_original_handoff():
            assert attest_finished_handoff(run, finish, candidate)
            assert all((run / name).read_bytes() == raw for name, raw in previous.items())
            assert (directory / "sim_inspection_draft.json").read_bytes() == draft
        assert bim.call("finish_bim", candidate=candidate, reviews_json=reviews,
                        inspection_json=changed, inferences_json="{}").is_error
        assert_original_handoff()
        from scripts.tool_scripts import bim_agent_replies
        original_reply = bim_agent_replies.compact_reply
        def fail_receipt(run, name, result, **kw):
            if "delivery_artifact_sha256" in result:
                raise RuntimeError("synthetic receipt write failure after signed files changed")
            return original_reply(run, name, result, **kw)
        with monkeypatch.context() as patch:
            patch.setattr(bim_agent_replies, "compact_reply", fail_receipt)
            assert bim.call("finish_bim", candidate=candidate, reviews_json=reviews, inspection_json=changed).is_error
        assert_original_handoff()
        toolkit = Toolkit(run)
        inherited = inherited_records(toolkit, source, candidate)
        assert inherited == assessment["declarations"]
        moved = json.loads(source_bytes)
        door = moved["openings"][0]
        door["vertices"][0][2] += 0.1
        retained = inherited_records(toolkit, moved, candidate)
        assert door["id"] not in retained["openings"]
        assert door["host_boundary_id"] not in retained["boundaries"]
        assert retained["spaces"] == inherited["spaces"]
        revised = bim.call("revise_bim", candidate=candidate, operations_json=[{
            "op": "set_notes", "assumptions": ["Recording fixture scope."], "unresolved": []}]).require_ok()
        inherited_save = json.loads((run / revised["candidate"] / "sim_inspection.json").read_bytes())
        assert inherited_save["source_model_sha256"] != assessment["source_model_sha256"]
        assert inherited_save["declarations"] == assessment["declarations"]
        sidecar.write_bytes(sidecar.read_bytes() + b" ")
        assert attest_finished_handoff(run, finish, candidate) is None


def test_evidence_and_selectors_reject_wrong_origin_unknown_ids_and_missing_refs(tmp_path):
    run, _, _ = _run(tmp_path)
    source = {"floors": [{"id": "L"}], "spaces": [{"id": "s", "floor_id": "L", "role": "office"}],
              "boundaries": [{"id": "w", "space_id": "s", "geometry_type": "wall"}], "openings": []}
    toolkit, rows = Toolkit(run), _empty()
    rule = {"kind": "boundary", "space_ids": ["s"], "floor_ids": ["L"], "type": "wall",
            "spatial": _grade(5, "Drawn wall.", "图纸标注墙体。", ["image:plan.png"])}
    assert apply_rules(toolkit, source, rows, {"rules": [rule]})["boundaries"]["w"]["spatial"]["grade"] == 5
    for update in [{"ids": ["missing"]}, {"type": "floor"},
                   {"spatial": _grade(2, "Partial.", "部分可见。")},
                   {"spatial": _grade(3, "Partial.", "部分可见。", ["image:no.png"])},
                   {"material": {"en": "Brick", "zh": "砖", "source": "from material", "source_refs": ["brief"]}},
                   {"thickness": {"value_mm": 250, "source": "from user", "source_refs": ["image:plan.png"]}}]:
        with pytest.raises(ValueError):
            apply_rules(toolkit, source, rows, {"rules": [rule, {**rule, **update}]})
        assert rows == _empty()


@pytest.mark.parametrize("filename", ["sim_inspection.json", "sim_inspection_draft.json"])
def test_legacy_side_materials_remain_auditable_across_revisions_without_becoming_primary(tmp_path, filename):
    from scripts.tool_scripts.bim_agent_inspection import snapshot
    from src.agent.execution.source_proposal import export_source_proposal
    from tests.test_source_proposal import _proposal

    run, _, _ = _run(tmp_path)
    candidate = "candidate_01"
    folder = run / candidate
    export_source_proposal(_proposal(), folder)
    source = json.loads((folder / "source_model.json").read_bytes())
    old = _empty()
    opening = next(o for o in source["openings"] if o["kind"] == "door")
    host, door = opening["host_boundary_id"], opening["id"]
    finish = {"en": "Decorative oak veneer", "zh": "装饰橡木贴面", "source": "from user", "source_refs": ["brief"]}
    sided = {host: finish}
    grade = _grade(1, "Assumed height.", "高度为推断。")
    thickness = {"value_mm": 45, "source": "inference", "source_refs": []}
    old["boundaries"][host] = {"material": finish, "spatial": grade}
    old["openings"][door] = {"material": finish, "materials_by_host": sided, "thickness": thickness}
    archive = {"schema_version": "sim_inspection_v1", "source_model_sha256": source["source_model_sha256"],
               "declarations": old}
    if filename == "sim_inspection.json":
        archive["properties"] = {}
    path = folder / filename
    path.write_text(json.dumps(archive), encoding="utf-8")
    toolkit = Toolkit(run)
    revised = inherited_records(toolkit, source, candidate)
    assert revised["openings"][door] == {
        "thickness": thickness, "legacy_surface_material": {"material": finish, "materials_by_host": sided}}
    assert revised["boundaries"][host] == {"spatial": grade, "legacy_surface_material": {"material": finish}}
    new_doc = snapshot(toolkit, source, revised, check_evidence=True)
    assert new_doc["spatial"]["boundaries"][host] == grade
    assert new_doc["properties"]["openings"][door]["material"]["source"] == "inference"
    assert finish["en"] not in json.dumps(new_doc["properties"])
    assert json.loads(path.read_bytes()) == archive


def test_dsh_initial_messages_and_resume_bind_candidates(tmp_path, monkeypatch):
    from src.agent_runtime import agent_registry
    from src.dsh_runtime import launcher, relay
    from src.dsh_runtime.product import verify_resume_inputs

    case = tmp_path / "case"
    shared, images = case / "shared", tmp_path / "originals"
    shared.mkdir(parents=True)
    images.mkdir()
    Image.new("RGB", (20, 20), "white").save(images / "plan.png")
    (shared / "task.txt").write_text("", encoding="utf-8")
    (case / "config.json").write_text(json.dumps({"limits": {"candidates": 5, "wall_seconds": 300}}))
    monkeypatch.setattr(agent_registry, "release_records", lambda _, **kw: {"domain": {}, "runtime": {}})
    monkeypatch.setattr(agent_registry, "source_commit", lambda _: "fixture")
    monkeypatch.setattr(launcher, "host_identity", lambda _: {"executable": "fixture"})
    materials = {"images": images, "mesh": None, "building_input": None, "thickness_candidates_mm": {"glazing": [24]}}
    run = launcher.prepare(case, material_paths=materials)
    manifest = json.loads((run / "inputs.json").read_bytes())
    monkeypatch.setattr(relay, "CASE", case)
    initial, specs = relay.cfg()
    guide, prompt = initial[0]["content"], initial[1]["content"][0]["text"]
    assert initial[0]["role"] == "system" and initial[1]["role"] == "user"
    assert "sim_inspection" in guide and '"glazing": [24]' in prompt
    assert SPATIAL_GRADE_SCALE in guide and SPATIAL_GRADE_SCALE not in prompt
    assert "Task kickoff" not in prompt and "plan.png" in prompt and manifest["scope"] == ""
    # Empty kickoff cannot be used to attribute a user material declaration.
    with pytest.raises(ValueError, match="evidence reference 'brief'"):
        apply_rules(Toolkit(run), {"floors": [], "spaces": [], "boundaries": [{"id": "face", "space_id": "z"}], "openings": []}, _empty(),
            {"rules": [{"kind": "boundary", "ids": ["face"], "material": {
                "en": "Oak", "zh": "橡木", "source": "from user", "source_refs": ["brief"]}}]})
    assert manifest["spatial_assessment_policy"] == "object_evidence_v1"
    assert {s["function"]["name"] for s in specs} >= {"record_sim_inspection", "finish_bim"}
    verify_resume_inputs(manifest, materials)
    with pytest.raises(ValueError, match="thickness"):
        verify_resume_inputs(manifest, {**materials, "thickness_candidates_mm": {"glazing": [32]}})


def test_interrupted_finish_restores_bundle_and_readers_never_see_partial_writes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import sys
    from threading import Event
    from src.agent.execution.handoff_transaction import finish_transaction, handoff_reader, handoff_writer

    selected = tmp_path / "candidate_01"
    selected.mkdir()
    viewer = selected / "viewer.html"
    viewer.write_bytes(b"previous")
    program = """
import os, sys
from pathlib import Path
from src.agent.execution.handoff_transaction import finish_transaction
run = Path(sys.argv[1])
with finish_transaction(run, "candidate_01"):
    (run / "delivery.json").write_bytes(b'incomplete')
    (run / "candidate_01/viewer.html").write_bytes(b'incomplete')
    os._exit(61)
"""
    process = subprocess.run([sys.executable, "-c", program, str(tmp_path)], check=False)
    assert process.returncode == 61
    with pytest.raises(ValueError, match="Interrupted"):
        with handoff_reader(tmp_path):
            pytest.fail("A pending rollback cannot be consumed as a handoff")
    with handoff_writer(tmp_path):
        assert viewer.read_bytes() == b"previous" and not (tmp_path / "delivery.json").exists()

    reading = Event()
    def observe():
        reading.set()
        with handoff_reader(tmp_path):
            return viewer.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as readers:
        with pytest.raises(ValueError, match="reject"):
            with finish_transaction(tmp_path, "candidate_01"):
                viewer.write_bytes(b"intermediate")
                pending = readers.submit(observe)
                assert reading.wait(5) and not pending.done()
                raise ValueError("reject this attempt")
        assert pending.result(timeout=10) == b"previous"
