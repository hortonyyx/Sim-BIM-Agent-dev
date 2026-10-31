"""Trusted drawing admission governs every single-model source geometry write."""
from __future__ import annotations

import copy
import hashlib
import json

from PIL import Image

from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.runtime_entry import prepare_inputs
from tests.test_lite_bim_regularization import SIZE, plan


def _read(path):
    return json.loads(path.read_bytes())


def _hashes(paths):
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _prepared(tmp_path, *, floors=1):
    originals = tmp_path / "originals"
    originals.mkdir()
    names = [f"floor_{i + 1}.png" for i in range(floors)]
    for name in names:
        Image.new("RGB", SIZE, "white").save(originals / name)
    before = _hashes(originals.iterdir())
    run, _, _ = prepare_inputs(tmp_path / "run", images=originals, mesh=None,
        building_input=None, scope="Synthetic drawing; no declared grid or model hints.",
        image_kind="drawings", max_candidates=12, floor_plan_images=names)
    assert _hashes(before) == before
    assert _read(run / "inputs.json")["lite_bim_grid_step_m"] == 0.1
    immutable = _hashes([*before, run / "inputs.json", *(run / "images").iterdir()])
    return Toolkit(run), names, immutable


def _build(toolkit, image, declaration):
    raw = json.dumps(declaration, ensure_ascii=False, indent=2) + "\n"
    result = toolkit.build_plan(image, raw)
    assert result.get("source_geometry_ready"), result
    record = result["plan_input"]
    submitted = toolkit.run / record["submitted_plan_file"]
    assert submitted.read_bytes() == raw.encode("utf-8")
    assert hashlib.sha256(submitted.read_bytes()).hexdigest() == record["submitted_plan_sha256"]
    return result, submitted


def _source(toolkit, result):
    source = _read(toolkit.run / result["candidate"] / "source_model.json")
    coordinates = [v for collection in ("boundaries", "openings")
                   for row in source[collection] for point in row["vertices"] for v in point]
    assert coordinates
    assert all(abs(v * 10 - round(v * 10)) < 1e-7 for v in coordinates)
    return source


def test_drawing_admission_applies_grid_without_model_marker_and_keeps_raw_inputs(tmp_path):
    toolkit, images, immutable = _prepared(tmp_path)
    declaration = plan()
    assert "regularization_inputs" not in declaration
    original = copy.deepcopy(declaration)
    result, _ = _build(toolkit, images[0], declaration)
    source = _source(toolkit, result)
    assert len(source["spaces"]) == 3 and len(source["openings"]) == 2
    effective = _read(toolkit.run / result["plan_input"]["plan_file"])
    assert effective["z_floor"] == 0 and effective["ceiling_height"] == 4
    assert effective["partitions"][0]["points"][0][0] == 490
    assert effective["partitions"][1]["points"][0][0] == 890
    assert declaration == original
    assert _hashes(immutable) == immutable


def test_model_audit_cannot_override_trusted_grid_and_local_revision_reapplies_it(tmp_path):
    toolkit, images, immutable = _prepared(tmp_path)
    declaration = plan()
    declaration["regularization_inputs"] = {
        "reading_alignment": {"schema_version": "synthetic_reading_alignment_v1",
            "ink": {"summary": {}, "items": [], "rejections": [], "search_or_tolerance": {}},
            "dimensions": {"summary": {}, "items": [], "rejections": [], "search_or_tolerance": {}},
            "lite_bim": {"grid_step_m": 0.2}}}
    declaration["regularization"] = {"lite_bim": {"grid_step_m": 0.2}, "status": "passed"}
    result, submitted = _build(toolkit, images[0], declaration)
    _source(toolkit, result)
    effective = _read(toolkit.run / result["plan_input"]["plan_file"])
    # The forged 0.2 m policy would instead place this wall at x=4.0 m.
    assert effective["partitions"][0]["points"][0][0] == 490
    before = _hashes([submitted, toolkit.run / result["plan_input"]["plan_file"]])
    draft_id = (toolkit.run / result["plan_input"]["plan_file"]).parent.name
    revised = toolkit.revise_plan(draft_id, result["plan_input"]["plan_sha256"], json.dumps([
        {"op": "update", "collection": "openings", "id": "W",
         "changes": {"z": [1.04, 2.66]}, "reason": "Synthetic local height observation",
         "source_refs": [images[0]]}]))
    assert revised.get("source_geometry_ready"), revised
    _source(toolkit, revised)
    revised_plan = _read(toolkit.run / revised["plan_input"]["plan_file"])
    assert next(row for row in revised_plan["openings"] if row["id"] == "W")["z"] == [1.0, 2.7]
    assert _hashes(before) == before and _hashes(immutable) == immutable


def test_two_floor_assembly_keeps_grid_and_bound_declarations_unchanged(tmp_path):
    toolkit, images, immutable = _prepared(tmp_path, floors=2)
    rows, saved = [], []
    for index, image in enumerate(images):
        declaration = plan()
        result, submitted = _build(toolkit, image, declaration)
        saved.extend([submitted, toolkit.run / result["plan_input"]["plan_file"]])
        rows.append({"draft_id": saved[-1].parent.name,
            "expected_plan_sha256": result["plan_input"]["plan_sha256"],
            "floor_id": f"F{index + 1}", "z_floor": index * 4.0,
            "evidence": "Synthetic floor placement"})
    before = _hashes(saved)
    off_grid = [dict(row, z_floor=row["z_floor"] + 0.02) for row in rows]
    rejected = toolkit.assemble_plans(json.dumps(off_grid))
    assert rejected["status"] == "error" and not rejected["source_geometry_ready"]
    assert "off grid" in rejected["error"]
    assert len(list(toolkit.run.glob("candidate_*"))) == 2
    assert _hashes(before) == before
    result = toolkit.assemble_plans(json.dumps(rows))
    assert result.get("source_geometry_ready"), result
    source = _source(toolkit, result)
    assert len(source["floors"]) == 2
    assert len(source["spaces"]) == 6 and len(source["openings"]) == 4
    assert _hashes(before) == before and _hashes(immutable) == immutable


def test_grid_only_admission_rejects_off_grid_edits_without_changing_source(tmp_path):
    toolkit, images, immutable = _prepared(tmp_path)
    # This run declares the world-coordinate grid only. Reference-line adoption
    # has its own parameter proof and is exercised in drawing_source_precision.
    manifest_path = toolkit.run / "inputs.json"
    manifest = _read(manifest_path)
    manifest.pop("source_precision_policy")
    manifest.pop("architectural_modelling_policy")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    immutable = _hashes(immutable)
    toolkit = Toolkit(toolkit.run)
    built, _ = _build(toolkit, images[0], plan())
    candidate = toolkit.run / built["candidate"]
    before = _hashes(candidate.iterdir())
    candidates = sorted(path.name for path in toolkit.run.glob("candidate_*"))
    proposal = _read(candidate / "proposal.json")
    proposal["geometry"]["windows"][0]["z"][0] = 0.94
    failed_build = toolkit.build(proposal)
    failed_revision = toolkit.revise(built["candidate"], json.dumps([
        {"op": "update_window", "id": "W", "changes": {"z": [0.94, 2.6]},
         "reason": "Synthetic off-grid write", "source_refs": [images[0]]}]))
    for result in (failed_build, failed_revision):
        assert result["status"] == "error" and not result["source_geometry_ready"]
        assert result["error_stage"] == "source_hard_constraints"
        assert "0.1 m" in result["error"]
    rejected = list((toolkit.run / "geometry_rejections").glob("attempt_*/proposal.json"))
    assert len(rejected) == 2
    assert all(_read(path)["geometry"]["windows"][0]["z"][0] == 0.94 for path in rejected)
    assert sorted(path.name for path in toolkit.run.glob("candidate_*")) == candidates
    assert _hashes(before) == before and _hashes(immutable) == immutable
