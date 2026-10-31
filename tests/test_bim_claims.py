"""Claims must feed actual edit parameters, not merely decorate arbitrary edits."""
import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from scripts.tool_scripts.run_bim_agent import Toolkit, digest
from src.agent.execution.source_proposal import export_source_proposal
from tests.test_bim_agent_tools import _json_result, _run_with_one_image, _server_session
from tests.test_source_proposal import _proposal
from tests.test_proposal_edits import _rectangular_wall_proposal


def setup_run(tmp_path, proposal=None):
    run = _run_with_one_image(tmp_path)
    report = export_source_proposal(proposal or _proposal(), run / "seed")
    assert report["source_geometry_ready"]
    return run, Toolkit(run)


def claim(**changes):
    value = {"candidate": "seed", "objects": [{"kind": "opening", "id": "door"}],
        "basis": "annotation_and_pixels", "reason": "Synthetic labelled vertical chain",
        "sources": [{"image": "plan.png", "box": [0, 0, 12, 8]}],
        "values": {"height": {"type": "dimension_chain", "lengths": [900, 1800, 300],
                    "unit": "mm", "origin_m": 3.0, "direction": -1, "segment": 1}}}
    value.update(changes)
    return value


def adopt(toolkit, data):
    row = toolkit.record_claim(json.dumps(data))
    toolkit.decide_claim(row["id"], "adopted", "Use the declared source evidence")
    return row


def edit(identity, target="door"):
    return {"op": "update_opening", "id": target,
            "changes": {"z": {"claim": identity, "value": "height"}}, "reason": "Apply recorded chain"}


@pytest.mark.parametrize("missing", [("direction",), ("origin_m", "direction")])
def test_chain_claim_errors_only_report_missing_chain_fields(missing):
    from pydantic import ValidationError
    from src.agent.execution.bim_claims import Claim

    data = claim()
    for name in missing:
        del data["values"]["height"][name]
    with pytest.raises(ValidationError) as caught:
        Claim.model_validate(data)
    errors = caught.value.errors()
    assert {tuple(error["loc"]) for error in errors} == {
        ("values", "height", "dimension_chain", name) for name in missing}
    assert all(error["type"] == "missing" for error in errors)
    assert len(str(caught.value).encode()) < 1000


@pytest.mark.parametrize("change", [
    {"type": "unknown"}, {"direction": 0}, {"origin_m": "0"}, {"invented": 1},
])
def test_claim_type_dispatch_keeps_strict_values_and_fields(change):
    from pydantic import ValidationError
    from src.agent.execution.bim_claims import Claim

    data = claim()
    data["values"]["height"].update(change)
    with pytest.raises(ValidationError):
        Claim.model_validate(data)


def test_real_parameter_application_and_candidate_provenance_survive_reload(tmp_path):
    run, toolkit = setup_run(tmp_path)
    before = (run / "seed/proposal.json").read_bytes()
    row = adopt(toolkit, claim())
    assert row["resolved_values"]["height"] == [.3, 2.1]
    result = toolkit.revise("seed", json.dumps([edit(row["id"])]))
    assert result["source_geometry_ready"]
    app = result["claim_application"]
    assert result["provenance"]["claim_application"] == {"file": "claims/application_0001.json"}
    assert app["status"] == "applied" and app["outside_declared_scope"] == []
    assert app["parameters_without_claims"] == []
    assert app["resolved_operations"][0]["changes"]["z"] == [.3, 2.1]
    source = json.loads((run / result["candidate"] / "source_model.json").read_text(encoding="utf-8"))
    door = next(row for row in source["openings"] if row["id"] == "door")
    assert sorted({v[2] for v in door["vertices"]}) == [.3, 2.1]
    assert source["generation"]["provenance"]["claim_application"]["claims"][row["id"]]["record"] == row
    assert [(c["kind"], c["id"]) for c in app["changes"]] == [("opening", "door")]
    assert (run / "seed/proposal.json").read_bytes() == before
    persisted = Toolkit(run).claims().status()
    assert persisted["claims"][0]["applied_anywhere"]
    assert persisted["applications"][0] == app


@pytest.mark.parametrize(("identity", "kind"), [("window", "window"), ("door", "opening")])
def test_aperture_removal_scope_matches_the_actual_proposal_collection(tmp_path, identity, kind):
    run, toolkit = setup_run(tmp_path)
    result = toolkit.revise("seed", json.dumps([{
        "op": "remove_opening", "id": identity, "reason": "synthetic aperture retraction",
        "source_refs": ["synthetic source review"],
    }]))
    app = result["claim_application"]
    assert app["status"] == "applied"
    assert app["scope_check"] == "checked" and app["outside_declared_scope"] == []
    assert [(row["kind"], row["id"], row["after"]) for row in app["changes"]] == [
        (kind, identity, None)]
    proposal = json.loads((run / result["candidate"] / "proposal.json").read_text(encoding="utf-8"))
    assert proposal["geometry"]["corrections"][-1]["target_collection"] == kind + "s"


def test_unadopted_retracted_wrong_target_and_stale_claims_leave_failure_records(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = toolkit.record_claim(json.dumps(claim()))
    with pytest.raises(ValueError, match="adopted"):
        toolkit.revise("seed", json.dumps([edit(row["id"])]))
    toolkit.decide_claim(row["id"], "adopted", "local evidence")
    wrong = {"op": "update_window", "id": "window", "changes": {"z": {"claim": row["id"], "value": "height"}}, "reason": "bad target"}
    with pytest.raises(ValueError, match="targets"):
        toolkit.revise("seed", json.dumps([wrong]))
    good = toolkit.revise("seed", json.dumps([edit(row["id"])]))
    with pytest.raises(ValueError, match="stale"):
        toolkit.revise(good["candidate"], json.dumps([edit(row["id"])]))
    toolkit.decide_claim(row["id"], "retracted", "new conflicting interpretation")
    with pytest.raises(ValueError, match="adopted"):
        toolkit.revise("seed", json.dumps([edit(row["id"])]))
    assert [row["status"] for row in toolkit.claims().status()["applications"]] == ["failed", "failed", "applied", "failed", "failed"]
    assert len(list(run.glob("candidate_*"))) == 1


def test_saved_profile_values_are_used_and_mutated_profiles_rejected(tmp_path):
    run, _ = setup_run(tmp_path)
    path = run / "images/plan.png"
    picture = Image.new("RGB", (12, 8), "white")
    for x in range(12):
        for y in (2, 6):
            picture.putpixel((x, y), (0, 255, 255))
    picture.save(path)
    manifest = json.loads((run / "inputs.json").read_text(encoding="utf-8"))
    manifest["images"]["plan.png"]["sha256"] = digest(path)
    (run / "inputs.json").write_text(json.dumps(manifest))
    toolkit = Toolkit(run)
    toolkit.view_profile("plan.png", [0, 0, 12, 8], "y", [0, 255, 255], 0, .5)
    spec = {"type": "image_axis", "image": "plan.png", "axis": "y",
            "anchors": [[0, 3.0], [8, 0.0]], "pixels": [
                {"profile": "profile_001", "candidate": "C01", "at": "peak"},
                {"profile": "profile_001", "candidate": "C02", "at": "peak"}]}
    row = adopt(toolkit, claim(basis="pixels", values={"height": spec}))
    assert row["resolved_values"]["height"] == [.75, 2.25]
    result = toolkit.revise("seed", json.dumps([edit(row["id"])]))
    assert result["claim_application"]["resolved_operations"][0]["changes"]["z"] == [.75, 2.25]
    profile = run / "pixel_profiles/profile_001.json"
    record = json.loads(profile.read_text(encoding="utf-8"))
    record["rgb"] = [0, 254, 255]
    profile.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="evidence changed"):
        toolkit.revise("seed", json.dumps([edit(row["id"])]))


@pytest.mark.parametrize("problem", ["nan", "bounds", "object", "image", "unit"])
def test_invalid_claims_never_enter_store(tmp_path, problem):
    _, toolkit = setup_run(tmp_path)
    data = claim()
    if problem == "nan":
        data["values"]["height"]["lengths"][0] = float("nan")
    elif problem == "bounds":
        data["sources"][0]["box"][2] = 13
    elif problem == "object":
        data["objects"][0]["id"] = "absent"
    elif problem == "image":
        data["sources"][0]["image"] = "../secret.png"
    else:
        data["values"]["height"]["unit"] = "cm"
    with pytest.raises(ValueError):
        toolkit.record_claim(json.dumps(data))
    assert toolkit.claims().status()["claims"] == []


def test_valid_claim_with_impossible_geometry_is_not_reported_applied(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = adopt(toolkit, claim(basis="inference", sources=[], values={
        "height": {"type": "literal", "value": [0, 9], "unit": "m"}}))
    result = toolkit.revise("seed", json.dumps([edit(row["id"])]))
    assert not result["source_geometry_ready"]
    assert result["claim_application"]["status"] == "failed"
    assert (run / result["candidate"] / "application.json").exists()
    delivered = toolkit.delivery("seed", selection_origin="agent_selected")
    assert delivered["adopted_unapplied_claims"] == [row["id"]]
    assert delivered["claim_applications"][0]["status"] == "failed"


def test_failed_revision_in_partial_transaction_preserves_last_commit_and_diagnostics(tmp_path):
    run, toolkit = setup_run(tmp_path)
    before = (run / "seed/source_model.json").read_bytes()
    impossible = claim(objects=[{"kind": "window", "id": "window"}], basis="inference", sources=[],
        values={"height": {"type": "literal", "value": [1, 9], "unit": "m"}})
    impossible.pop("candidate")  # Bind this independent entry to the preceding successful source.
    result = toolkit.claim_transaction("seed", json.dumps([
        {"claim": claim(), "action": "apply", "operations": [edit("$claim")]},
        {"claim": impossible, "action": "apply", "operations": [{
            "op": "update_window", "id": "window", "changes": {"z": {"claim": "$claim", "value": "height"}},
            "reason": "Synthetic impossible height to exercise source-host validation."}]},
    ]))
    assert result["status"] == "partial"
    assert result["saved_candidate"] == result["result_candidate"] == "candidate_01"
    assert result["save_effects"]["created_candidates"] == ["candidate_01"]
    assert result["save_effects"]["geometry_applied"] is True
    applied, failed = result["entries"]
    assert applied["status"] == "applied" and failed["status"] == "failed"
    assert failed["candidate"] == "candidate_01" and failed["attempted_candidate"] == "candidate_02"
    assert "window" in failed["error"] and "host" in failed["error"]
    assert any(row.get("opening_id") == "window" for row in failed["source_validation"]["findings"])
    journal = json.loads((run / result["audit_file"]).read_bytes())
    assert journal["entries"][1]["source_validation"] == failed["source_validation"]
    assert journal["entries"][1]["attempted_candidate"] == "candidate_02"
    assert journal["entries"][1]["error"] == failed["error"]
    assert result["failure_summary"]["entries"][0]["attempted_candidate"] == "candidate_02"
    attempted = json.loads((run / "candidate_02/report.json").read_bytes())
    assert attempted["source_geometry_ready"] is False
    actual = json.loads((run / "candidate_01/proposal.json").read_bytes())
    assert actual["geometry"]["openings"][0]["z"] == [.3, 2.1]
    assert actual["geometry"]["windows"][0]["z"] == [1, 2]
    assert (run / "seed/source_model.json").read_bytes() == before


def test_shared_wall_move_records_legitimate_hosted_door_and_preserves_other_floor(tmp_path):
    _, toolkit = setup_run(tmp_path, _rectangular_wall_proposal())
    row = adopt(toolkit, claim(objects=[{"kind": "space", "id": x} for x in ("left", "right")],
        basis="pixels", values={"position": {"type": "image_axis", "image": "plan.png", "axis": "x",
            "anchors": [[0, 0], [12, 10]], "pixels": [6]}}))
    result = toolkit.revise("seed", json.dumps([{"op": "move_shared_wall", "space_ids": ["left", "right"],
        "coordinate_m": {"claim": row["id"], "value": "position"}, "reason": "synthetic wall evidence"}]))
    app = result["claim_application"]
    assert app["status"] == "applied" and not app["outside_declared_scope"]
    assert {(row["kind"], row["id"]) for row in app["changes"]} == {("space", "left"), ("space", "right"), ("opening", "between")}


@pytest.mark.parametrize("kind", ["wall", "floor"])
def test_component_thickness_persists_without_changing_geometry(tmp_path, kind):
    run, toolkit = setup_run(tmp_path)
    source = json.loads((run / "seed/source_model.json").read_text(encoding="utf-8"))
    boundary = next(b for b in source["boundaries"] if b["geometry_type"] == kind and
                    (b["counterpart_ids"] if kind == "wall" else True))
    row = adopt(toolkit, claim(objects=[{"kind": "boundary", "id": boundary["id"]}],
        basis="declared", sources=[], values={"thickness": {"type": "literal", "value": .2, "unit": "m"}}))
    result = toolkit.revise("seed", json.dumps([{"op": "set_component_thickness", "boundary_id": boundary["id"],
        "thickness_m": {"claim": row["id"], "value": "thickness"}, "basis": "declared overall thickness",
        "reason": "retain building property"}]))
    app = result["claim_application"]
    assert app["status"] == "applied" and not app["changes"]
    assert app["geometry_before_sha256"] == app["geometry_after_sha256"]
    new = json.loads((run / result["candidate"] / "source_model.json").read_text(encoding="utf-8"))
    assert len(new["component_attributes"]) == 1
    assert new["component_attributes"][0]["thickness_m"] == .2
    assert len(new["component_attributes"][0]["boundary_ids"]) == (2 if kind == "wall" else 1)
    for field in ("spaces", "boundaries", "openings", "connections"):
        assert new[field] == source[field]


def test_stdio_claim_transaction_is_coordinator_only_and_applies_bound_edit(tmp_path):
    async def scenario():
        run, _ = setup_run(tmp_path)
        async with _server_session(run, readonly=True) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert not names.intersection({"claim_transaction", "claim_status", "revise_bim"})
        async with _server_session(run, readonly=False) as session:
            built = _json_result(await session.call_tool("claim_transaction", {"candidate": "seed",
                "entries_json": json.dumps([dict(claim=claim(), action="apply", reason="Source chain", operations=[edit("$claim")])])}))
            assert built["status"] == "completed"
            assert built["save_effects"]["geometry_applied"]
            status = _json_result(await session.call_tool("claim_status", {}))
            assert status["claims"][0]["applied_anywhere"]
    asyncio.run(scenario())


def test_stdio_saved_claim_regions_reopen_exact_pixels_without_changing_evidence(tmp_path):
    async def scenario():
        run, toolkit = setup_run(tmp_path)
        original = Image.new("RGB", (12, 8))
        original.putdata([(x * 20, y * 30, 100) for y in range(8) for x in range(12)])
        original.save(run / "images/plan.png")
        toolkit.manifest["images"]["plan.png"]["sha256"] = digest(run / "images/plan.png")
        (run / "inputs.json").write_text(json.dumps(toolkit.manifest), encoding="utf-8")
        refs = [{"image": "plan.png", "box": box} for box in
                ([0, 0, 6, 8], [6, 0, 12, 8], [1.2, 2.3, 1.3, 2.4], [3, 4, 9, 8])]
        row = toolkit.record_claim(json.dumps(claim(sources=refs)))
        path = run / "claims" / (row["id"] + ".json")
        before = path.read_bytes()
        async with _server_session(run, readonly=False) as session:
            for index, box in enumerate(([0, 0, 6, 8], [6, 0, 12, 8], [1, 2, 2, 3], [3, 4, 9, 8])):
                reply = await session.call_tool("view_image", {"claim_id": row["id"], "source_index": index})
                metadata = _json_result(reply)
                image = next(part for part in reply.content if part.type == "image")
                actual = Image.open(io.BytesIO(base64.b64decode(image.data))).convert("RGB")
                expected = original.crop(box)
                assert actual.size == expected.size and actual.tobytes() == expected.tobytes()
                assert metadata["box_original_pixels"] == box
            assert path.read_bytes() == before
    asyncio.run(scenario())


def test_claim_preview_preserves_record_and_geometry_and_rejects_changed_original(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = toolkit.record_claim(json.dumps(claim()))
    before = (run / "seed/source_model.json").read_bytes()
    saved = (run / "claims" / (row["id"] + ".json")).read_bytes()
    toolkit.view_claim_evidence(row["id"])
    corrected = toolkit.record_claim(json.dumps(claim(sources=[{"image": "plan.png", "box": [1, 1, 10, 7]}])))
    toolkit.decide_claim(row["id"], "retracted", "wrong located region")
    toolkit.view_claim_evidence(corrected["id"])
    assert (run / "seed/source_model.json").read_bytes() == before
    assert (run / "claims" / (row["id"] + ".json")).read_bytes() == saved
    assert toolkit.claims().status()["applications"] == []
    with pytest.raises(ValueError, match="coordinator"):
        Toolkit(run, readonly=True).view_claim_evidence(row["id"])
    for index in (-1, 1, True):
        with pytest.raises(ValueError, match="source_index"):
            toolkit.view_claim_evidence(row["id"], index)
    Image.new("RGB", (12, 8), "red").save(run / "images/plan.png")
    with pytest.raises(ValueError, match="image changed"):
        toolkit.view_claim_evidence(row["id"])
    # Even re-admitting changed bytes cannot make an old claim refer to them.
    toolkit.manifest["images"]["plan.png"]["sha256"] = digest(run / "images/plan.png")
    with pytest.raises(ValueError, match="claim original image changed"):
        toolkit.view_claim_evidence(row["id"])


def test_stdio_inferred_claim_transaction_does_not_invent_image_evidence(tmp_path):
    async def scenario():
        run, _ = setup_run(tmp_path)
        async with _server_session(run, readonly=False) as session:
            reply = await session.call_tool("claim_transaction", {"candidate": "seed", "entries_json": json.dumps([
                dict(claim=claim(basis="inference", sources=[]), action="record")])})
            result = _json_result(reply)
            assert result["status"] == "completed"
            assert not any(part.type == "image" for part in reply.content)
            row = json.loads((run / "claims" / (result["entries"][0]["claim_id"] + ".json")).read_bytes())
            assert row["sources"] == []
    asyncio.run(scenario())


def test_whole_image_claim_reference_binds_original_extent_without_guessing_box(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = toolkit.record_claim(json.dumps(claim(sources=[{"image": "plan.png"}])))
    assert row["claim"]["sources"][0]["box"] is None
    assert row["sources"][0] == {"image": "plan.png", "box": [0, 0, 12, 8],
        "size": [12, 8], "sha256": digest(run / "images/plan.png")}
    reply = toolkit.view_claim_evidence(row["id"])
    actual = Image.open(io.BytesIO(base64.b64decode(reply.content[0].data))).convert("RGB")
    with Image.open(run / "images/plan.png") as original:
        assert actual.tobytes() == original.convert("RGB").tobytes()
    assert row["verification"] == "not_independently_verified"
