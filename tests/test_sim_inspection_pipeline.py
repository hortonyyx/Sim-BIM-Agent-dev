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
        host_id = source["openings"][0]["host_boundary_id"]
        wall_construction = {"kind": "boundary", "ids": [host_id],
            "material": {"en": "Concrete", "zh": "混凝土", "source": "from user", "source_refs": ["brief"]},
            "thickness": {"value_mm": 200, "source": "from user", "source_refs": ["brief"]}}
        packet = {"rules": [boundary, opening, oak, wall_construction]}
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
        with monkeypatch.context() as patch:
            patch.setitem(toolkit.manifest, "model_revision", {"parentCandidate": candidate})
            carried = inherited_records(toolkit, moved, candidate)
            assert carried["openings"][door["id"]] == {"material": oak["material"]}
            assert carried["boundaries"][host_id] == {
                key: wall_construction[key] for key in ("material", "thickness")}
            updated = apply_rules(toolkit, moved, carried, {"rules": [
                {"kind": "boundary", "ids": [host_id],
                 "thickness": {**wall_construction["thickness"], "value_mm": 320}}]})
            assert updated["boundaries"][host_id]["thickness"]["value_mm"] == 320
            assert updated["boundaries"][host_id]["material"] == wall_construction["material"]
            door["kind"] = "window"
            assert door["id"] not in inherited_records(toolkit, moved, candidate)["openings"]
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


def test_missing_spatial_selects_only_unassessed_revision_objects_and_fields(tmp_path):
    from scripts.tool_scripts.bim_agent_inspection import revision_spatial_feedback

    run, _, _ = _run(tmp_path)
    toolkit = Toolkit(run)
    source = {"floors": [{"id": "L1"}, {"id": "L2"}],
              "spaces": [{"id": "s1", "floor_id": "L1"}, {"id": "s2", "floor_id": "L2"}],
              "boundaries": [{"id": i, "space_id": s, "geometry_type": t} for i, s, t in (
                  ("w0", "s1", "wall"), ("w1", "s1", "wall"), ("w2", "s2", "wall"), ("f", "s1", "floor"))],
              "openings": [{"id": "d", "kind": "door"}, {"id": "p", "kind": "open"}]}
    old = _grade(1, "Unconfirmed.", "待核实。")
    new = _grade(2, "Located on plan.", "平面可见。", ["image:plan.png"])
    thickness = {"value_mm": 180, "source": "inference"}
    rows = {**_empty(), "boundaries": {"w0": {"spatial": old}, "w1": {"thickness": thickness}}}
    rule = {"kind": "boundary", "missing_spatial": True, "spatial": new}
    def apply(records, *rules):
        return apply_rules(toolkit, source, records, {"rules": list(rules)})

    with pytest.raises(ValueError, match="editing a delivered model"):
        apply(rows, rule)
    toolkit.manifest["model_revision"] = {"parentCandidate": "candidate_01"}
    for invalid in ({"all": True}, {"thickness": thickness}, {"material": None},
                    {"ids": ["stale"]}, {"missing_spatial": 1}, {"missing_spatial": False}):
        with pytest.raises(ValueError):
            apply(rows, {**rule, **invalid})
    floor = {**rule, "type": "wall", "floor_ids": ["L1"], "space_ids": ["s1"], "ids": ["w0", "w1", "w2"]}
    first = apply(rows, floor, {**floor, "spatial": old})
    assert first["boundaries"] == {"w0": {"spatial": old}, "w1": {"spatial": new, "thickness": thickness}}
    assert apply(first, floor) == first
    assert apply(first, {**rule, "type": "ceiling"}) == first
    second = apply(first, rule, {**rule, "kind": "opening", "physical_only": True})
    assert second["boundaries"] == {**first["boundaries"], "w2": {"spatial": new}, "f": {"spatial": new}}
    assert second["openings"] == {"d": {"spatial": new}}
    assert rows["boundaries"]["w1"] == {"thickness": thickness}
    assessment = {"spatial": {group: {i: v["spatial"] for i, v in objects.items() if "spatial" in v}
                              for group, objects in second.items()}}
    feedback = revision_spatial_feedback(source, assessment)
    assert {group: value["missing"] for group, value in feedback["coverage"].items()} == {
        "spaces": 2, "boundaries": 0, "openings": 1}
    assert feedback["spatial_selectors"] == [{"kind": "space", "missing_spatial": True},
                                             {"kind": "opening", "missing_spatial": True}]


def test_revision_keeps_construction_on_moved_partition_and_lengthened_wall():
    from scripts.tool_scripts.bim_agent_inspection import _carried_construction

    def ring(lo, hi, y):
        return [[lo, y, 0], [hi, y, 0], [hi, y, 3], [lo, y, 3]]

    wall = {"id": "corridor/wall/4", "space_id": "corridor", "geometry_type": "wall",
            "kind": "physical", "enclosure": "physical", "vertices": ring(0, 4, 0),
            "adjacent_space_ids": ["service"], "counterpart_ids": ["service/wall/0"]}
    thickness = {"value_mm": 160, "source": "from user", "source_refs": ["brief"]}
    region = {"vertices": ring(2, 4, 0), "thickness": {**thickness, "value_mm": 200}}
    fields = {"material": {"en": "Masonry", "zh": "砌体", "source": "from user", "source_refs": ["brief"]},
              "thickness": thickness, "thickness_regions": [region], "spatial": {"grade": 4}}
    retained = _carried_construction("boundaries", wall, {**wall, "vertices": ring(0, 5, 0.1)}, fields)
    assert "spatial" not in retained and retained["thickness"] == thickness and retained["material"] == fields["material"]
    assert retained["thickness_regions"][0]["vertices"] == ring(2, 4, 0.1)
    assert region["vertices"] == ring(2, 4, 0)
    shorter = _carried_construction("boundaries", wall, {**wall, "vertices": ring(0, 3, 0)}, fields)
    assert shorter["thickness_regions"][0]["thickness"] == region["thickness"]
    assert {p[0] for p in shorter["thickness_regions"][0]["vertices"]} == {2, 3}
    for other in ({"space_id": "other"}, {"geometry_type": "floor"}, {"kind": "virtual"},
                  {"counterpart_ids": ["other/wall/1"]}, {"vertices": ring(4, 7, 0)},
                  {"vertices": list(reversed(wall["vertices"][:2])) + list(reversed(wall["vertices"][2:]))}):
        assert _carried_construction("boundaries", wall, {**wall, **other}, fields) == {}


def test_construction_filters_preserve_spatial_coverage_and_reject_null(tmp_path):
    from tests.test_sim_components import _source, _horizontal_pair, _declaration, _row, apply_source_enclosure

    run, _, _ = _run(tmp_path)
    toolkit = Toolkit(run)
    source = _source()
    lower, upper = _horizontal_pair(source)
    source = apply_source_enclosure(source, _declaration(source, [_row(lower["id"]), _row(upper["id"])]))
    full = {"kind": "boundary", "all": True, "spatial": _grade(1, "Assumed.", "推断。")}
    solid = {"kind": "boundary", "physical_only": True, "thickness": {"value_mm": 180, "source": "inference"}}
    original = _empty()
    for invalid, match in (({**solid, "all": True}, "Remove 'all'"),
                           ({**solid, "material": None}, "material must be an object"),
                           ({**solid, "thickness": None}, "thickness must be an object"),
                           ({"kind": "boundary", "ids": [upper["id"]], "thickness": solid["thickness"]},
                            "no physical area")):
        with pytest.raises(ValueError, match=match):
            apply_rules(toolkit, source, original, {"rules": [full, invalid]})
        assert original == _empty()
    records = apply_rules(toolkit, source, original, {"rules": [full, solid]})
    assert set(records["boundaries"]) == {row["id"] for row in source["boundaries"]}
    assert records["boundaries"][lower["id"]].keys() == {"spatial"}
    assert records["boundaries"][upper["id"]].keys() == {"spatial"}
    assert any("thickness" in fields for fields in records["boundaries"].values())


def test_only_frozen_revision_keys_can_support_user_evidence(tmp_path):
    from scripts.tool_scripts.bim_agent_inspection import _evidence_resolver
    run, _, _ = _run(tmp_path)
    toolkit = Toolkit(run)
    toolkit.manifest["revisionEvidence"] = {"note1": "The owner confirmed the wall material"}
    resolve = _evidence_resolver(toolkit)
    assert resolve("revision:note1") == "user"
    with pytest.raises(ValueError, match="Unresolved evidence"):
        resolve("revision:other")


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
